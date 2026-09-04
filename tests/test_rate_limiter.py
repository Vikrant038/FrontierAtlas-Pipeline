"""
Unit tests for per-key LLM rate-limit windows (Phase 7 parallelization):
pooled API keys each get an independent RPM window, and the tier selector
picks the first unsaturated key instead of failing over while siblings idle.
Follows AAA pattern per CODING_STANDARDS.md.
"""

from unittest.mock import patch

import pytest

from src.config import settings
from src.llm.fallback_chain import llm_engine
from src.llm.rate_limiter import ProviderRateLimiter, RateLimitExceededError, rate_limiter


@pytest.fixture(autouse=True)
def _reset_global_limiter():
    """Clear the global limiter's windows after each test (test isolation)."""
    yield
    rate_limiter._history.clear()
    rate_limiter._locks.clear()


async def _fill_window(provider: str, key: str) -> None:
    """Acquire until the key's window is saturated."""
    while True:
        try:
            await rate_limiter.acquire(provider, max_wait=0.0, key=key)
        except RateLimitExceededError:
            return


@pytest.mark.asyncio
async def test_acquire_without_key_uses_provider_window():
    # Arrange
    limiter = ProviderRateLimiter(rpm_limits={"prov": 2}, window_seconds=60.0)

    # Act
    await limiter.acquire("prov", max_wait=0.0)
    await limiter.acquire("prov", max_wait=0.0)

    # Assert: 3rd call within the window is refused at max_wait=0
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire("prov", max_wait=0.0)


@pytest.mark.asyncio
async def test_per_key_windows_are_independent():
    # Arrange
    limiter = ProviderRateLimiter(rpm_limits={"prov": 2}, window_seconds=60.0)
    await limiter.acquire("prov", max_wait=0.0, key="k1")
    await limiter.acquire("prov", max_wait=0.0, key="k1")

    # Act & Assert: k1 saturated at 2 RPM, but k2's window is untouched
    with pytest.raises(RateLimitExceededError):
        await limiter.acquire("prov", max_wait=0.0, key="k1")
    await limiter.acquire("prov", max_wait=0.0, key="k2")
    await limiter.acquire("prov", max_wait=0.0, key="k2")

    # Assert: the keyless provider window is a separate bucket from keyed ones
    await limiter.acquire("prov", max_wait=0.0)
    await limiter.acquire("prov", max_wait=0.0)


@pytest.mark.asyncio
async def test_tier_slot_picks_first_unsaturated_key():
    # Arrange: saturate k1's gemini window only
    with patch.object(settings, "gemini_api_keys", "k1,k2"):
        await _fill_window("gemini", "k1")

        # Act
        chosen = await llm_engine._acquire_tier_slot("gemini", settings.gemini_api_key_list, "probe-salt")

        # Assert: falls through to the idle k2 rather than failing over providers
        assert chosen == "k2"


@pytest.mark.asyncio
async def test_tier_slot_raises_when_all_keys_saturated():
    # Arrange
    with patch.object(settings, "gemini_api_keys", "k1,k2"):
        await _fill_window("gemini", "k1")
        await _fill_window("gemini", "k2")

        # Act & Assert: every key window full -> error drives tier failover
        with pytest.raises(RateLimitExceededError):
            await llm_engine._acquire_tier_slot("gemini", settings.gemini_api_key_list, "probe-salt")


@pytest.mark.asyncio
async def test_tier_slot_single_key_returns_it():
    # Arrange
    with patch.object(settings, "gemini_api_keys", "only-key"):
        # Act
        chosen = await llm_engine._acquire_tier_slot("gemini", settings.gemini_api_key_list, "probe-salt")

        # Assert: single-key pool returns the key (its own window paces it)
        assert chosen == "only-key"


@pytest.mark.asyncio
async def test_tier_slot_no_keys_uses_provider_window():
    # Arrange: no keys configured for the tier
    with patch.object(settings, "gemini_api_keys", None), patch.object(settings, "gemini_api_key", None):
        # Act: no key -> provider-level window grant, None returned
        chosen = await llm_engine._acquire_tier_slot("gemini", [], "probe-salt")

        # Assert
        assert chosen is None
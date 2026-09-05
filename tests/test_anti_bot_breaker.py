"""
Unit tests for the per-host anti-bot circuit breaker and challenge-page
detection in src.crawlers.base. Follows AAA pattern per CODING_STANDARDS.md;
module-level breaker state is reset after every test so a tripped host cannot
leak into other tests.
"""

import time
from unittest.mock import AsyncMock

import pytest

import src.crawlers.anti_bot as anti_bot
import src.crawlers.base as base
from src.crawlers.anti_bot import (
    _BLOCK_THRESHOLD,
    _BLOCK_WINDOW_SECONDS,
    _breaker_is_open,
    _looks_like_challenge,
    _record_block,
    _record_escalation_success,
)
from src.crawlers.base import BotBlockedError, TargetedCrawler


@pytest.fixture(autouse=True)
def _reset_breaker_state():
    """Clear module-level breaker state after every test (test isolation)."""
    yield
    base._block_history.clear()
    base._block_cooldown_until.clear()


def test_breaker_trips_after_threshold_blocks():
    # Arrange / Act
    host = "breaker.example.com"
    tripped = [_record_block(host) for _ in range(_BLOCK_THRESHOLD)]

    # Assert
    assert tripped == [False, False, True]
    assert _breaker_is_open(host)


def test_breaker_expires_after_cooldown(monkeypatch):
    # Arrange
    host = "expire.example.com"
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    for _ in range(_BLOCK_THRESHOLD):
        _record_block(host)
    assert _breaker_is_open(host)

    # Act: advance the clock past the cooldown window
    clock[0] += anti_bot._BLOCK_COOLDOWN_SECONDS + 1.0

    # Assert
    assert not _breaker_is_open(host)


def test_blocks_age_out_of_window_before_tripping(monkeypatch):
    # Arrange
    host = "aging.example.com"
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    _record_block(host)
    _record_block(host)
    clock[0] += _BLOCK_WINDOW_SECONDS + 1.0

    # Act: a third block after the two old ones aged out must not trip
    tripped = _record_block(host)

    # Assert
    assert tripped is False
    assert not _breaker_is_open(host)


def test_escalation_success_resets_host_history(monkeypatch):
    # Arrange
    host = "transient.example.com"
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    _record_block(host)
    _record_block(host)

    # Act
    _record_escalation_success(host)

    # Assert: history cleared, so a fresh block starts counting from zero
    assert _record_block(host) is False
    assert not _breaker_is_open(host)


@pytest.mark.asyncio
async def test_fetch_skips_escalation_when_circuit_open():
    # Arrange: trip the breaker for this host
    host = "fetch-open.example.com"
    for _ in range(_BLOCK_THRESHOLD):
        _record_block(host)
    crawler = TargetedCrawler(target_count=1)
    crawler._request = AsyncMock(side_effect=BotBlockedError(f"HTTP 403 for https://{host}/x"))
    crawler._escalate_tls = AsyncMock(return_value="escaped")

    # Act
    with pytest.raises(BotBlockedError):
        await crawler.fetch(f"https://{host}/x")
    await crawler.close()

    # Assert: block propagates to the caller's fallback without burning an escalation
    assert crawler._escalate_tls.await_count == 0


@pytest.mark.asyncio
async def test_fetch_escalates_and_resets_history_on_transient_block():
    # Arrange
    host = "transient-fetch.example.com"
    crawler = TargetedCrawler(target_count=1)
    crawler._request = AsyncMock(side_effect=BotBlockedError(f"HTTP 403 for https://{host}/x"))
    crawler._escalate_tls = AsyncMock(return_value="TLS content")

    # Act
    content = await crawler.fetch(f"https://{host}/x")
    await crawler.close()

    # Assert: one escalation ran and a success reset the host's block history
    assert content == "TLS content"
    assert crawler._escalate_tls.await_count == 1
    assert not base._block_history[host]


@pytest.mark.asyncio
async def test_escalate_tls_treats_challenge_page_as_block():
    # Arrange
    crawler = TargetedCrawler(target_count=1)

    async def fake_fetch_tls(url, params=None, timeout=None):
        return "<html><title>Attention Required!</title>cf-challenge</html>"

    crawler.fetch_tls = fake_fetch_tls  # type: ignore[method-assign]
    successes_before = crawler.escalation_successes

    # Act: TLS challenge escalates to Camoufox, which fails -> BotBlockedError
    with pytest.raises(BotBlockedError):
        await crawler._escalate_tls("https://challenge.example.com/page")
    await crawler.close()

    # Assert: a challenge page is never counted as an escalation success
    assert crawler.escalation_successes == successes_before


def test_looks_like_challenge_detects_short_interstitials():
    # Assert: short pages carrying verification markers are challenges
    assert _looks_like_challenge("<html><title>Attention Required! | Cloudflare</title><body>cf-challenge</body></html>")
    assert _looks_like_challenge("Please verify you are human to continue...")
    assert _looks_like_challenge("Access denied: ddos-guard challenge")


def test_looks_like_challenge_ignores_long_pages_and_non_strings():
    # Arrange: long pages mention these words but are real content
    long_page = "<p>" + "ordinary article about captcha and hcaptcha bots " * 400 + "</p>"

    # Assert
    assert not _looks_like_challenge(long_page)
    assert not _looks_like_challenge("")
    assert not _looks_like_challenge(None)
    assert not _looks_like_challenge(b"captcha")

@pytest.mark.asyncio
async def test_escalation_counters_are_instance_scoped():
    # A2: counters must be per-instance, not class-level shared state — one
    # crawler's escalations must not leak into another's telemetry.
    c1 = TargetedCrawler(target_count=1)
    c2 = TargetedCrawler(target_count=1)

    async def fake_fetch_tls(url, params=None, timeout=None):
        return "<html>real content, definitely not a challenge page</html>"

    c1.fetch_tls = fake_fetch_tls  # type: ignore[method-assign]
    c2.fetch_tls = fake_fetch_tls  # type: ignore[method-assign]

    await c1._escalate_tls("https://one.example.com/a")
    await c1._escalate_tls("https://one.example.com/b")
    await c2._escalate_tls("https://two.example.com/a")
    await c1.close()
    await c2.close()

    assert c1.escalation_attempts == 2 and c1.escalation_successes == 2
    assert c2.escalation_attempts == 1 and c2.escalation_successes == 1
    # Aggregation over a caller-provided registry (used by the run report)
    snap = base.anti_bot_snapshot(active_crawlers=[c1, c2])
    assert snap["escalation_attempts"] == 3
    assert snap["escalation_successes"] == 3

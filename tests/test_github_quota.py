"""
Unit tests for GitHub star enrichment pacing and quota-exhaustion shutdown in ResearchPapersCrawler.
Follows AAA pattern with offline respx mocking per CODING_STANDARDS.md.
"""

import asyncio

import httpx
import pytest
import respx

from src.config import settings
from src.crawlers.papers_crawler import (
    GITHUB_MIN_REQUEST_INTERVAL_SECONDS,
    ResearchPapersCrawler,
)


@pytest.mark.asyncio
async def test_github_pacing_enforces_minimum_interval():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)

    # Act
    first_slot_start = asyncio.get_running_loop().time()
    await crawler._pace_github()
    first_slot_end = asyncio.get_running_loop().time()
    second_slot_start = asyncio.get_running_loop().time()
    await crawler._pace_github()
    second_slot_end = asyncio.get_running_loop().time()

    # Assert - first call is unpaced, second waits at least the minimum interval
    assert first_slot_end - first_slot_start < GITHUB_MIN_REQUEST_INTERVAL_SECONDS
    assert second_slot_end - second_slot_start >= GITHUB_MIN_REQUEST_INTERVAL_SECONDS * 0.95


@pytest.mark.asyncio
async def test_pace_github_different_tokens_do_not_serialize(monkeypatch):
    # Arrange: shorten the pacing interval so the test runs fast
    monkeypatch.setattr(settings, "github_interval_seconds", 0.2)
    crawler = ResearchPapersCrawler(target_count=1)

    # Act: first use of two different pooled tokens fires both immediately
    start = asyncio.get_running_loop().time()
    await asyncio.gather(crawler._pace_github("tok-a"), crawler._pace_github("tok-b"))
    elapsed = asyncio.get_running_loop().time() - start
    await crawler.close()

    # Assert: independent per-token slots, no shared wait
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_pace_github_same_token_chains_at_interval(monkeypatch):
    # Arrange
    monkeypatch.setattr(settings, "github_interval_seconds", 0.2)
    crawler = ResearchPapersCrawler(target_count=1)

    # Act: concurrent lookups with the same token chain at the pacing interval
    start = asyncio.get_running_loop().time()
    await asyncio.gather(crawler._pace_github("tok-a"), crawler._pace_github("tok-a"))
    elapsed = asyncio.get_running_loop().time() - start
    await crawler.close()

    # Assert: the second same-token call waited for the first's slot to clear
    assert elapsed >= 0.19


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stars_403_quota_message_disables_enrichment():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)
    respx.get("https://api.github.com/repos/some-org/some-repo").mock(
        return_value=httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            text="API rate limit exceeded",
        )
    )
    assert crawler._github_quota_blocked() is False

    # Act
    repo_url, stars = await crawler._fetch_stars("some-org/some-repo")

    # Assert - failure logged via record, quota flag flips for subsequent calls
    assert repo_url == "https://github.com/some-org/some-repo"
    assert stars is None
    assert crawler._github_quota_blocked() is True

    # Subsequent calls short-circuit without hitting the API again
    respx.get("https://api.github.com/repos/other-org/other-repo").mock(
        return_value=httpx.Response(200, json={"stargazers_count": 10})
    )
    repo_skipped, stars_skipped = await crawler._fetch_stars("other-org/other-repo")
    assert stars_skipped is None
    await crawler.close()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stars_plain_404_does_not_disable_enrichment():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)
    respx.get("https://api.github.com/repos/missing/repo").mock(
        return_value=httpx.Response(404)
    )

    # Act
    repo_url, stars = await crawler._fetch_stars("missing/repo")

    # Assert - repo not found is normal, quota stays active
    assert stars is None
    assert crawler._github_quota_blocked() is False
    await crawler.close()

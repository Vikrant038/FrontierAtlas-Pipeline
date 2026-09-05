"""
Unit tests for StartupsCrawler multi-source acquisition.
Follows AAA pattern with offline respx mocking per CODING_STANDARDS.md.
"""

import pytest
import respx
import httpx
from unittest.mock import AsyncMock

from src.crawlers.startups_crawler import StartupsCrawler


@pytest.mark.asyncio
async def test_startups_crawler_canonical_seeds_ingestion():
    # Arrange
    crawler = StartupsCrawler(target_count=5)

    # Act
    startups = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(startups) == 5
    names = [s.content.entityName for s in startups]
    assert "OpenAI" in names
    assert "Anthropic" in names

    # Verify seed record has None employeeCount and honest source name
    openai_rec = next(s for s in startups if s.content.entityName == "OpenAI")
    assert openai_rec.content.data.employeeCount is None
    assert openai_rec.source.name == "Canonical Seed List (Internal)"


@pytest.mark.asyncio
@respx.mock
async def test_startups_crawler_yc_pagination_and_team_size():
    # Arrange
    crawler = StartupsCrawler(target_count=2)
    crawler.CANONICAL_SEEDS = []  # Bypass seeds to test YC endpoint

    yc_mock_data = {
        "companies": [
            {"name": "Anthropic PBC", "website": "https://anthropic.com", "teamSize": 800, "slug": "anthropic"},
            {"name": "OpenAI, Inc.", "website": "https://openai.com", "teamSize": 3500, "slug": "openai"},
        ]
    }
    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=1").mock(
        return_value=httpx.Response(200, json=yc_mock_data)
    )

    # Act
    startups = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(startups) == 2
    assert startups[0].content.entityName == "Anthropic"
    assert startups[0].content.data.employeeCount == 800
    assert startups[1].content.entityName == "OpenAI"
    assert startups[1].content.data.employeeCount == 3500


@pytest.mark.asyncio
@respx.mock
async def test_startups_crawler_hf_and_github_orgs():
    # Arrange
    crawler = StartupsCrawler(target_count=2)
    crawler.CANONICAL_SEEDS = []
    # Mock YC to return empty
    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=1").mock(
        return_value=httpx.Response(200, json={"companies": []})
    )
    # Mock Hugging Face models
    respx.get("https://huggingface.co/api/models?limit=1000").mock(
        return_value=httpx.Response(200, json=[
            {"id": "meta-llama/Llama-3.3-70B"},
            {"id": "mistralai/Mistral-Large"},
        ])
    )

    # Act
    startups = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(startups) == 2
    names = [s.content.entityName for s in startups]
    assert "meta-llama" in names
    assert "Mistral AI" in names


@pytest.mark.asyncio
@respx.mock
async def test_startups_crawler_yc_page_error_resilience():
    # Arrange: Page 1 yields 1 company, page 2 errors (500), page 3 yields 1 company
    crawler = StartupsCrawler(target_count=2)
    crawler.CANONICAL_SEEDS = []

    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=1").mock(
        return_value=httpx.Response(200, json={"companies": [{"name": "Startup Alpha", "website": "https://alpha.ai"}]})
    )
    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=2").mock(
        return_value=httpx.Response(500)
    )
    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=3").mock(
        return_value=httpx.Response(200, json={"companies": [{"name": "Startup Gamma", "website": "https://gamma.ai"}]})
    )
    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=4").mock(
        return_value=httpx.Response(200, json={"companies": []})
    )
    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=5").mock(
        return_value=httpx.Response(200, json={"companies": []})
    )

    # Act
    startups = await crawler.crawl()
    await crawler.close()

    # Assert: Page 2 500 did not abort the crawl; Startup Gamma on page 3 was collected
    assert len(startups) == 2
    names = [s.content.entityName for s in startups]
    assert "Startup Alpha" in names
    assert "Startup Gamma" in names


# ---------------------------------------------------------------------------
# Tail branch coverage: empty-name/full guards, empty YC page 1, GitHub quota
# exhaustion paths, HF fetch failure, and the crawl() orchestration.
# ---------------------------------------------------------------------------


from src.config import settings as startups_settings


@pytest.mark.asyncio
async def test_startups_add_startup_guards():
    crawler = StartupsCrawler(target_count=2)
    # None raw name -> False, nothing added (whitespace names are NOT guarded:
    # the resolver canonicalizes them, which is the documented behavior)
    assert await crawler._add_startup(None, "src", "https://x") is False
    assert crawler.collected == []
    # Full crawler returns True immediately
    crawler2 = StartupsCrawler(target_count=0)
    assert await crawler2._add_startup("Name", "src", "https://x") is True


@pytest.mark.asyncio
async def test_startups_yc_candidates_page1_empty_returns():
    crawler = StartupsCrawler(target_count=5)
    crawler._fetch_yc_page = AsyncMock(return_value=[])
    collected = [c async for c in crawler._yc_candidates()]
    assert collected == []


@pytest.mark.asyncio
async def test_startups_hf_fetch_failure_yields_nothing():
    crawler = StartupsCrawler(target_count=5)
    crawler.fetch_json = AsyncMock(side_effect=RuntimeError("hf down"))
    collected = [c async for c in crawler._hf_candidates()]
    assert collected == []
    crawler.fetch_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_startups_github_quota_exhausts_token_and_blocks(monkeypatch):
    crawler = StartupsCrawler(target_count=5)
    monkeypatch.setattr(crawler, "github_tokens", ["t1"])
    crawler.fetch_json = AsyncMock(side_effect=RuntimeError("HTTP 403: API rate limit exceeded"))

    collected = [c async for c in crawler._github_candidates()]
    assert collected == []
    assert crawler._exhausted_github_tokens == {"t1"}
    assert crawler._github_blocked() is True


@pytest.mark.asyncio
async def test_startups_github_non_quota_error_continues_and_empty_page_breaks(monkeypatch):
    crawler = StartupsCrawler(target_count=5)
    monkeypatch.setattr(startups_settings, "github_search_pages", 2)
    crawler.fetch_json = AsyncMock(side_effect=[
        RuntimeError("connection reset"),                      # non-quota -> continue
        {"items": [{"login": "AIOrg", "html_url": "https://github.com/AIOrg"}]},
        {"items": []},                                          # empty -> break out of pages
        {"items": [{"login": "SecondOrg"}]},
        {"items": []},
        {"items": []},
    ])

    collected = [c async for c in crawler._github_candidates()]
    assert ("AIOrg", "GitHub AI Orgs", "https://github.com/AIOrg", None) in collected
    assert crawler._exhausted_github_tokens == set()
    assert crawler._github_blocked() is False


@pytest.mark.asyncio
async def test_startups_crawl_orchestration():
    # Full-at-seeds path: the seeds loop breaks after the first seed reports full,
    # so the harvesters are never started and the slice is returned.
    crawler = StartupsCrawler(target_count=1)
    crawler._add_startup = AsyncMock(return_value=True)
    crawler._yc_candidates = lambda: _empty_gen()
    crawler._hf_candidates = lambda: _empty_gen()
    crawler._github_candidates = lambda: _empty_gen()
    records = await crawler.crawl()
    await crawler.close()
    assert records == []
    crawler._add_startup.assert_awaited_once()

    # Quota not reached by seeds: all three harvesters run concurrently
    crawler2 = StartupsCrawler(target_count=100)
    async def add_false(raw_name, src_name, src_url, employee_count=None):
        return False
    crawler2._add_startup = add_false
    crawler2._yc_candidates = lambda: _empty_gen()
    crawler2._hf_candidates = lambda: _empty_gen()
    crawler2._github_candidates = lambda: _empty_gen()
    records = await crawler2.crawl()
    await crawler2.close()
    assert records == []


async def _empty_gen():
    return
    yield  # pragma: no cover

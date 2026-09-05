"""
Unit tests for ResearchPapersCrawler and base crawler retry mechanisms.
Follows AAA pattern with offline respx mocking per CODING_STANDARDS.md.
"""

import asyncio
import time
from unittest.mock import AsyncMock

import feedparser
import httpx
import pytest
import respx

from src.crawlers.base import AsyncBaseCrawler
from src.crawlers.papers_crawler import ResearchPapersCrawler
from src.schemas.entities import ResearchPaperContent, ResearchPaperRecord

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>cs.AI updates on arXiv.org</title>
    <link>http://arxiv.org/rss/cs.AI</link>
    <description>cs.AI updates on the arXiv.org e-print archive.</description>
    <item>
      <title>Diffusion Models in Vision: A Survey. (arXiv:2409.99999v1 [cs.AI])</title>
      <link>https://arxiv.org/abs/2409.99999</link>
      <description>We present a comprehensive survey. Code is at https://github.com/survey-ai/diffusion-survey.</description>
      <dc:creator>Alice Smith, Bob Jones</dc:creator>
      <pubDate>Thu, 03 Sep 2026 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def test_parse_feed_entry_rss():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)
    feed = feedparser.parse(RSS_SAMPLE)

    # Act
    paper = crawler._parse_feed_entry(feed.entries[0])

    # Assert
    assert paper is not None
    assert paper["title"] == "Diffusion Models in Vision: A Survey."
    assert "Alice Smith" in paper["authors"]
    assert "Bob Jones" in paper["authors"]
    assert paper["paper_url"] == "https://arxiv.org/abs/2409.99999"
    assert paper["abstract_repo"] == "survey-ai/diffusion-survey"
    assert paper["published_date"].year == 2026


def test_parse_feed_entry_date_fail_skip():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)

    class MockEntry:
        title = "Paper With No Date"
        link = "https://arxiv.org/abs/2609.99999"
        published = ""
        updated = ""
        pubDate = ""

    # Act
    parsed = crawler._parse_feed_entry(MockEntry())

    # Assert
    assert parsed is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stars_direct_author_repo():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)
    respx.get("https://api.github.com/repos/survey-ai/diffusion-survey").mock(
        return_value=httpx.Response(200, json={"stargazers_count": 1250})
    )

    # Act
    repo_url, stars = await crawler._fetch_stars("survey-ai/diffusion-survey")
    await crawler.close()

    # Assert
    assert repo_url == "https://github.com/survey-ai/diffusion-survey"
    assert stars == 1250


@pytest.mark.asyncio
@respx.mock
async def test_fetch_stars_404_and_missing_repo():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)
    respx.get("https://api.github.com/repos/nonexistent/project").mock(
        return_value=httpx.Response(404)
    )

    # Act - 404 response
    repo_404, stars_404 = await crawler._fetch_stars("nonexistent/project")
    # Act - None path
    repo_none, stars_none = await crawler._fetch_stars(None)
    await crawler.close()

    # Assert
    assert repo_404 == "https://github.com/nonexistent/project"
    assert stars_404 is None
    assert repo_none is None
    assert stars_none is None


@pytest.mark.asyncio
@respx.mock
async def test_arxiv_cdn_failover_when_api_throttled():
    # Arrange
    respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(429))
    respx.get("https://rss.arxiv.org/rss/cs.AI").mock(return_value=httpx.Response(200, text=RSS_SAMPLE))
    respx.get("https://api.github.com/repos/survey-ai/diffusion-survey").mock(
        return_value=httpx.Response(200, json={"stargazers_count": 1250})
    )

    crawler = ResearchPapersCrawler(target_count=1)
    crawler._use_cdn = True

    # Act
    papers = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(papers) == 1
    assert papers[0].content.title == "Diffusion Models in Vision: A Survey."
    assert str(papers[0].content.github_url).rstrip("/") == "https://github.com/survey-ai/diffusion-survey"
    assert papers[0].content.github_stars == 1250


class DummyCrawler(AsyncBaseCrawler):
    async def crawl(self):
        return []


@pytest.mark.asyncio
@respx.mock
async def test_crawler_retry_on_429_with_retry_after():
    # Arrange
    test_url = "https://example.com/test-endpoint"
    crawler = DummyCrawler()
    route = respx.get(test_url)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0.01"}),
        httpx.Response(200, text="Success"),
    ]

    # Act
    content = await crawler.fetch(test_url)
    await crawler.close()

    # Assert
    assert content == "Success"
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_hf_daily_papers_failover():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)
    hf_data = [{
        "paper": {
            "title": "HF Paper on Transformers",
            "id": "2609.99998",
            "publishedAt": "2026-09-03T12:00:00Z",
            "authors": [{"name": "HF Scientist"}],
            "githubRepo": "https://github.com/huggingface/transformers",
        }
    }]
    respx.get("https://huggingface.co/api/daily_papers?limit=100").mock(
        return_value=httpx.Response(200, json=hf_data)
    )

    # Act
    papers = await crawler._query_hf_papers(limit=1)
    await crawler.close()

    # Assert
    assert len(papers) == 1
    assert papers[0]["title"] == "HF Paper on Transformers"
    assert papers[0]["paper_url"] == "https://arxiv.org/abs/2609.99998"
    assert papers[0]["abstract_repo"] == "huggingface/transformers"


@pytest.mark.asyncio
@respx.mock
async def test_openalex_papers_failover():
    # Arrange
    crawler = ResearchPapersCrawler(target_count=1)
    alex_data = {
        "results": [{
            "title": "OpenAlex AI Study",
            "primary_location": {"landing_page_url": "https://arxiv.org/abs/2609.99997"},
            "publication_date": "2026-09-03",
            "authorships": [{"author": {"display_name": "Dr. Alex"}}],
        }]
    }
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json=alex_data)
    )

    # Act
    papers = await crawler._query_openalex_papers(page=1, limit=1)
    await crawler.close()

    # Assert
    assert len(papers) == 1
    assert papers[0]["title"] == "OpenAlex AI Study"
    assert papers[0]["paper_url"] == "https://arxiv.org/abs/2609.99997"
    assert papers[0]["authors"] == ["Dr. Alex"]


@pytest.mark.asyncio
async def test_fetch_papers_batch_merges_concurrent_supplements_deduplicated():
    # Arrange: CDN returns two papers; HF returns a duplicate; OpenAlex fails outright
    crawler = ResearchPapersCrawler(target_count=1)
    crawler._use_cdn = True  # bypass the arXiv API primary path
    shared = {"title": "Shared", "authors": [], "paper_url": "https://arxiv.org/abs/2609.99990",
              "published_date": "2026-09-03", "abstract_repo": None}
    second = {"title": "Second", "authors": [], "paper_url": "https://arxiv.org/abs/2609.99991",
              "published_date": "2026-09-03", "abstract_repo": None}
    crawler._query_arxiv_cdn = AsyncMock(return_value=[shared, second])
    crawler._query_hf_papers = AsyncMock(return_value=[shared])
    crawler._query_openalex_papers = AsyncMock(side_effect=RuntimeError("OpenAlex down"))

    # Act: supplements gathered concurrently, merged in CDN -> HF priority
    batch = await crawler.fetch_papers_batch(0, 10)
    await crawler.close()

    # Assert: deduplicated, priority-ordered, failing source tolerated
    assert [p["paper_url"] for p in batch] == ["https://arxiv.org/abs/2609.99990", "https://arxiv.org/abs/2609.99991"]
    crawler._query_arxiv_cdn.assert_awaited_once()
    crawler._query_hf_papers.assert_awaited_once()
    crawler._query_openalex_papers.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_enrichment_prunes_finished_tasks_at_cap():
    # Arrange: 19 completed enrichment tasks, so the next spawn crosses the 20-in-flight cap
    crawler = ResearchPapersCrawler(target_count=1)
    crawler._enrich_batch = AsyncMock()
    done_tasks = [asyncio.create_task(crawler._enrich_batch([])) for _ in range(19)]
    await asyncio.gather(*done_tasks)

    # Act: the 20th spawn crosses the cap while its task is still in flight (slow real coroutine)
    async def slow_enrich(batch):
        await asyncio.sleep(0.2)

    crawler._enrich_batch = slow_enrich
    surviving = await crawler._spawn_enrichment([("rec", "repo")], list(done_tasks))
    await asyncio.gather(*surviving)
    await crawler.close()

    # Assert: the 19 finished tasks are pruned; the in-flight one survives
    assert len(surviving) == 1
    assert surviving[0] not in done_tasks


@pytest.mark.asyncio
async def test_spawn_enrichment_skipped_when_quota_blocked():
    # Arrange: quota backoff active, so no enrichment task may start
    crawler = ResearchPapersCrawler(target_count=1)
    crawler._github_quota_blocked_until = time.monotonic() + 3600.0

    # Act
    surviving = await crawler._spawn_enrichment([("rec", "repo")], [])
    await crawler.close()

    # Assert: no task was started while the quota backoff is active
    assert surviving == []


# ---------------------------------------------------------------------------
# Tail branch coverage: feed-entry edge shapes, quota paths, pagination, and
# the crawl loop itself.
# ---------------------------------------------------------------------------

from types import SimpleNamespace as _NS

from src.crawlers.base import BotBlockedError
from src.config import settings as papers_settings


@pytest.mark.parametrize(
    "entry, expect_none",
    [
        # Empty title -> skipped outright
        (_NS(title="  ", link="https://arxiv.org/abs/1", published="2026-09-03"), True),
        # Authors given as author objects with a name attribute
        (_NS(title="T", authors=[_NS(name="Ada Lovelace")], link="https://arxiv.org/abs/2", published="2026-09-03"), False),
        # No authors, no creator, no date -> skipped
        (_NS(title="T", link="https://arxiv.org/abs/3"), True),
        # http:// link upgraded to https://
        (_NS(title="T", creator="Grace Hopper", link="http://arxiv.org/abs/4", published="2026-09-03"), False),
        # dc_creator fallback with HTML tags stripped
        (_NS(title="T", dc_creator="<b>Alan Turing</b>", link="https://arxiv.org/abs/5", published="2026-09-03"), False),
        # Semicolon-separated creator names
        (_NS(title="T", creator="One; Two; Three", link="https://arxiv.org/abs/6", published="2026-09-03"), False),
    ],
)
def test_parse_feed_entry_variant_shapes(entry, expect_none):
    crawler = ResearchPapersCrawler(target_count=1)
    paper = crawler._parse_feed_entry(entry)
    assert (paper is None) is expect_none


def test_parse_feed_entry_repo_and_pdf_link_edge_cases():
    crawler = ResearchPapersCrawler(target_count=1)
    # Repository ending in .git is normalized; punctuation around the repo is stripped
    entry = _NS(
        title="T", creator="A B", link="https://arxiv.org/abs/7", published="2026-09-03",
        summary="code: https://github.com/acme/repo.git.)",
    )
    paper = crawler._parse_feed_entry(entry)
    assert paper["abstract_repo"] == "acme/repo"
    # application/pdf link overrides the abstract page URL
    pdf_entry = _NS(
        title="T2", creator="C D", link="https://arxiv.org/abs/8", published="2026-09-03",
        summary="",
        links=[_NS(type="text/html", href="https://arxiv.org/abs/8"), _NS(type="application/pdf", href="https://arxiv.org/pdf/8")],
    )
    paper = crawler._parse_feed_entry(pdf_entry)
    assert paper["paper_url"] == "https://arxiv.org/pdf/8"


def test_paper_record_empty_authors_default():
    from src.crawlers.papers_crawler import _paper_record

    rec = _paper_record("T", [], "https://arxiv.org/abs/9", "2026-09-03", None)
    assert rec["authors"] == ["AI Researcher"]


def test_collect_until_limit_filters_and_dedupes():
    crawler = ResearchPapersCrawler(target_count=5)
    p1 = {"paper_url": "https://arxiv.org/abs/1"}
    p2 = {"paper_url": "https://arxiv.org/abs/2"}
    # None candidate filtered; p1 duplicate filtered by is_seen; breaks at the limit
    collected = crawler._collect_until_limit([None, p1, p1, p2, {"paper_url": "https://arxiv.org/abs/3"}], 2)
    assert [p["paper_url"] for p in collected] == ["https://arxiv.org/abs/1", "https://arxiv.org/abs/2"]


@pytest.mark.asyncio
async def test_fetch_stars_anonymous_budget_and_backoff(monkeypatch):
    crawler = ResearchPapersCrawler(target_count=5)
    monkeypatch.setattr(papers_settings, "github_anonymous_lookup_budget", 1)
    monkeypatch.setattr(crawler, "github_tokens", [])

    # First anonymous lookup is allowed and increments the counter
    crawler.fetch_json = AsyncMock(return_value={"stargazers_count": 10})
    url, stars = await crawler._fetch_stars("acme/repo")
    assert stars == 10
    assert crawler._anonymous_lookups == 1

    # Second lookup exceeds the budget: enrichment pauses for an hour, no request made
    url, stars = await crawler._fetch_stars("acme/repo2")
    assert stars is None
    assert crawler._github_quota_blocked() is True
    assert crawler.fetch_json.await_count == 1


@pytest.mark.asyncio
async def test_fetch_stars_403_quota_vs_plain_and_generic_quota(monkeypatch):
    crawler = ResearchPapersCrawler(target_count=5)
    monkeypatch.setattr(crawler, "github_tokens", ["t1"])
    crawler._pace_github = AsyncMock()

    # 403 carrying rate-limit language -> token exhausted and enrichment disabled
    crawler.fetch_json = AsyncMock(side_effect=BotBlockedError("HTTP 403 for ...: API rate limit exceeded"))
    await crawler._fetch_stars("acme/repo")
    assert "t1" in crawler._exhausted_github_tokens
    assert crawler._github_quota_blocked() is True

    # Plain 403 (blocked repo) -> logged, token NOT dropped, enrichment stays live
    crawler2 = ResearchPapersCrawler(target_count=5)
    monkeypatch.setattr(crawler2, "github_tokens", ["t2"])
    crawler2.fetch_json = AsyncMock(side_effect=BotBlockedError("HTTP 403 for ...: repository access blocked"))
    await crawler2._fetch_stars("acme/repo")
    assert crawler2._exhausted_github_tokens == set()
    assert crawler2._github_quota_blocked() is False

    # Generic (non-BotBlocked) exception carrying quota language -> token dropped
    crawler3 = ResearchPapersCrawler(target_count=5)
    monkeypatch.setattr(crawler3, "github_tokens", ["t3"])
    crawler3.fetch_json = AsyncMock(side_effect=RuntimeError("403 quota exceeded"))
    await crawler3._fetch_stars("acme/repo")
    assert "t3" in crawler3._exhausted_github_tokens


@pytest.mark.asyncio
async def test_query_arxiv_api_success_path():
    crawler = ResearchPapersCrawler(target_count=1)
    crawler.fetch = AsyncMock(return_value=RSS_SAMPLE)
    papers = await crawler._query_arxiv_api(0, 1)
    assert len(papers) == 1
    assert papers[0]["title"] == "Diffusion Models in Vision: A Survey."
    await crawler.close()


@pytest.mark.asyncio
async def test_fetch_papers_batch_arxiv_primary_and_failover(monkeypatch):
    # Primary arXiv API returns papers -> immediate early return, no supplements
    crawler = ResearchPapersCrawler(target_count=2)
    paper = {"title": "P", "authors": [], "paper_url": "https://arxiv.org/abs/1",
             "published_date": "2026-09-03", "abstract_repo": None}
    crawler._query_arxiv_api = AsyncMock(return_value=[paper])
    batch = await crawler.fetch_papers_batch(0, 1)
    assert batch == [paper]
    assert crawler._use_cdn is False

    # arXiv API raises -> CDN flag flips and supplements still produce the batch
    crawler2 = ResearchPapersCrawler(target_count=2)
    crawler2._query_arxiv_api = AsyncMock(side_effect=RuntimeError("arxiv down"))
    crawler2._query_arxiv_cdn = AsyncMock(return_value=[])
    crawler2._query_hf_papers = AsyncMock(return_value=[paper])
    crawler2._query_openalex_papers = AsyncMock(return_value=[])
    batch = await crawler2.fetch_papers_batch(0, 1)
    assert batch == [paper]
    assert crawler2._use_cdn is True


@pytest.mark.asyncio
async def test_query_arxiv_cdn_empty_and_failing_categories(monkeypatch):
    crawler = ResearchPapersCrawler(target_count=5)
    # No configured categories -> empty result
    monkeypatch.setattr(papers_settings, "arxiv_cdn_categories", "")
    assert await crawler._query_arxiv_cdn(5) == []

    # One category raises; remaining categories fill the limit
    monkeypatch.setattr(papers_settings, "arxiv_cdn_categories", "cs.AI,cs.LG")
    paper = {"title": "P", "authors": [], "paper_url": "https://arxiv.org/abs/1",
             "published_date": "2026-09-03", "abstract_repo": None}
    crawler._query_cdn_category = AsyncMock(
        side_effect=[RuntimeError("rss down"), [paper, dict(paper, paper_url="https://arxiv.org/abs/2")]]
    )
    result = await crawler._query_arxiv_cdn(2)
    assert [p["paper_url"] for p in result] == ["https://arxiv.org/abs/1", "https://arxiv.org/abs/2"]
    await crawler.close()


@pytest.mark.asyncio
async def test_query_hf_papers_skips_incomplete_items():
    crawler = ResearchPapersCrawler(target_count=5)
    items = [
        {"paper": {"title": "", "id": "1", "publishedAt": "2026-09-03"}},          # no title
        {"paper": {"title": "T", "id": "2"}},                                      # no date
        {"paper": {"title": "T2", "publishedAt": "2026-09-03"}},                  # no id
        {"paper": {
            "title": "T3", "id": "2406.00001", "publishedAt": "2026-09-03",
            "githubRepo": "https://github.com/acme/repo",
            "authors": [{"name": "A"}, {}, {"name": "B"}],
        }},
    ]
    crawler.fetch_json = AsyncMock(return_value=items)
    papers = await crawler._query_hf_papers(5)
    assert len(papers) == 1
    assert papers[0]["abstract_repo"] == "acme/repo"
    assert papers[0]["authors"] == ["A", "B"]
    await crawler.close()


@pytest.mark.asyncio
async def test_query_openalex_papers_mailto_and_skips(monkeypatch):
    crawler = ResearchPapersCrawler(target_count=5)
    monkeypatch.setattr(papers_settings, "openalex_email", "researcher@example.com")
    captured = {}

    async def fake_fetch_json(url, params=None, **kwargs):
        captured["params"] = params or {}
        return {"results": [
            {"title": "", "publication_date": "2026-09-03"},                            # no title
            {"title": "Good Paper", "publication_date": "2026-09-03"},                  # no location
            {"title": "Good Paper 2", "publication_date": "2026-09-03"},                # no date
            {
                "title": "Paper With Repo https://github.com/acme/alex",
                "primary_location": {"landing_page_url": "https://arxiv.org/abs/2406.1"},
                "publication_date": "2026-09-03",
                "authorships": [{"author": {"display_name": "A"}}, {"author": {}}],
            },
        ]}

    crawler.fetch_json = fake_fetch_json
    papers = await crawler._query_openalex_papers(1, 5)
    assert captured["params"]["mailto"] == "researcher@example.com"
    assert len(papers) == 1
    assert papers[0]["abstract_repo"] == "acme/alex"
    assert papers[0]["authors"] == ["A"]
    await crawler.close()


@pytest.mark.asyncio
async def test_fetch_papers_batch_dedupe_url_and_falsy_url(monkeypatch):
    # The supplement merge drops duplicate URLs and tolerates candidates with no URL
    crawler = ResearchPapersCrawler(target_count=5)
    crawler._use_cdn = True
    a = {"title": "A", "authors": [], "paper_url": "https://arxiv.org/abs/1", "published_date": "2026-09-03", "abstract_repo": None}
    crawler._query_arxiv_cdn = AsyncMock(return_value=[a, dict(a, title="A dup"), {"title": "No URL", "authors": [], "paper_url": "", "published_date": "2026-09-03", "abstract_repo": None}])
    crawler._query_hf_papers = AsyncMock(return_value=[])
    crawler._query_openalex_papers = AsyncMock(return_value=[])
    batch = await crawler.fetch_papers_batch(0, 5)
    urls = [p["paper_url"] for p in batch]
    assert urls.count("https://arxiv.org/abs/1") == 1
    assert "" in urls  # falsy-URL candidate appended without dedup registration
    await crawler.close()


@pytest.mark.asyncio
async def test_crawl_loop_full_target_with_enrichment(monkeypatch):
    # Full crawl: batches until the target is met, background enrichment runs and is
    # awaited at the end, WAL truncation and the target slice are applied.
    crawler = ResearchPapersCrawler(target_count=2)
    paper = {
        "title": "Paper A", "authors": ["A"], "paper_url": "https://arxiv.org/abs/1",
        "published_date": "2026-09-03", "abstract_repo": "acme/repo",
    }
    paper2 = dict(paper, paper_url="https://arxiv.org/abs/2")
    crawler.fetch_papers_batch = AsyncMock(side_effect=[[paper], [paper2]])
    crawler._fetch_stars = AsyncMock(return_value=("https://github.com/acme/repo", 42))
    crawler._enrich_batch = AsyncMock()  # no-op so spawned tasks complete instantly

    records = await crawler.crawl()
    await crawler.close()

    assert len(records) == 2
    assert crawler.fetch_papers_batch.await_count == 2


@pytest.mark.asyncio
async def test_crawl_loop_breaks_on_empty_batch():
    crawler = ResearchPapersCrawler(target_count=10)
    crawler.fetch_papers_batch = AsyncMock(return_value=[])
    records = await crawler.crawl()
    await crawler.close()
    assert records == []


@pytest.mark.asyncio
async def test_papers_enrichment_counters_and_state():
    crawler = ResearchPapersCrawler(target_count=2)
    assert crawler.enriched_count == 0
    assert crawler.enrich_total == 0
    assert not crawler.is_enriching

    rec1 = ResearchPaperRecord(
        content=ResearchPaperContent(
            title="Paper One", authors=["A"], paper_url="https://arxiv.org/abs/1", published_date="2026-09-03"
        )
    )
    rec2 = ResearchPaperRecord(
        content=ResearchPaperContent(
            title="Paper Two", authors=["B"], paper_url="https://arxiv.org/abs/2", published_date="2026-09-03"
        )
    )
    batch = [(rec1, "org/repo1"), (rec2, "org/repo2")]

    crawler._fetch_stars = AsyncMock(return_value=("https://github.com/org/repo", 99))
    tasks = await crawler._spawn_enrichment(batch, [])
    assert crawler.enrich_total == 2
    assert crawler.is_enriching

    await asyncio.gather(*tasks)
    assert crawler.enriched_count == 2
    assert not crawler.is_enriching
    await crawler.close()


@pytest.mark.asyncio
async def test_papers_enrichment_counters_advance_on_quota_block():
    crawler = ResearchPapersCrawler(target_count=2)
    crawler._disable_github_enrichment()
    assert crawler._github_quota_blocked()

    rec = ResearchPaperRecord(
        content=ResearchPaperContent(
            title="Paper One", authors=["A"], paper_url="https://arxiv.org/abs/1", published_date="2026-09-03"
        )
    )
    batch = [(rec, "org/repo1"), (rec, "org/repo2")]
    tasks = await crawler._spawn_enrichment(batch, [])
    assert len(tasks) == 0
    assert crawler.enrich_total == 2
    assert crawler.enriched_count == 2
    assert not crawler.is_enriching
    await crawler.close()

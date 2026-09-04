"""
Unit tests for ResearchPapersCrawler and base crawler retry mechanisms.
Follows AAA pattern with offline respx mocking per CODING_STANDARDS.md.
"""

import feedparser
import httpx
import pytest
import respx

from src.crawlers.base import AsyncBaseCrawler
from src.crawlers.papers_crawler import ResearchPapersCrawler

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

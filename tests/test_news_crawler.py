"""
Unit tests for NewsCrawler freshness filtering, URL deduplication, and article extraction.
Follows AAA pattern with offline respx mocking and freezegun per CODING_STANDARDS.md.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
import httpx
from freezegun import freeze_time

from src.crawlers.news_crawler import NewsCrawler

ARTICLE_HTML = """<html>
<head><title>AI Breakthrough</title></head>
<body>
<main>
<article>
<h1>Frontier Model Breakthrough</h1>
<p>Today researchers announced a significant breakthrough in frontier artificial intelligence reasoning models that demonstrate state of the art performance across all major industry benchmarks.</p>
<p>The new architecture leverages advanced self-reflection and multi-turn planning algorithms to solve complex programming and scientific reasoning problems with unprecedented accuracy and robustness.</p>
</article>
</main>
</body>
</html>
"""


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_crawler_freshness_filtering():
    # Arrange
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>AI News Feed</title>
    <item>
      <title>Frontier Model Breakthrough</title>
      <link>https://example.com/fresh-news</link>
      <pubDate>Fri, 04 Sep 2026 11:00:00 GMT</pubDate>
      <description>Exciting new intelligence milestone.</description>
    </item>
    <item>
      <title>Old News from Last Week</title>
      <link>https://example.com/stale-news</link>
      <pubDate>Mon, 25 Aug 2026 00:00:00 GMT</pubDate>
      <description>Outdated article.</description>
    </item>
  </channel>
</rss>
"""
    crawler = NewsCrawler(sources=[{"name": "Test Feed", "feed_url": "https://example.com/feed.xml"}])
    respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=rss_xml))
    respx.get("https://example.com/fresh-news").mock(return_value=httpx.Response(200, text=ARTICLE_HTML))

    # Act
    articles = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(articles) == 1
    assert articles[0].content.title == "Frontier Model Breakthrough"
    assert articles[0].source.url == "https://example.com/fresh-news"
    assert "frontier" in articles[0].content.full_text.lower()


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_crawler_url_deduplication():
    # Arrange
    rss_dup_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Duplicate Feed</title>
    <item>
      <title>Article 1</title>
      <link>https://example.com/dup-article</link>
      <pubDate>Fri, 04 Sep 2026 11:00:00 GMT</pubDate>
      <description>First appearance.</description>
    </item>
    <item>
      <title>Article 1 Duplicate</title>
      <link>https://example.com/dup-article</link>
      <pubDate>Fri, 04 Sep 2026 11:30:00 GMT</pubDate>
      <description>Second appearance.</description>
    </item>
  </channel>
</rss>
"""
    crawler = NewsCrawler(sources=[{"name": "Dup Feed", "feed_url": "https://example.com/dup.xml"}])
    respx.get("https://example.com/dup.xml").mock(return_value=httpx.Response(200, text=rss_dup_xml))
    respx.get("https://example.com/dup-article").mock(return_value=httpx.Response(200, text=ARTICLE_HTML))

    # Act
    articles = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(articles) == 1
    assert articles[0].source.url == "https://example.com/dup-article"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_crawler_cross_source_url_normalization():
    # Arrange
    rss_feed_1 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Feed 1</title>
<item>
  <title>DeepSeek Breakthrough Released</title>
  <link>https://example.com/deepseek-story/?utm_source=feed&amp;utm_medium=rss&amp;ref=hn</link>
  <pubDate>Fri, 04 Sep 2026 11:00:00 GMT</pubDate>
  <description>DeepSeek reasoning story.</description>
</item>
</channel></rss>"""

    rss_feed_2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Feed 2</title>
<item>
  <title>DeepSeek Story Repost</title>
  <link>https://example.com/deepseek-story/</link>
  <pubDate>Fri, 04 Sep 2026 11:15:00 GMT</pubDate>
  <description>Same story without UTM params.</description>
</item>
</channel></rss>"""

    crawler = NewsCrawler(sources=[
        {"name": "Feed 1", "feed_url": "https://example.com/f1.xml"},
        {"name": "Feed 2", "feed_url": "https://example.com/f2.xml"},
    ])
    respx.get("https://example.com/f1.xml").mock(return_value=httpx.Response(200, text=rss_feed_1))
    respx.get("https://example.com/f2.xml").mock(return_value=httpx.Response(200, text=rss_feed_2))
    respx.get("https://example.com/deepseek-story/?utm_source=feed&utm_medium=rss&ref=hn").mock(
        return_value=httpx.Response(200, text=ARTICLE_HTML)
    )
    respx.get("https://example.com/deepseek-story/").mock(
        return_value=httpx.Response(200, text=ARTICLE_HTML)
    )

    # Act
    articles = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(articles) == 1
    assert "deepseek" in articles[0].source.url


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_crawler_title_fuzzy_deduplication():
    # Arrange
    rss_f1 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>TechCrunch Feed</title>
<item>
  <title>Nvidia Acquires Hugging Face for $13 Billion</title>
  <link>https://techcrunch.com/story-1</link>
  <pubDate>Fri, 04 Sep 2026 11:00:00 GMT</pubDate>
  <description>Nvidia announced acquisition.</description>
</item>
</channel></rss>"""

    rss_f2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Verge Feed</title>
<item>
  <title>Nvidia Acquires Hugging Face for $13 Billion - TechCrunch</title>
  <link>https://theverge.com/story-2</link>
  <pubDate>Fri, 04 Sep 2026 11:20:00 GMT</pubDate>
  <description>Verge coverage of same announcement.</description>
</item>
</channel></rss>"""

    crawler = NewsCrawler(sources=[
        {"name": "TC", "feed_url": "https://techcrunch.com/feed.xml"},
        {"name": "Verge", "feed_url": "https://theverge.com/feed.xml"},
    ])
    respx.get("https://techcrunch.com/feed.xml").mock(return_value=httpx.Response(200, text=rss_f1))
    respx.get("https://theverge.com/feed.xml").mock(return_value=httpx.Response(200, text=rss_f2))
    respx.get("https://techcrunch.com/story-1").mock(return_value=httpx.Response(200, text=ARTICLE_HTML))
    respx.get("https://theverge.com/story-2").mock(return_value=httpx.Response(200, text=ARTICLE_HTML))

    # Act
    articles = await crawler.crawl()
    await crawler.close()

    # Assert - second article dropped due to rapidfuzz token_sort_ratio >= 90
    assert len(articles) == 1
    assert articles[0].content.title == "Nvidia Acquires Hugging Face for $13 Billion"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_crawler_title_fuzzy_dedup_paren_publisher_suffix():
    # Arrange - same story where one feed wraps publisher in parens (both dedupe layers miss 85% raw score)
    rss_f1 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>TechCrunch Feed</title>
<item>
  <title>Nvidia acquires Hugging Face</title>
  <link>https://techcrunch.com/story-p</link>
  <pubDate>Fri, 04 Sep 2026 11:00:00 GMT</pubDate>
  <description>Nvidia announced acquisition.</description>
</item>
</channel></rss>"""

    rss_f2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Verge Feed</title>
<item>
  <title>Nvidia acquires Hugging Face (The Verge)</title>
  <link>https://theverge.com/story-p</link>
  <pubDate>Fri, 04 Sep 2026 11:20:00 GMT</pubDate>
  <description>Verge coverage of same announcement.</description>
</item>
</channel></rss>"""

    crawler = NewsCrawler(sources=[
        {"name": "TC", "feed_url": "https://techcrunch.com/feed.xml"},
        {"name": "Verge", "feed_url": "https://theverge.com/feed.xml"},
    ])
    respx.get("https://techcrunch.com/feed.xml").mock(return_value=httpx.Response(200, text=rss_f1))
    respx.get("https://theverge.com/feed.xml").mock(return_value=httpx.Response(200, text=rss_f2))
    respx.get("https://techcrunch.com/story-p").mock(return_value=httpx.Response(200, text=ARTICLE_HTML))
    respx.get("https://theverge.com/story-p").mock(return_value=httpx.Response(200, text=ARTICLE_HTML))

    # Act
    articles = await crawler.crawl()
    await crawler.close()

    # Assert - paren-wrapped publisher suffix stripped before fuzzy match; short-title
    # variant scores 85% raw (below 90 cutoff) and only dedupes after suffix strip
    assert len(articles) == 1
    assert articles[0].content.title == "Nvidia acquires Hugging Face"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_crawler_full_text_fallback_quality():
    # Arrange
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Fallback Feed</title>
<item>
  <title>Secret AI Lab Launches Stealth Product</title>
  <link>https://example.com/paywalled-article</link>
  <pubDate>Fri, 04 Sep 2026 11:00:00 GMT</pubDate>
  <description>Detailed summary of the stealth lab product announcement that exceeds twenty characters.</description>
</item>
</channel></rss>"""

    crawler = NewsCrawler(sources=[{"name": "Paywalled Feed", "feed_url": "https://example.com/feed.xml"}])
    respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=rss_xml))
    # Article fetch fails with 404 (non-retryable client error)
    respx.get("https://example.com/paywalled-article").mock(return_value=httpx.Response(404))

    # Act
    articles = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(articles) == 1
    # Full text fell back to summary
    assert "Detailed summary" in articles[0].content.full_text
    assert len(articles[0].content.full_text) >= 20
    assert crawler.stats["Paywalled Feed"]["total"] == 1
    assert crawler.stats["Paywalled Feed"]["full_text"] == 0


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_crawler_stale_feed_date_rejected_not_novelty_stamped():
    # Arrange - feed states a date weeks old; all heuristics miss; novelty must NOT rescue.
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Stale Feed</title>
<item>
  <title>Month Old Story Without Metadata</title>
  <link>https://example.com/month-old-story</link>
  <pubDate>Tue, 11 Aug 2026 10:00:00 GMT</pubDate>
  <description>An old story with plain description text lacking any relative recency phrases.</description>
</item>
</channel></rss>"""
    crawler = NewsCrawler(sources=[{"name": "Stale Feed", "feed_url": "https://example.com/stale.xml"}])
    respx.get("https://example.com/stale.xml").mock(return_value=httpx.Response(200, text=rss_xml))
    # Article body with no date metadata and no freshness phrases
    respx.get("https://example.com/month-old-story").mock(
        return_value=httpx.Response(200, text="<html><body><p>Plain archived content paragraph with sufficient length for the schema validation requirements.</p></body></html>")
    )

    # Act
    articles = await crawler.crawl()
    await crawler.close()

    # Assert - stale source date strictly rejected, novelty stamp must not override
    assert articles == []


def test_extract_hn_article_url_returns_external_link():
    """Arrange / Act / Assert: _extract_hn_article_url extracts the real article URL from HN RSS HTML."""
    # Arrange
    hn_description = (
        '<p>Article URL: <a href="https://videm.ai/">https://videm.ai/</a></p>\n'
        '<p>Comments URL: <a href="https://news.ycombinator.com/item?id=123">...</a></p>\n'
        '<p>Points: 5</p>\n'
        '<p># Comments: 3</p>'
    )

    # Act
    result = NewsCrawler._extract_hn_article_url(hn_description)

    # Assert — external article URL returned, HN link excluded
    assert result == "https://videm.ai/"


def test_extract_hn_article_url_excludes_hn_links():
    """_extract_hn_article_url must not return news.ycombinator.com URLs."""
    # Arrange — description with only an HN self-link (no external article URL)
    hn_only_description = '<p>Comments URL: <a href="https://news.ycombinator.com/item?id=456">...</a></p>'

    # Act
    result = NewsCrawler._extract_hn_article_url(hn_only_description)

    # Assert
    assert result is None


def test_extract_hn_article_url_handles_empty_input():
    """_extract_hn_article_url returns None for empty / None input without raising."""
    assert NewsCrawler._extract_hn_article_url("") is None
    assert NewsCrawler._extract_hn_article_url(None) is None


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_hacker_news_summary_never_contains_html_tags():
    """
    Regression guard: HN items must never store raw '<p>Article URL:...</p>' HTML in the summary field.
    When the linked external article fetch fails, summary must fall back to the title string (plain text).
    """
    # Arrange — HN RSS item with the characteristic HTML description blob
    # hnrss.org format: <link> = external article URL, <description> = HTML blob with Article/Comments URLs
    hn_rss = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>HN AI Feed</title>
    <item>
      <title>Show HN: Open-Source AI Terminal</title>
      <link>https://example.com/ai-terminal</link>
      <pubDate>Thu, 04 Sep 2026 11:00:00 GMT</pubDate>
      <description>&lt;p&gt;Article URL: &lt;a href="https://example.com/ai-terminal"&gt;https://example.com/ai-terminal&lt;/a&gt;&lt;/p&gt;
&lt;p&gt;Comments URL: &lt;a href="https://news.ycombinator.com/item?id=99999"&gt;...&lt;/a&gt;&lt;/p&gt;
&lt;p&gt;Points: 3&lt;/p&gt;
&lt;p&gt;# Comments: 1&lt;/p&gt;</description>
    </item>
  </channel>
</rss>"""

    crawler = NewsCrawler(sources=[{"name": "Hacker News AI", "feed_url": "https://hnrss.org/newest?q=AI"}])
    # Mock the HN RSS feed
    respx.get("https://hnrss.org/newest?q=AI").mock(return_value=httpx.Response(200, text=hn_rss))
    # Mock the external article fetch → fails (site unavailable)
    respx.get("https://example.com/ai-terminal").mock(return_value=httpx.Response(503))


    # Act
    articles = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(articles) == 1
    rec = articles[0]
    summary = rec.content.summary or ""
    # Core regression guard: no raw HTML tags in the summary field
    assert "<p>" not in summary, f"Raw HTML tag found in summary: {summary!r}"
    assert "<a " not in summary, f"Raw HTML anchor tag found in summary: {summary!r}"
    assert "Article URL:" not in summary, f"HN RSS metadata found in summary: {summary!r}"
    # Summary must be non-empty meaningful text (at minimum the title)
    assert len(summary) >= 5


@pytest.mark.asyncio
async def test_news_finalize_entry_rolls_back_stale_and_builds_fresh():
    # Arrange
    crawler = NewsCrawler()
    crawler.stats = {"Test Feed": {"total": 0, "full_text": 0, "llm_summary": 0, "rss_fallback": 0}}
    crawler._build_summary = AsyncMock(return_value=("A sufficiently long summary text.", False))
    crawler._rollback_seen = MagicMock()
    stale_iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

    # Act (stale): a source-stated date that fails the 24h gate must be rejected
    stale_record = await crawler._finalize_entry(
        "Test Feed", None, "Old News", "https://example.com/stale", "", False, "",
        stale_iso, None, "https://example.com/stale", "old-news",
    )

    # Assert (stale): rejected, dedup reservations rolled back, no record built
    assert stale_record is None
    crawler._rollback_seen.assert_called_once_with("https://example.com/stale", "old-news")

    # Act (fresh): a truly dateless, unseen entry is novelty-stamped and built
    fresh_record = await crawler._finalize_entry(
        "Test Feed", None, "Brand New Announcement", "https://example.com/fresh", "", False, "",
        None, None, "https://example.com/fresh", "brand-new-announcement",
    )

    # Assert (fresh): record built, stamped within the last minute, stats + summary recorded
    assert fresh_record is not None
    age = datetime.now(timezone.utc) - fresh_record.content.published_date
    assert age.total_seconds() < 60
    assert fresh_record.content.summary == "A sufficiently long summary text."
    assert crawler.stats["Test Feed"]["total"] == 1
    assert crawler.stats["Test Feed"]["rss_fallback"] == 1


# ---------------------------------------------------------------------------
# Coverage for the remaining branches: source override, URL normalization
# edges, HN article full-text path, summary/date resolution, and crawl tails.
# ---------------------------------------------------------------------------

from types import SimpleNamespace as _NS

from src.config import settings
from src.crawlers.news_crawler import NEWS_SOURCES
from src.llm.fallback_chain import llm_engine as news_llm_engine
from src.llm.prompts import NewsSummarySchema
from src.schemas.entities import NewsContent, NewsRecord, SourceMetadata


def test_news_configured_sources_json_override(monkeypatch):
    # A valid override replaces the built-in feed list entirely
    monkeypatch.setattr(
        settings, "news_sources_json",
        '[{"name": "Custom Feed", "feed_url": "https://custom.example/rss"}, {"name": "No Url"}]',
    )
    sources = NewsCrawler._configured_sources()
    assert sources == [{"name": "Custom Feed", "feed_url": "https://custom.example/rss"}]

    # Empty / unparseable overrides fall back to the built-ins
    monkeypatch.setattr(settings, "news_sources_json", "")
    assert NewsCrawler._configured_sources() is NEWS_SOURCES
    monkeypatch.setattr(settings, "news_sources_json", "{not json")
    assert NewsCrawler._configured_sources() is NEWS_SOURCES


def test_news_normalize_url_edge_cases():
    norm = NewsCrawler._normalize_url
    assert norm("") == ""
    assert norm(None) == ""
    assert norm(123) == ""
    # Default-port stripping + param filtering + trailing-slash removal
    assert norm("HTTPS://Example.com:443/path/?utm_source=x&ref=y#frag") == "https://example.com/path"
    assert norm("http://example.com:80/") == "http://example.com/"
    # Root path preserved
    assert norm("https://example.com") == "https://example.com/"
    # Kept params survive
    assert norm("https://example.com/a?q=1&utm_campaign=z") == "https://example.com/a?q=1"


def test_news_normalize_title_non_string():
    assert NewsCrawler._normalize_title("") == ""
    assert NewsCrawler._normalize_title(None) == ""
    assert NewsCrawler._normalize_title(42) == ""


def test_extract_hn_article_url_rejects_javascript_targets():
    # Only a javascript: href exists -> rejected -> None (not a usable article URL)
    html = '<p><a href="javascript:void(0)">click</a></p>'
    assert NewsCrawler._extract_hn_article_url(html) is None
    # Real external URL is still found when present alongside it
    mixed = '<p><a href="javascript:void(0)">x</a><a href="https://arxiv.org/abs/2406.1">y</a></p>'
    assert NewsCrawler._extract_hn_article_url(mixed) == "https://arxiv.org/abs/2406.1"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_news_rollback_seen_removes_reserved_keys():
    crawler = NewsCrawler()
    crawler._seen_urls = {"https://example.com/a"}
    crawler._seen_titles = ["title a"]
    crawler._rollback_seen("https://example.com/a", "title a")
    assert crawler._seen_urls == set()
    assert crawler._seen_titles == []
    # Missing title is a no-op
    crawler._rollback_seen("https://example.com/a", "never added")


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_news_build_summary_llm_success_and_failure(monkeypatch):
    crawler = NewsCrawler(sources=[{"name": "TechCrunch AI", "feed_url": "https://x/feed"}])
    entry = _NS(summary="<p>RSS lead paragraph.</p>")

    # LLM returns a long-enough summary -> used, flagged as llm_summary
    async def fake_llm(*args, **kwargs):
        return NewsSummarySchema(summary="A well formed AI generated summary sentence.")

    monkeypatch.setattr(news_llm_engine, "extract_structured", fake_llm)
    summary, is_llm = await crawler._build_summary("TechCrunch AI", entry, "Title", "Full article body text here.")
    assert is_llm is True
    assert "well formed AI generated" in summary

    # LLM raises -> fall back to the cleaned RSS summary, not llm_summary
    async def failing_llm(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(news_llm_engine, "extract_structured", failing_llm)
    summary, is_llm = await crawler._build_summary("TechCrunch AI", entry, "Title", "Full article body text here.")
    assert is_llm is False
    assert "RSS lead paragraph" in summary

    # LLM returns a summary shorter than the 20-char floor -> RSS fallback wins
    async def short_llm(*args, **kwargs):
        return NewsSummarySchema(summary="tiny")

    monkeypatch.setattr(news_llm_engine, "extract_structured", short_llm)
    summary, is_llm = await crawler._build_summary("TechCrunch AI", entry, "Title", "Full article body text here.")
    assert is_llm is False


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_news_resolve_entry_date_full_text_inference(monkeypatch):
    crawler = NewsCrawler()
    # Full-text relative recency expression: '3 hours ago' resolves to a fresh date
    resolved = crawler._resolve_entry_date(
        None, None, "", "Published 3 hours ago by the editorial team.",
        "https://example.com/x", "https://example.com/x", "title",
    )
    assert resolved is not None
    age = datetime.now(timezone.utc) - resolved
    assert 2.5 * 3600 < age.total_seconds() < 3.5 * 3600

    # HTML meta-date that fails the freshness gate counts as a source date -> strict
    # rejection (None), never rescued by the dateless-novelty stamp.
    stale_html = '<meta property="article:published_time" content="2026-08-01T10:00:00Z">'
    assert crawler._resolve_entry_date(
        None, None, stale_html, "", "https://example.com/y", "https://example.com/y", "title2",
    ) is None


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_news_process_entry_early_rejects():
    crawler = NewsCrawler()
    # Missing title / missing link / HN-internal link all short-circuit before any fetch
    assert await crawler._process_entry(_NS(title="", link="https://example.com/a"), "Feed") is None
    assert await crawler._process_entry(_NS(title="T", link=""), "Feed") is None
    assert await crawler._process_entry(_NS(title="T", link="https://news.ycombinator.com/item?id=1"), "Feed") is None


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_news_crawl_source_error_paths():
    crawler = NewsCrawler(sources=[{"name": "Feed A", "feed_url": "https://a/feed"}])

    # Source-level fetch failure -> [] plus a warning
    async def fetch_feed_boom(url, **kwargs):
        raise RuntimeError("feed down")

    crawler.fetch_feed = fetch_feed_boom  # type: ignore[method-assign]
    assert await crawler.crawl_source({"name": "Feed A", "feed_url": "https://a/feed"}) == []

    # Entry-level processing failure -> isolated by gather(return_exceptions=True)
    async def process_boom(entry, source_name):
        raise ValueError("entry exploded")

    crawler.fetch_feed = AsyncMock(return_value=[_NS(title="a")])
    crawler._process_entry = process_boom  # type: ignore[method-assign]
    assert await crawler.crawl_source({"name": "Feed A", "feed_url": "https://a/feed"}) == []


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_news_crawl_post_gather_dedup_and_telemetry_failure(monkeypatch, tmp_path, capsys):
    # Two sources producing near-identical normalized titles: the post-gather pass
    # removes the duplicate pair; telemetry persistence failures are swallowed.
    crawler = NewsCrawler(sources=[{"name": "A", "feed_url": "https://a/feed"}, {"name": "B", "feed_url": "https://b/feed"}])

    async def fake_crawl_source(src):
        return [
            NewsRecord(
                source=SourceMetadata(name="A", url="https://a/1"),
                content=NewsContent(
                    title="OpenAI Ships Agent Tools",
                    published_date="2026-09-04T10:00:00Z",
                    summary=None,
                    full_text="A sufficiently long article body that repeats the headline news multiple times.",
                ),
            ),
            NewsRecord(
                source=SourceMetadata(name="B", url="https://b/1"),
                content=NewsContent(
                    title="OpenAI Ships Agent Tools!",  # fuzzy duplicate of the first
                    published_date="2026-09-04T10:30:00Z",
                    summary=None,
                    full_text="Another sufficiently long article body repeating the same headline news over and over.",
                ),
            ),
        ]

    crawler.crawl_source = fake_crawl_source  # type: ignore[method-assign]

    def failing_dump(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("src.crawlers.news_crawler.json.dump", failing_dump)

    # Act
    records = await crawler.crawl()

    # Assert - one record survives the fuzzy post-gather dedup; telemetry failure did not abort
    assert len(records) == 1
    assert records[0].source.url == "https://a/1"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_hacker_news_article_full_text_paths():
    # HN feed entries link out to external articles via their RSS description blob.
    # Entry 1: article fetch succeeds -> full text from the linked article.
    # Entry 2: article fetch fails -> title fallback, no HTML ever leaks into the summary.
    hn_feed = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>HN AI</title>
<item>
<title>Frontier Model Breakthrough Announced</title>
<link>https://news.example.org/items/1</link>
<pubDate>Fri, 04 Sep 2026 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Article URL: &lt;a href="https://arxiv.org/abs/2406.0001"&gt;paper&lt;/a&gt;&lt;/p&gt;&lt;p&gt;Comments URL: &lt;a href="https://news.ycombinator.com/item?id=1"&gt;comments&lt;/a&gt;&lt;/p&gt;</description>
</item>
<item>
<title>Another Frontier Model Milestone</title>
<link>https://news.example.org/items/2</link>
<pubDate>Fri, 04 Sep 2026 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Article URL: &lt;a href="https://arxiv.org/abs/2406.0002"&gt;paper&lt;/a&gt;&lt;/p&gt;</description>
</item>
</channel></rss>
"""
    crawler = NewsCrawler(sources=[{"name": "Hacker News AI", "feed_url": "https://hn.example/ai"}])
    respx.get("https://hn.example/ai").mock(return_value=httpx.Response(200, text=hn_feed))
    respx.get("https://arxiv.org/abs/2406.0001").mock(return_value=httpx.Response(200, text=ARTICLE_HTML))
    respx.get("https://arxiv.org/abs/2406.0002").mock(return_value=httpx.Response(500))

    # Act
    records = await crawler.crawl()
    await crawler.close()

    # Assert - both survive; the first has real full text, the second falls back to its title
    assert len(records) == 2
    assert "frontier" in records[0].content.full_text.lower()
    assert records[0].content.summary and "<" not in records[0].content.summary
    assert records[1].content.full_text == "Another Frontier Model Milestone"
    assert records[1].content.summary == "Another Frontier Model Milestone"

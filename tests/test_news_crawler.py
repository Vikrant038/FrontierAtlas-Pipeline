"""
Unit tests for NewsCrawler freshness filtering, URL deduplication, and article extraction.
Follows AAA pattern with offline respx mocking and freezegun per CODING_STANDARDS.md.
"""

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

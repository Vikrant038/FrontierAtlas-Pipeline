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

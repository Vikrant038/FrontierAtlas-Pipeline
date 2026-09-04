import asyncio
from typing import Any, Dict, List, Optional

from src.crawlers.base import AsyncBaseCrawler
from src.llm.chunker import clean_html_text
from src.schemas.entities import NewsContent, NewsRecord, SourceMetadata
from src.utils.logger import logger

NEWS_SOURCES = [
    {"name": "TechCrunch AI", "feed_url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "VentureBeat AI", "feed_url": "https://venturebeat.com/category/ai/feed/"},
    {"name": "MIT Technology Review AI", "feed_url": "https://www.technologyreview.com/topic/artificial-intelligence/feed/"},
    {"name": "The Verge AI", "feed_url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"name": "Hacker News AI", "feed_url": "https://hnrss.org/newest?q=AI"},
]


class NewsCrawler(AsyncBaseCrawler):
    """Crawler for ingesting fresh (<24h) articles across 5 AI news sources."""

    def __init__(self, sources: Optional[List[Dict[str, str]]] = None, **kwargs):
        super().__init__(**kwargs)
        self.sources = sources or NEWS_SOURCES
        self._seen_urls: set = set()

    async def _process_entry(self, entry: Any, source_name: str) -> Optional[NewsRecord]:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not title or not link or link in self._seen_urls:
            return None
        self._seen_urls.add(link)

        pub_date = self.check_freshness(getattr(entry, "published", None) or getattr(entry, "updated", None))
        if not pub_date:
            return None

        full_text = ""
        try:
            full_text = clean_html_text(await self.fetch(link))
        except Exception:
            pass

        summary = getattr(entry, "summary", "") or (full_text[:300] if full_text else title)
        return NewsRecord(
            source=SourceMetadata(name=source_name, url=link),
            content=NewsContent(title=title, published_date=pub_date, summary=summary[:500] if summary else None, full_text=full_text or summary or title),
        )

    async def crawl_source(self, src: Dict[str, str]) -> List[NewsRecord]:
        try:
            entries = await self.fetch_feed(src["feed_url"])
            tasks = [self._process_entry(e, src["name"]) for e in entries]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return [r for r in results if isinstance(r, NewsRecord)]
        except Exception as exc:
            logger.warning(f"Error crawling {src['name']}: {exc}")
            return []

    async def crawl(self) -> List[NewsRecord]:
        """Crawl all 5 AI news sources concurrently."""
        logger.info(f"Starting NewsCrawler across {len(self.sources)} sources...")
        results = await asyncio.gather(*[self.crawl_source(s) for s in self.sources])
        records = [rec for sublist in results for rec in sublist]
        logger.info(f"Completed NewsCrawler: {len(records)} fresh articles (<24h) collected.")
        return records

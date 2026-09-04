import asyncio
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from typing import Any, Dict, List, Optional
from rapidfuzz import fuzz, process

from src.crawlers.base import AsyncBaseCrawler
from src.llm.chunker import clean_html_text
from src.schemas.entities import NewsContent, NewsRecord, SourceMetadata
from src.utils.date_normalizer import extract_date_from_html, infer_content_freshness
from src.utils.run_state import load_seen_keys, save_seen_keys
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
        self._seen_titles: List[str] = []
        self.stats: Dict[str, Dict[str, int]] = {
            s["name"]: {"total": 0, "full_text": 0} for s in self.sources
        }
        # Keys collected in the PREVIOUS run: cross-run novelty heuristic state.
        self._prev_run_urls = load_seen_keys("news")
        self._novelty_keys: set = set()

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize URL: lowercase scheme/host, strip tracking/referral params, strip trailing slash."""
        if not url or not isinstance(url, str):
            return ""
        parsed = urlparse(url.strip())
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
            netloc = netloc.rsplit(":", 1)[0]
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"
        filtered_params = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"ref", "referrer", "source", "fbclid", "gclid", "ncid", "ocid"}
        ]
        query = urlencode(filtered_params)
        return urlunparse((scheme, netloc, path, parsed.params, query, ""))

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize title: strip publisher brand suffixes, bracketed tags, punctuation, and lowercase."""
        if not title or not isinstance(title, str):
            return ""
        cleaned = re.sub(
            r"\s*(?:[-|–—]|\()\s*(TechCrunch|The Verge|VentureBeat|MIT Technology Review|Hacker News|Ars Technica|Wired|Gizmodo)\s*\)?\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\[video\]|\[pdf\]|\[audio\]", "", cleaned, flags=re.IGNORECASE)
        cleaned = "".join(c for c in unicodedata.normalize("NFKD", cleaned.strip().lower()) if not unicodedata.combining(c))
        cleaned = re.sub(r"[^\w\s]", " ", cleaned)
        return " ".join(cleaned.split())

    async def _process_entry(self, entry: Any, source_name: str) -> Optional[NewsRecord]:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not title or not link or "news.ycombinator.com" in link:
            return None

        norm_url = self._normalize_url(link)
        if norm_url in self._seen_urls:
            return None

        norm_title = self._normalize_title(title)
        if self._seen_titles:
            match = process.extractOne(norm_title, self._seen_titles, scorer=fuzz.token_sort_ratio, score_cutoff=90.0)
            if match:
                logger.info(f"Duplicate news title skipped: '{title}' ({match[1]:.1f}% match with '{match[0]}')")
                return None

        # Reserve dedup keys synchronously BEFORE awaiting the fetch: concurrent
        # gather tasks would otherwise both pass the membership check (add-after-await race).
        self._seen_urls.add(norm_url)
        self._seen_titles.append(norm_title)

        full_text = ""
        is_full_text = False
        raw_html = ""
        try:
            raw_html = await self.fetch(link)
            cleaned = clean_html_text(raw_html)
            if cleaned and len(cleaned.strip()) >= 20:
                full_text = cleaned.strip()
                is_full_text = True
        except Exception as exc:
            logger.debug(f"Full-text fetch failed for {link}: {exc}")

        # Freshness gate: strict feed date, then HTML metadata, then text inference,
        # then cross-run novelty (unseen since last run = new; previously seen = old).
        pub_date = self.check_freshness(getattr(entry, "published", None) or getattr(entry, "updated", None))
        date_inferred = False
        if not pub_date:
            pub_date = self.check_freshness(extract_date_from_html(raw_html, page_url=link))
            date_inferred = pub_date is not None
        if not pub_date and full_text:
            pub_date = self.check_freshness(infer_content_freshness(full_text))
            date_inferred = pub_date is not None
        if not pub_date:
            if norm_url in self._prev_run_urls:
                # Seen in a previous run and no fresh-date signal: not new, reject.
                self._seen_urls.discard(norm_url)
                if norm_title in self._seen_titles:
                    self._seen_titles.remove(norm_title)
                return None
            # Never seen before: treat as new since last run, stamp collection time.
            pub_date = datetime.now(timezone.utc)
            date_inferred = True
            logger.debug(f"Dateless entry treated as new-since-last-run: '{title}' ({source_name}).")
        if date_inferred:
            logger.debug(f"Publication date inferred heuristically for '{title}' ({source_name}).")

        if source_name in self.stats:
            self.stats[source_name]["total"] += 1
            if is_full_text:
                self.stats[source_name]["full_text"] += 1

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
        """Crawl all 5 AI news sources concurrently with deduplication and coverage telemetry."""
        logger.info(f"Starting NewsCrawler across {len(self.sources)} sources...")
        results = await asyncio.gather(*[self.crawl_source(s) for s in self.sources])
        records = [rec for sublist in results for rec in sublist]

        # Persist this run's keys for the next run's novelty heuristic.
        self._novelty_keys.update(self._seen_urls)
        save_seen_keys("news", self._novelty_keys)

        # Post-gather title deduplication pass ensuring zero duplicate pairs
        deduped_records: List[NewsRecord] = []
        final_seen_titles: List[str] = []
        for rec in records:
            nt = self._normalize_title(rec.content.title)
            if final_seen_titles:
                match = process.extractOne(nt, final_seen_titles, scorer=fuzz.token_sort_ratio, score_cutoff=90.0)
                if match:
                    logger.info(f"Post-gather duplicate title removed: '{rec.content.title}' ({match[1]:.1f}% match with '{match[0]}')")
                    continue
            final_seen_titles.append(nt)
            deduped_records.append(rec)

        # Log full-text coverage warnings and stats per source
        coverage_summary = ", ".join(
            f"{s}: {st['full_text']}/{st['total']} full-text"
            for s, st in self.stats.items() if st["total"] > 0
        )
        logger.warning(f"News full-text coverage summary: {coverage_summary}")
        for src_name, st in self.stats.items():
            if st["total"] > 0:
                logger.warning(f"Full-text coverage: {src_name}: {st['full_text']}/{st['total']} full-text")

        logger.info(f"Completed NewsCrawler: {len(deduped_records)} fresh articles (<24h) collected.")
        return deduped_records

import asyncio
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from typing import Any, Dict, List, Optional, Tuple
from rapidfuzz import fuzz, process

from src.crawlers.base import AsyncBaseCrawler
from src.llm.chunker import clean_html_text
from src.llm.fallback_chain import llm_engine
from src.llm.prompts import NewsSummarySchema, NEWS_SUMMARY_PROMPT
from src.schemas.entities import NewsContent, NewsRecord, SourceMetadata
from src.utils.date_normalizer import extract_date_from_html, infer_content_freshness, parse_datetime_to_utc
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
            s["name"]: {"total": 0, "full_text": 0, "llm_summary": 0, "rss_fallback": 0} for s in self.sources
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
    def _extract_hn_article_url(rss_description: str) -> Optional[str]:
        """Extract the real external Article URL from HN's RSS description HTML blob.

        HN RSS descriptions look like:
          <p>Article URL: <a href="https://example.com/...">...</a></p>
          <p>Comments URL: <a href="https://news.ycombinator.com/...">...</a></p>
        We want the first non-HN href — the actual source article.
        """
        if not rss_description:
            return None
        # Match any href that is NOT a news.ycombinator.com link
        href_pattern = re.compile(r'href="(https?://(?!news\.ycombinator\.com)[^"]+)"', re.IGNORECASE)
        match = href_pattern.search(rss_description)
        if match:
            candidate = match.group(1).strip()
            # Reject obviously bad targets (GitHub issue trackers, video embeds, etc. are fine)
            if candidate and not candidate.startswith("javascript:"):
                return candidate
        return None

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

    @staticmethod
    def _is_duplicate_title(norm_title: str, seen_titles: List[str]) -> Optional[Tuple[str, float]]:
        """Return (matched_title, score) if norm_title fuzzy-matches a seen title, else None."""
        if not seen_titles:
            return None
        match = process.extractOne(norm_title, seen_titles, scorer=fuzz.token_sort_ratio, score_cutoff=90.0)
        return (match[0], match[1]) if match else None

    async def _fetch_full_text(
        self, source_name: str, entry: Any, link: str, title: str
    ) -> Tuple[str, bool, str]:
        """Fetch the article body text; HN resolves the linked article from the RSS description blob."""
        full_text = ""
        is_full_text = False
        raw_html = ""
        if source_name == "Hacker News AI":
            # HN RSS descriptions are HTML blobs: <p>Article URL: <a href="...">...</a></p>...
            # Extract the real external article URL and attempt a full-text fetch.
            rss_description = getattr(entry, "summary", "") or ""
            article_url = self._extract_hn_article_url(rss_description) or link
            if article_url != link:
                try:
                    raw_html = await self.fetch(article_url, timeout=6.0, allow_tls_fallback=False, allow_retry=False)
                    cleaned = clean_html_text(raw_html)
                    if cleaned and len(cleaned.strip()) >= 20:
                        full_text = cleaned.strip()
                        is_full_text = True
                        logger.debug(f"HN full-text fetched from linked article: {article_url}")
                except Exception as exc:
                    logger.debug(f"HN article fetch failed for {article_url}: {exc}")
            # If full-text unavailable, synthesize a clean summary from title (no HTML dumped)
            if not full_text:
                full_text = title
        else:
            try:
                raw_html = await self.fetch(link, timeout=5.0, allow_tls_fallback=False, allow_retry=False)
                cleaned = clean_html_text(raw_html)
                if cleaned and len(cleaned.strip()) >= 20:
                    full_text = cleaned.strip()
                    is_full_text = True
            except Exception as exc:
                logger.debug(f"Full-text fetch failed for {link}: {exc}")
        return full_text, is_full_text, raw_html

    async def _build_summary(
        self, source_name: str, entry: Any, title: str, full_text: str
    ) -> Tuple[str, bool]:
        """Construct a plain-text summary; LLM-generated when full text is available."""
        # HN: rss_summary contains "Article URL: / Comments URL: / Points:" metadata even after HTML strip.
        # For HN we use full_text exclusively (fetched article body or title-fallback — never metadata).
        # Other sources: strip any residual HTML tags from the RSS snippet to keep the field plain-text.
        if source_name == "Hacker News AI":
            summary = (full_text[:300] if full_text else title)
        else:
            rss_summary_raw = getattr(entry, "summary", "") or ""
            rss_summary_clean = clean_html_text(rss_summary_raw) if rss_summary_raw else ""
            summary = rss_summary_clean or (full_text[:300] if full_text else title)
        is_llm_summary = False
        if full_text:
            try:
                llm_out = await llm_engine.extract_structured(
                    raw_text=full_text,
                    schema_cls=NewsSummarySchema,
                    instruction=NEWS_SUMMARY_PROMPT,
                )
                if llm_out.summary and len(llm_out.summary.strip()) >= 20:
                    summary = llm_out.summary.strip()
                    is_llm_summary = True
            except Exception as exc:
                logger.debug(f"LLM summary extraction fallback for '{title}': {exc}")
        return summary, is_llm_summary

    def _rollback_seen(self, url: str, title: str) -> None:
        """Roll back reserved dedup keys on freshness rejection."""
        self._seen_urls.discard(url)
        if title in self._seen_titles:
            self._seen_titles.remove(title)

    async def _process_entry(self, entry: Any, source_name: str) -> Optional[NewsRecord]:
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link or "news.ycombinator.com" in link:
            return None

        norm_url = self._normalize_url(link)
        if norm_url in self._seen_urls:
            return None

        norm_title = self._normalize_title(title)
        match = self._is_duplicate_title(norm_title, self._seen_titles)
        if match:
            logger.info(f"Duplicate news title skipped: '{title}' ({match[1]:.1f}% match with '{match[0]}')")
            return None

        # Fast freshness pre-check: skip network fetch if explicit feed date fails 24h gate
        raw_feed_date = getattr(entry, "published", None) or getattr(entry, "updated", None)
        parsed_feed_date = parse_datetime_to_utc(raw_feed_date) if raw_feed_date else None
        pub_date = self.check_freshness(raw_feed_date)
        if parsed_feed_date is not None and pub_date is None:
            return None

        # Reserve dedup keys synchronously BEFORE awaiting the fetch: concurrent
        # gather tasks would otherwise both pass the membership check (add-after-await race).
        self._seen_urls.add(norm_url)
        self._seen_titles.append(norm_title)

        full_text, is_full_text, raw_html = await self._fetch_full_text(source_name, entry, link, title)

        has_source_date = raw_feed_date is not None and str(raw_feed_date).strip() != ""
        date_inferred = False
        if not pub_date:
            html_date = extract_date_from_html(raw_html, page_url=link)
            has_source_date = has_source_date or html_date is not None
            pub_date = self.check_freshness(html_date)
            date_inferred = pub_date is not None
        if not pub_date and full_text:
            pub_date = self.check_freshness(infer_content_freshness(full_text))
            date_inferred = pub_date is not None
        if not pub_date:
            if has_source_date:
                # Source stated a date but it failed the 24h gate (all heuristics missed):
                # strictly stale - novelty stamping must not override a real date.
                self._rollback_seen(norm_url, norm_title)
                return None
            if norm_url in self._prev_run_urls:
                # Truly dateless, seen in a previous run: not new, reject.
                self._rollback_seen(norm_url, norm_title)
                return None
            # Truly dateless, never seen before: treat as new since last run.
            pub_date = datetime.now(timezone.utc)
            date_inferred = True
            logger.debug(f"Dateless entry treated as new-since-last-run: '{title}' ({source_name}).")
        if date_inferred:
            logger.debug(f"Publication date inferred heuristically for '{title}' ({source_name}).")

        if source_name in self.stats:
            self.stats[source_name]["total"] += 1
            if is_full_text:
                self.stats[source_name]["full_text"] += 1

        summary, is_llm_summary = await self._build_summary(source_name, entry, title, full_text)

        if source_name in self.stats:
            if is_llm_summary:
                self.stats[source_name]["llm_summary"] += 1
            else:
                self.stats[source_name]["rss_fallback"] += 1

        return NewsRecord(
            source=SourceMetadata(name=source_name, url=link),
            content=NewsContent(title=title, published_date=pub_date, summary=summary[:500] if summary else None, full_text=full_text or summary or title),
        )

    async def crawl_source(self, src: Dict[str, str]) -> List[NewsRecord]:
        try:
            entries = await self.fetch_feed(src["feed_url"])
            tasks = [self._process_entry(e, src["name"]) for e in entries]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning(f"Entry processing failed in {src['name']}: {r!r}")
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
            match = self._is_duplicate_title(nt, final_seen_titles)
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

        # Persist summary telemetry for audit and verification
        try:
            os.makedirs("exports", exist_ok=True)
            tmp_path = "exports/news_summary_telemetry.json.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, indent=2)
            os.replace(tmp_path, "exports/news_summary_telemetry.json")
        except Exception as exc:
            logger.debug(f"Could not persist news summary telemetry: {exc}")

        logger.info(f"Completed NewsCrawler: {len(deduped_records)} fresh articles (<24h) collected.")
        return deduped_records

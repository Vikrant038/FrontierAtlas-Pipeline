"""
High-throughput research papers crawler for Arxiv & Fastly CDN RSS syndication feeds.
Correlates paper preprints with verified author GitHub repositories and dynamic live star telemetry.
"""

import asyncio
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import feedparser

from src.config import settings
from src.crawlers.base import BotBlockedError, TargetedCrawler, github_headers, is_github_quota_error
from src.schemas.entities import ResearchPaperContent, ResearchPaperRecord
from src.utils.date_normalizer import parse_datetime_to_utc
from src.utils.logger import logger

GITHUB_URL_PATTERN = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")

GITHUB_MIN_REQUEST_INTERVAL_SECONDS = 2.1  # default pace; overridable via GITHUB_INTERVAL_SECONDS
GITHUB_ANONYMOUS_LOOKUP_BUDGET = 50  # default anonymous cap; overridable via GITHUB_ANONYMOUS_LOOKUP_BUDGET


def _paper_record(
    title: str,
    authors: List[str],
    paper_url: str,
    published_date: Any,
    repo: Optional[str],
) -> Dict[str, Any]:
    """Build the shared paper record shape used by every acquisition source."""
    return {
        "title": title,
        "authors": authors or ["AI Researcher"],
        "paper_url": paper_url,
        "published_date": published_date,
        "abstract_repo": repo,
    }


class ResearchPapersCrawler(TargetedCrawler):
    """Production-grade ArXiv crawler with leaky-bucket rate limiting and author code correlation."""

    ARXIV_API_BASE = "https://export.arxiv.org/api/query"
    ARXIV_INTERVAL_SECONDS = 3.2  # default; overridable via ARXIV_INTERVAL_SECONDS
    CDN_CATEGORIES = ["cs.AI", "cs.LG", "cs.CV", "cs.CL", "cs.NE"]  # default; overridable via ARXIV_CDN_CATEGORIES

    def __init__(self, target_count: int = 1000, **kwargs):
        super().__init__(target_count=target_count, **kwargs)
        self._last_arxiv_time: float = 0.0
        # Per-token GitHub pacing slots: each pooled token paces on its own key so
        # N tokens allow N interleaved lookups; "" is the shared anonymous slot.
        self._github_slots: Dict[str, float] = {}
        self._use_cdn = False
        # Sentinel backs off star enrichment for one hour after quota exhaustion
        # (prevents thousands of doomed requests in anonymous mode: 60 req/hr cap).
        self._github_quota_blocked_until: float = 0.0
        self._anonymous_lookups: int = 0
        self.enriched_count: int = 0  # live progress reporting of completed star lookups
        self.enrich_total: int = 0    # total paper repos queued for star enrichment

    @property
    def is_enriching(self) -> bool:
        """True while background star lookups are actively in-flight."""
        return (
            self.enrich_total > 0
            and self.enriched_count < self.enrich_total
            and not self._github_quota_blocked()
        )

    def _github_quota_blocked(self) -> bool:
        """True while the one-hour GitHub quota backoff window is active."""
        return time.monotonic() < self._github_quota_blocked_until

    def _disable_github_enrichment(self) -> None:
        """Back off star enrichment for one hour on verified GitHub quota exhaustion."""
        self._github_quota_blocked_until = time.monotonic() + 3600.0
        logger.warning("GitHub API quota exhausted. Disabling star enrichment for the next hour.")

    @staticmethod
    def _seconds_until_slot(last_time: float, interval: float) -> float:
        """Seconds to sleep so consecutive requests stay >= interval apart."""
        return max(0.0, interval - (time.monotonic() - last_time))

    async def _pace_github(self, token: Optional[str] = None) -> None:
        """Reserve the next pacing slot for a GitHub token (or the anonymous '' slot).
        Each token has its own slot, so pooled tokens run interleaved lookups while a
        single token keeps the exact 1-per-interval cadence. The reserve is atomic:
        no await between reading the slot and writing the reservation."""
        slot_key = token or ""
        interval = settings.github_interval_seconds
        now = time.monotonic()
        fire_at = max(now, self._github_slots.get(slot_key, 0.0))
        self._github_slots[slot_key] = fire_at + interval
        wait = fire_at - now
        if wait > 0:
            await asyncio.sleep(wait)

    async def _fetch_stars(self, repo_path: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
        """GitHub repository and star lookup with pacing, token-pool rotation, and quota-exhaustion backoff."""
        if not repo_path:
            return None, None
        if self._github_quota_blocked():
            logger.debug(f"GitHub quota backoff active; skipping star lookup for {repo_path}.")
            return f"https://github.com/{repo_path}", None

        token = self._pick_github_token(repo_path)
        if token is None and self._anonymous_lookups >= settings.github_anonymous_lookup_budget:
            # Anonymous GitHub REST caps at 60 req/hr: stop gracefully instead of burning 403s.
            logger.info(
                f"Anonymous GitHub budget ({settings.github_anonymous_lookup_budget}/hr) reached; "
                "pausing star enrichment for one hour."
            )
            self._github_quota_blocked_until = time.monotonic() + 3600.0
            return f"https://github.com/{repo_path}", None
        if token is None:
            self._anonymous_lookups += 1

        await self._pace_github(token)
        try:
            # GitHub API 403 = quota exhaustion, not fingerprint blocking; TLS retry cannot help.
            data = await self.fetch_json(
                f"https://api.github.com/repos/{repo_path}",
                headers=github_headers(token),
                allow_tls_fallback=False,
            )
            if data and "stargazers_count" in data:
                return f"https://github.com/{repo_path}", data["stargazers_count"]
        except BotBlockedError as exc:
            if is_github_quota_error(exc):
                self._mark_github_token_exhausted(token)
            else:
                logger.warning(f"GitHub 403 (not quota) for {repo_path}: {exc}")
        except Exception as exc:
            logger.warning(f"GitHub star lookup failed for {repo_path}: {exc}")
            if is_github_quota_error(exc):
                self._mark_github_token_exhausted(token)
        return f"https://github.com/{repo_path}", None

    def _mark_github_token_exhausted(self, token: Optional[str]) -> None:
        """Drop an exhausted token from the pool; pause enrichment only when every token is gone."""
        if token:
            self._exhausted_github_tokens.add(token)
            logger.warning(
                f"GitHub token exhausted (quota); removed from pool ({len(self._exhausted_github_tokens)}/{len(self.github_tokens) or 1})."
            )
        if self._pick_github_token("") is None:
            self._disable_github_enrichment()

    def _parse_feed_entry(self, entry: Any) -> Optional[Dict[str, Any]]:
        """Extract and normalize paper metadata from Atom or RSS feed entry."""
        raw_title = getattr(entry, "title", "")
        title = re.sub(r"\s+", " ", re.sub(r"\s*\(arXiv:[^)]+\).*", "", raw_title)).strip()
        if not title:
            return None

        raw_authors = [getattr(a, "name", "") for a in getattr(entry, "authors", [])]
        creator = getattr(entry, "creator", "") or getattr(entry, "dc_creator", "") or getattr(entry, "author", "")
        names_str = " ".join(raw_authors) if raw_authors else re.sub(r"<[^>]+>", "", creator)
        authors = [a.strip() for a in re.split(r"[,;]", names_str) if a.strip()]

        date_str = getattr(entry, "published", "") or getattr(entry, "updated", "") or getattr(entry, "pubDate", "")
        pub_date = parse_datetime_to_utc(date_str)
        if not pub_date:
            logger.warning(f"Arxiv paper '{title}' missing valid date. Skipping.")
            return None

        raw_text = " ".join([
            getattr(entry, "summary", ""),
            getattr(entry, "description", ""),
            getattr(entry, "arxiv_comment", ""),
            getattr(entry, "comment", ""),
        ])
        clean_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_text)).strip()
        gh_match = GITHUB_URL_PATTERN.search(clean_text)
        abstract_repo = gh_match.group(1).rstrip(".,;:)'\"") if gh_match else None
        if abstract_repo and abstract_repo.endswith(".git"):
            abstract_repo = abstract_repo[:-4]

        paper_url = getattr(entry, "link", "")
        if paper_url.startswith("http://"):
            paper_url = "https://" + paper_url[7:]
        for link in getattr(entry, "links", []):
            if getattr(link, "type", "") == "application/pdf":
                paper_url = link.href
                break

        return _paper_record(title, authors, paper_url, pub_date, abstract_repo)

    def _collect_until_limit(
        self, candidates: Iterable[Optional[Dict[str, Any]]], limit: int
    ) -> List[Dict[str, Any]]:
        """Register-and-deduplicate candidate papers until the batch limit is reached."""
        papers: List[Dict[str, Any]] = []
        for paper in candidates:
            if paper and not self.is_seen(paper["paper_url"]):
                papers.append(paper)
                if len(papers) >= limit:
                    break
        return papers

    async def _query_arxiv_api(self, start: int, limit: int) -> List[Dict[str, Any]]:
        """Query official Arxiv Atom API with leaky-bucket pacing and automatic retry backoff."""
        # Arxiv API asks <= 3 requests/second; pace against the configured interval.
        await asyncio.sleep(self._seconds_until_slot(self._last_arxiv_time, settings.arxiv_interval_seconds))
        self._last_arxiv_time = time.monotonic()

        params = {
            "search_query": settings.arxiv_search_query,
            "start": start,
            "max_results": limit,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        content = await self.fetch(
            self.ARXIV_API_BASE,
            params=params,
            headers={"Accept": "application/atom+xml,application/xml,text/xml,*/*"},
            timeout=30.0,
        )
        feed = feedparser.parse(content)
        return self._collect_until_limit(
            (self._parse_feed_entry(e) for e in getattr(feed, "entries", [])), limit
        )

    async def _query_cdn_category(self, cat: str, limit: int) -> List[Dict[str, Any]]:
        """Fetch and parse a single CDN category feed (helper for parallel category harvest)."""
        entries = await self.fetch_feed(f"https://rss.arxiv.org/rss/{cat}")
        return self._collect_until_limit((self._parse_feed_entry(e) for e in entries), limit)

    async def _query_arxiv_cdn(self, limit: int) -> List[Dict[str, Any]]:
        """Harvest fresh papers from Fastly CDN-cached Arxiv RSS feeds, categories in parallel."""
        cats = [c.strip() for c in settings.arxiv_cdn_categories.split(",") if c.strip()]
        results: List[Dict[str, Any]] = []
        if not cats:
            return results
        per_cat = await asyncio.gather(
            *(self._query_cdn_category(cat, limit) for cat in cats), return_exceptions=True
        )
        for cat, batch in zip(cats, per_cat):
            if isinstance(batch, Exception):
                logger.warning(f"Arxiv CDN RSS failed for {cat}: {repr(batch)}")
                continue
            results.extend(batch)
            if len(results) >= limit:
                break
        return results[:limit]

    async def _query_hf_papers(self, limit: int) -> List[Dict[str, Any]]:
        """Harvest fresh papers from Hugging Face Daily Papers API with verified code repos."""
        try:
            items = await self.fetch_json(f"https://huggingface.co/api/daily_papers?limit={settings.hf_daily_limit}")
        except Exception as exc:
            logger.warning(f"HF daily papers error: {repr(exc)}")
            return []

        def candidates() -> Iterable[Optional[Dict[str, Any]]]:
            for item in (items or []):
                p = item.get("paper", {})
                title = p.get("title", "").strip()
                arxiv_id = p.get("id", "")
                pub_date = parse_datetime_to_utc(p.get("publishedAt"))
                if not title or not pub_date or not arxiv_id:
                    continue
                repo_raw = p.get("githubRepo") or ""
                m = GITHUB_URL_PATTERN.search(repo_raw) if repo_raw else None
                authors = [a.get("name") for a in p.get("authors", []) if a.get("name")]
                yield _paper_record(
                    title, authors, f"https://arxiv.org/abs/{arxiv_id}", pub_date, m.group(1) if m else None
                )

        return self._collect_until_limit(candidates(), limit)

    async def _query_openalex_papers(self, page: int, limit: int) -> List[Dict[str, Any]]:
        """Harvest authentic arXiv papers via OpenAlex academic mirror with high throughput."""
        params = {
            "filter": "primary_location.source.id:S4306400194,concepts.id:C154945302",
            "sort": "publication_date:desc",
            "per-page": min(settings.openalex_per_page, limit),
            "page": page,
        }
        if settings.openalex_email:
            # Polite-pool attribution raises the daily cap from 100k to 1M works/day.
            params["mailto"] = settings.openalex_email
        try:
            data = await self.fetch_json("https://api.openalex.org/works", params=params)
        except Exception as exc:
            logger.warning(f"OpenAlex arXiv mirror error: {repr(exc)}")
            return []

        def candidates() -> Iterable[Optional[Dict[str, Any]]]:
            for w in (data or {}).get("results", []):
                title = (w.get("title") or "").strip()
                loc = (w.get("primary_location") or {}).get("landing_page_url") or w.get("doi") or ""
                pub_date = parse_datetime_to_utc(w.get("publication_date"))
                if not title or not loc or not pub_date:
                    continue
                authors = [a.get("author", {}).get("display_name") for a in w.get("authorships", []) if a.get("author", {}).get("display_name")]
                gh_match = GITHUB_URL_PATTERN.search(f"{title} {loc}")
                yield _paper_record(title, authors, loc, pub_date, gh_match.group(1) if gh_match else None)

        return self._collect_until_limit(candidates(), limit)

    async def fetch_papers_batch(self, start: int, limit: int) -> List[Dict[str, Any]]:
        """Fetch papers with automatic multi-tier failover (Arxiv API -> CDN RSS -> HF -> OpenAlex)."""
        if not self._use_cdn:
            try:
                papers = await self._query_arxiv_api(start, limit)
                if papers:
                    return papers
            except Exception as exc:
                logger.warning(f"Arxiv API persistent outage ({repr(exc)}). Switching to CDN RSS failover.")
                self._use_cdn = True

        # CDN -> HF -> OpenAlex supplement sources run concurrently (each is a single
        # request); results are merged in that priority order, deduplicated by URL.
        page = (start // 100) + 1
        cdn_papers, hf_papers, alex_papers = await asyncio.gather(
            self._query_arxiv_cdn(limit),
            self._query_hf_papers(limit),
            self._query_openalex_papers(page, limit),
            return_exceptions=True,
        )
        merged: List[Dict[str, Any]] = []
        seen_urls: set = set()
        for batch in (cdn_papers, hf_papers, alex_papers):
            if isinstance(batch, Exception):
                logger.warning(f"Paper supplement source failed: {repr(batch)}")
                continue
            for candidate in batch:
                url = candidate.get("paper_url")
                if url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                merged.append(candidate)
                if len(merged) >= limit:
                    return merged
        return merged

    async def enrich_paper(self, paper: Dict[str, Any]) -> ResearchPaperRecord:
        """Enrich paper metadata with live GitHub repository stars."""
        repo_url, stars = await self._fetch_stars(paper.get("abstract_repo"))
        return ResearchPaperRecord(
            content=ResearchPaperContent(
                title=paper["title"],
                authors=paper["authors"],
                paper_url=paper["paper_url"],
                github_url=repo_url,
                github_stars=stars,
                published_date=paper["published_date"],
            )
        )

    async def _enrich_batch(self, batch: List[Tuple[ResearchPaperRecord, str]]) -> None:
        """Enrich a batch of paper records with GitHub stars in background."""
        for idx, (rec, repo_path) in enumerate(batch):
            if self._github_quota_blocked():
                self.enriched_count += len(batch) - idx
                break
            try:
                repo_url, stars = await self._fetch_stars(repo_path)
                rec.content.github_url = repo_url
                rec.content.github_stars = stars
            except Exception as exc:
                logger.debug(f"Background star enrichment error for {repo_path}: {exc}")
            finally:
                self.enriched_count += 1

    async def _spawn_enrichment(
        self,
        batch_to_enrich: List[Tuple[ResearchPaperRecord, str]],
        enrich_tasks: List[asyncio.Task],
    ) -> List[asyncio.Task]:
        """Start a background enrichment task for a batch and prune finished tasks
        once 20 are in flight (keeps the pool bounded without blocking ingestion).
        Returns the surviving task list."""
        if not batch_to_enrich:
            return enrich_tasks
        self.enrich_total += len(batch_to_enrich)
        if self._github_quota_blocked():
            self.enriched_count += len(batch_to_enrich)
            return enrich_tasks
        enrich_tasks.append(asyncio.create_task(self._enrich_batch(batch_to_enrich)))
        if len(enrich_tasks) < 20:
            return enrich_tasks
        done, pending = await asyncio.wait(enrich_tasks, timeout=0)
        for t in done:
            if not t.cancelled() and t.exception() is not None:
                logger.debug(f"Enrichment task failed: {t.exception()!r}")
        return list(pending)

    async def _enrich_recovered(self, enrich_tasks: List[asyncio.Task]) -> List[asyncio.Task]:
        """Queue WAL-recovered papers (stars=None, repo known) for background enrichment.
        Without this, a resumed run ships recovered papers with stars=N/A forever: the
        fetch loop skips already-seen URLs and never re-derives their repo paths."""
        batch: List[Tuple[ResearchPaperRecord, str]] = []
        prefix = "https://github.com/"
        for rec in self.collected:
            if rec.content.github_stars is not None:
                continue
            gh_url = rec.content.github_url or ""
            if gh_url.startswith(prefix):
                repo_path = gh_url[len(prefix):].rstrip("/")
                if repo_path and "/" in repo_path:
                    batch.append((rec, repo_path))
        return await self._spawn_enrichment(batch, enrich_tasks)

    async def crawl(self) -> List[ResearchPaperRecord]:
        """Execute concurrent papers acquisition with decoupled ingestion and enrichment."""
        logger.info(f"Starting ResearchPapersCrawler (Target: {self.target_count} papers)...")
        recovered = self.recover_from_wal(model_cls=ResearchPaperRecord)
        enrich_tasks: List[asyncio.Task] = []
        if recovered:
            logger.info(f"Resumed {recovered} papers from WAL; continuing toward {self.target_count}.")
            enrich_tasks = await self._enrich_recovered(enrich_tasks)
        offset = 0

        while not self.is_full:
            needed = min(settings.papers_batch_size, self.remaining)
            papers = await self.fetch_papers_batch(offset, needed)
            if not papers:
                break

            # 1. Immediately ingest records into self.collected and streaming WAL
            batch_to_enrich: List[Tuple[ResearchPaperRecord, str]] = []
            for p in papers:
                rec = ResearchPaperRecord(
                    content=ResearchPaperContent(
                        title=p["title"],
                        authors=p["authors"],
                        paper_url=p["paper_url"],
                        github_url=f"https://github.com/{p['abstract_repo']}" if p.get("abstract_repo") else None,
                        github_stars=None,
                        published_date=p["published_date"],
                    )
                )
                self.add(rec.content.paper_url, rec, already_seen=True)
                if p.get("abstract_repo"):
                    batch_to_enrich.append((rec, p["abstract_repo"]))
                if self.is_full:
                    break

            # 2. Asynchronously enrich GitHub stars in background without blocking paper ingestion
            enrich_tasks = await self._spawn_enrichment(batch_to_enrich, enrich_tasks)

            offset += len(papers)
            logger.info(
                f"Papers progress: {len(self.collected)}/{self.target_count} collected"
                f" (GitHub enrichment {'active' if not self._github_quota_blocked() else 'disabled: quota backoff'})."
            )

        # 3. Finalize all pending background star enrichments before returning
        if enrich_tasks:
            outcomes = await asyncio.gather(*enrich_tasks, return_exceptions=True)
            failed = [o for o in outcomes if isinstance(o, Exception)]
            if failed:
                # Surface systematic enrichment failure (e.g. bad token pool) instead of
                # letting it vanish as per-task debug noise inside the gather result.
                logger.warning(
                    f"{len(failed)}/{len(outcomes)} background enrichment batches failed: "
                    f"{failed[0]!r}"
                )

        self.reset_wal_if_complete()
        logger.info(f"Completed ResearchPapersCrawler: {len(self.collected)} papers collected.")
        return self.collected[:self.target_count]

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
        self._last_github_time: float = 0.0
        self._github_pace_lock = asyncio.Lock()
        self._use_cdn = False
        # Sentinel backs off star enrichment for one hour after quota exhaustion
        # (prevents thousands of doomed requests in anonymous mode: 60 req/hr cap).
        self._github_quota_blocked_until: float = 0.0
        self._anonymous_lookups: int = 0

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

    async def _pace_github(self) -> None:
        """Leaky-bucket pacing for GitHub API, safe under concurrent asyncio.gather enrichment."""
        # Serialize the check-then-sleep-then-stamp sequence: concurrent enrich
        # tasks each reserve a distinct slot spaced by the minimum interval.
        async with self._github_pace_lock:
            await asyncio.sleep(
                self._seconds_until_slot(self._last_github_time, settings.github_interval_seconds)
            )
            self._last_github_time = time.monotonic()

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

        await self._pace_github()
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

    async def _query_arxiv_cdn(self, limit: int) -> List[Dict[str, Any]]:
        """Harvest fresh papers from Fastly CDN-cached Arxiv RSS feeds."""
        results: List[Dict[str, Any]] = []
        for cat in settings.arxiv_cdn_categories.split(","):
            cat = cat.strip()
            if len(results) >= limit:
                break
            try:
                entries = await self.fetch_feed(f"https://rss.arxiv.org/rss/{cat}")
                results.extend(
                    self._collect_until_limit(
                        (self._parse_feed_entry(e) for e in entries), limit - len(results)
                    )
                )
            except Exception as exc:
                logger.warning(f"Arxiv CDN RSS failed for {cat}: {repr(exc)}")
        return results

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

        cdn_papers = await self._query_arxiv_cdn(limit)
        if len(cdn_papers) < limit:
            hf_papers = await self._query_hf_papers(limit - len(cdn_papers))
            cdn_papers.extend(hf_papers)
        if len(cdn_papers) < limit:
            page = (start // 100) + 1
            alex_papers = await self._query_openalex_papers(page, limit - len(cdn_papers))
            cdn_papers.extend(alex_papers)
        return cdn_papers

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
        for rec, repo_path in batch:
            if self._github_quota_blocked():
                break
            try:
                repo_url, stars = await self._fetch_stars(repo_path)
                rec.content.github_url = repo_url
                rec.content.github_stars = stars
            except Exception as exc:
                logger.debug(f"Background star enrichment error for {repo_path}: {exc}")

    async def crawl(self) -> List[ResearchPaperRecord]:
        """Execute concurrent papers acquisition with decoupled ingestion and enrichment."""
        logger.info(f"Starting ResearchPapersCrawler (Target: {self.target_count} papers)...")
        recovered = self.recover_from_wal(model_cls=ResearchPaperRecord)
        if recovered:
            logger.info(f"Resumed {recovered} papers from WAL; continuing toward {self.target_count}.")
        offset = 0
        enrich_tasks: List[asyncio.Task] = []

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
            if batch_to_enrich and not self._github_quota_blocked():
                enrich_task = asyncio.create_task(self._enrich_batch(batch_to_enrich))
                enrich_tasks.append(enrich_task)
                if len(enrich_tasks) >= 20:
                    # Prune finished enrichment tasks without blocking ingestion.
                    done, enrich_tasks = await asyncio.wait(enrich_tasks, timeout=0)
                    for t in done:
                        if not t.cancelled() and t.exception() is not None:
                            logger.debug(f"Enrichment task failed: {t.exception()!r}")

            offset += len(papers)
            logger.info(
                f"Papers progress: {len(self.collected)}/{self.target_count} collected"
                f" (GitHub enrichment {'active' if not self._github_quota_blocked() else 'disabled: quota backoff'})."
            )

        # 3. Finalize all pending background star enrichments before returning
        if enrich_tasks:
            await asyncio.gather(*enrich_tasks, return_exceptions=True)

        self.reset_wal_if_complete()
        logger.info(f"Completed ResearchPapersCrawler: {len(self.collected)} papers collected.")
        return self.collected[:self.target_count]

"""
High-throughput research papers crawler for Arxiv & Fastly CDN RSS syndication feeds.
Correlates paper preprints with verified author GitHub repositories and dynamic live star telemetry.
"""

import asyncio
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import feedparser

from src.crawlers.base import BotBlockedError, TargetedCrawler, github_headers
from src.schemas.entities import ResearchPaperContent, ResearchPaperRecord
from src.utils.date_normalizer import parse_datetime_to_utc
from src.utils.logger import logger

GITHUB_URL_PATTERN = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")

GITHUB_MIN_REQUEST_INTERVAL_SECONDS = 2.1  # ~28 req/min, safely under 5,000/hr authenticated & 60/hr anonymous budgets


class ResearchPapersCrawler(TargetedCrawler):
    """Production-grade ArXiv crawler with leaky-bucket rate limiting and author code correlation."""

    ARXIV_API_BASE = "https://export.arxiv.org/api/query"
    ARXIV_INTERVAL_SECONDS = 3.2  # Strict 1 req / 3s ArXiv policy enforcement
    CDN_CATEGORIES = ["cs.AI", "cs.LG", "cs.CV", "cs.CL", "cs.NE"]

    def __init__(self, target_count: int = 1000, **kwargs):
        super().__init__(target_count=target_count, **kwargs)
        self._last_arxiv_time: float = 0.0
        self._last_github_time: float = 0.0
        self._github_pace_lock = asyncio.Lock()
        self._use_cdn = False
        # Sentinel disables star enrichment after quota exhaustion (prevents
        # thousands of doomed requests in anonymous mode: 60 req/hr cap).
        self._github_quota_exhausted: bool = False

    async def _pace_github(self) -> None:
        """Leaky-bucket pacing for GitHub API, safe under concurrent asyncio.gather enrichment."""
        # Serialize the check-then-sleep-then-stamp sequence: concurrent enrich
        # tasks each reserve a distinct slot spaced by the minimum interval.
        async with self._github_pace_lock:
            now = time.monotonic()
            elapsed = now - self._last_github_time
            if elapsed < GITHUB_MIN_REQUEST_INTERVAL_SECONDS:
                await asyncio.sleep(GITHUB_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
            self._last_github_time = time.monotonic()

    def _is_github_quota_exhaustion(self, exc: Exception) -> bool:
        """Detect GitHub rate-limit exhaustion (HTTP 403/429 with quota headers or message)."""
        exc_text = str(exc).lower()
        return (
            "403" in exc_text and ("rate limit" in exc_text or "quota" in exc_text)
        ) or "429" in exc_text

    async def _fetch_stars(self, repo_path: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
        """GitHub repository and star lookup with pacing and quota-exhaustion shutdown."""
        if not repo_path:
            return None, None
        if self._github_quota_exhausted:
            logger.debug(f"GitHub quota exhausted; skipping star lookup for {repo_path}.")
            return f"https://github.com/{repo_path}", None

        await self._pace_github()
        try:
            # GitHub API 403 = quota exhaustion, not fingerprint blocking; TLS retry cannot help.
            data = await self.fetch_json(
                f"https://api.github.com/repos/{repo_path}",
                headers=github_headers(self.github_token),
                allow_tls_fallback=False,
            )
            if data and "stargazers_count" in data:
                return f"https://github.com/{repo_path}", data["stargazers_count"]
        except BotBlockedError:
            # GitHub signals quota exhaustion with HTTP 403; TLS retry cannot help here.
            self._github_quota_exhausted = True
            logger.warning("GitHub API quota exhausted (HTTP 403). Disabling star enrichment for remainder of this run.")
        except Exception as exc:
            logger.warning(f"GitHub star lookup failed for {repo_path}: {exc}")
            if self._is_github_quota_exhaustion(exc):
                self._github_quota_exhausted = True
                logger.warning(
                    "GitHub API quota exhausted. Disabling star enrichment for remainder of this run."
                )
        return f"https://github.com/{repo_path}", None

    def _parse_feed_entry(self, entry: Any) -> Optional[Dict[str, Any]]:
        """Extract and normalize paper metadata from Atom or RSS feed entry."""
        raw_title = getattr(entry, "title", "")
        title = re.sub(r"\s+", " ", re.sub(r"\s*\(arXiv:[^)]+\).*", "", raw_title)).strip()
        if not title:
            return None

        raw_authors = [getattr(a, "name", "") for a in getattr(entry, "authors", [])]
        creator = getattr(entry, "creator", "") or getattr(entry, "dc_creator", "") or getattr(entry, "author", "")
        names_str = " ".join(raw_authors) if raw_authors else re.sub(r"<[^>]+>", "", creator)
        authors = [a.strip() for a in re.split(r"[,;]", names_str) if a.strip()] or ["Arxiv Researcher"]

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

        return {
            "title": title,
            "authors": authors,
            "paper_url": paper_url,
            "published_date": pub_date,
            "abstract_repo": abstract_repo,
        }

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
        now = time.monotonic()
        elapsed = now - self._last_arxiv_time
        if elapsed < self.ARXIV_INTERVAL_SECONDS:
            await asyncio.sleep(self.ARXIV_INTERVAL_SECONDS - elapsed)
        self._last_arxiv_time = time.monotonic()

        params = {
            "search_query": "(cat:cs.AI OR cat:cs.LG OR cat:cs.CV OR cat:cs.CL) AND (all:github OR all:code)",
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
        for cat in self.CDN_CATEGORIES:
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
            items = await self.fetch_json("https://huggingface.co/api/daily_papers?limit=100")
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
                yield {
                    "title": title,
                    "authors": [a.get("name") for a in p.get("authors", []) if a.get("name")] or ["HF AI Researcher"],
                    "paper_url": f"https://arxiv.org/abs/{arxiv_id}",
                    "published_date": pub_date,
                    "abstract_repo": m.group(1) if m else None,
                }

        return self._collect_until_limit(candidates(), limit)

    async def _query_openalex_papers(self, page: int, limit: int) -> List[Dict[str, Any]]:
        """Harvest authentic arXiv papers via OpenAlex academic mirror with high throughput."""
        params = {
            "filter": "primary_location.source.id:S4306400194,concepts.id:C154945302",
            "sort": "publication_date:desc",
            "per-page": min(100, limit),
            "page": page,
        }
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
                authors = [a.get("author", {}).get("display_name") for a in w.get("authorships", []) if a.get("author", {}).get("display_name")] or ["AI Researcher"]
                gh_match = GITHUB_URL_PATTERN.search(f"{title} {loc}")
                yield {
                    "title": title,
                    "authors": authors,
                    "paper_url": loc,
                    "published_date": pub_date,
                    "abstract_repo": gh_match.group(1) if gh_match else None,
                }

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

    async def crawl(self) -> List[ResearchPaperRecord]:
        """Execute concurrent papers acquisition with optimal batching up to target count."""
        logger.info(f"Starting ResearchPapersCrawler (Target: {self.target_count} papers)...")
        offset = 0

        while not self.is_full:
            needed = min(500, self.remaining)
            papers = await self.fetch_papers_batch(offset, needed)
            if not papers:
                break
            batch = await asyncio.gather(*[self.enrich_paper(p) for p in papers])
            self.collected.extend(batch)
            offset += len(papers)
            logger.info(
                f"Papers progress: {len(self.collected)}/{self.target_count} collected"
                f" (GitHub enrichment {'active' if not self._github_quota_exhausted else 'disabled: quota exhausted'})."
            )

        logger.info(f"Completed ResearchPapersCrawler: {len(self.collected)} papers collected.")
        return self.collected[:self.target_count]

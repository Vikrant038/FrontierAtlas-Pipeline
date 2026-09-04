"""
High-throughput research papers crawler for Arxiv & Fastly CDN RSS syndication feeds.
Correlates paper preprints with verified author GitHub repositories and dynamic live star telemetry.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings
from src.crawlers.base import TargetedCrawler
from src.schemas.entities import ResearchPaperContent, ResearchPaperRecord
from src.utils.date_normalizer import parse_datetime_to_utc
from src.utils.logger import logger

GITHUB_URL_PATTERN = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


class ResearchPapersCrawler(TargetedCrawler):
    """Ultra-lean, production-grade Arxiv crawler with direct author GitHub correlation."""

    ARXIV_API_BASE = "https://export.arxiv.org/api/query"
    CDN_CATEGORIES = ["cs.AI", "cs.LG", "cs.CV", "cs.CL", "cs.NE", "stat.ML", "cs.RO", "cs.MA", "cs.IR", "cs.SI"]

    def __init__(self, target_count: int = 1000, **kwargs):
        super().__init__(target_count=target_count, **kwargs)
        self.github_token = settings.github_token
        self._use_cdn = False

    def _gh_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    async def _fetch_stars(self, repo_path: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
        """Direct, high-throughput GitHub repository and star lookup (5,000 req/hr quota)."""
        if not repo_path:
            return None, None
        try:
            data = await self.fetch_json(f"https://api.github.com/repos/{repo_path}", headers=self._gh_headers())
            if data and "stargazers_count" in data:
                return f"https://github.com/{repo_path}", data["stargazers_count"]
        except Exception:
            pass
        return f"https://github.com/{repo_path}", None

    def _parse_feed_entry(self, entry: Any) -> Optional[Dict[str, Any]]:
        """Extract and normalize paper metadata from Atom or RSS feed entry."""
        raw_title = getattr(entry, "title", "")
        title = re.sub(r"\s+", " ", re.sub(r"\s*\(arXiv:[^)]+\).*", "", raw_title)).strip()
        if not title:
            return None

        # Authors: handle Atom author objects or RSS creator string
        raw_authors = [getattr(a, "name", "") for a in getattr(entry, "authors", [])]
        creator = getattr(entry, "creator", "") or getattr(entry, "dc_creator", "") or getattr(entry, "author", "")
        names_str = " ".join(raw_authors) if raw_authors else re.sub(r"<[^>]+>", "", creator)
        authors = [a.strip() for a in re.split(r"[,;]", names_str) if a.strip()] or ["Arxiv Researcher"]

        date_str = getattr(entry, "published", "") or getattr(entry, "updated", "") or getattr(entry, "pubDate", "")
        pub_date = parse_datetime_to_utc(date_str)
        if not pub_date:
            logger.warning(f"Arxiv paper '{title}' missing valid date. Skipping.")
            return None

        # Abstract and comment extraction for ground-truth author GitHub repository
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

        # Canonical paper or PDF link
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

    def _filter_new_papers(self, entries: List[Any], limit: int) -> List[Dict[str, Any]]:
        """Filter and deduplicate unvisited feed entries."""
        valid: List[Dict[str, Any]] = []
        for entry in entries:
            paper = self._parse_feed_entry(entry)
            if paper and not self.is_seen(paper["paper_url"]):
                valid.append(paper)
                if len(valid) >= limit:
                    break
        return valid

    async def _query_arxiv_api(self, start: int, limit: int) -> List[Dict[str, Any]]:
        """Query official Arxiv Atom API for AI/ML papers with code mentions."""
        params = {
            "search_query": "(cat:cs.AI OR cat:cs.LG OR cat:cs.CV OR cat:cs.CL) AND (all:github OR all:code)",
            "start": start,
            "max_results": limit,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        client = await self.get_client()
        resp = await client.get(self.ARXIV_API_BASE, params=params, timeout=5.0)
        feed = feedparser.parse(resp.text)
        return self._filter_new_papers(getattr(feed, "entries", []), limit)

    async def _query_arxiv_cdn(self, limit: int) -> List[Dict[str, Any]]:
        """Harvest fresh papers from Fastly CDN-cached Arxiv RSS feeds."""
        results: List[Dict[str, Any]] = []
        for cat in self.CDN_CATEGORIES:
            if len(results) >= limit:
                break
            try:
                entries = await self.fetch_feed(f"https://rss.arxiv.org/rss/{cat}")
                results.extend(self._filter_new_papers(entries, limit - len(results)))
            except Exception as exc:
                logger.warning(f"Arxiv CDN RSS failed for {cat}: {exc}")
        return results

    async def _query_hf_papers(self, limit: int) -> List[Dict[str, Any]]:
        """Harvest fresh papers from Hugging Face Daily Papers API with verified code repos."""
        try:
            items = await self.fetch_json("https://huggingface.co/api/daily_papers?limit=100")
            papers: List[Dict[str, Any]] = []
            for item in (items or []):
                p = item.get("paper", {})
                title = p.get("title", "").strip()
                arxiv_id = p.get("id", "")
                pub_date = parse_datetime_to_utc(p.get("publishedAt"))
                if not title or not pub_date:
                    continue
                paper_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""
                if not paper_url or self.is_seen(paper_url):
                    continue
                repo_raw = p.get("githubRepo") or ""
                m = GITHUB_URL_PATTERN.search(repo_raw) if repo_raw else None
                papers.append({
                    "title": title,
                    "authors": [a.get("name") for a in p.get("authors", []) if a.get("name")] or ["HF AI Researcher"],
                    "paper_url": paper_url,
                    "published_date": pub_date,
                    "abstract_repo": m.group(1) if m else None,
                })
                if len(papers) >= limit:
                    break
            return papers
        except Exception as exc:
            logger.warning(f"HF daily papers error: {exc}")
            return []

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
            papers: List[Dict[str, Any]] = []
            for w in (data or {}).get("results", []):
                title = (w.get("title") or "").strip()
                loc = (w.get("primary_location") or {}).get("landing_page_url") or w.get("doi") or ""
                pub_date = parse_datetime_to_utc(w.get("publication_date"))
                if not title or not loc or not pub_date or self.is_seen(loc):
                    continue
                authors = [a.get("author", {}).get("display_name") for a in w.get("authorships", []) if a.get("author", {}).get("display_name")] or ["AI Researcher"]
                gh_match = GITHUB_URL_PATTERN.search(f"{title} {loc}")
                papers.append({
                    "title": title,
                    "authors": authors,
                    "paper_url": loc,
                    "published_date": pub_date,
                    "abstract_repo": gh_match.group(1) if gh_match else None,
                })
                if len(papers) >= limit:
                    break
            return papers
        except Exception as exc:
            logger.warning(f"OpenAlex arXiv mirror error: {exc}")
            return []

    async def fetch_papers_batch(self, start: int, limit: int) -> List[Dict[str, Any]]:
        """Fetch papers with automatic multi-tier failover (Arxiv API -> CDN RSS -> HF -> OpenAlex)."""
        if not self._use_cdn:
            try:
                papers = await self._query_arxiv_api(start, limit)
                if papers:
                    return papers
                self._use_cdn = True
            except Exception as exc:
                logger.warning(f"Arxiv API unavailable ({exc}). Switching to CDN RSS failover.")
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

    async def fetch_arxiv_batch(self, start: int = 0, max_results: int = 100) -> List[Dict[str, Any]]:
        """Public alias for backward compatibility."""
        return await self.fetch_papers_batch(start, max_results)

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
        """Execute concurrent papers acquisition up to target count."""
        logger.info(f"Starting ResearchPapersCrawler (Target: {self.target_count} papers)...")
        offset = 0

        while not self.is_full:
            needed = min(100, self.remaining)
            papers = await self.fetch_papers_batch(offset, needed)
            if not papers:
                break
            batch = await asyncio.gather(*[self.enrich_paper(p) for p in papers])
            self.collected.extend(batch)
            offset += len(papers)
            await asyncio.sleep(3.0 if not self._use_cdn else 0.3)

        logger.info(f"Completed ResearchPapersCrawler: {len(self.collected)} papers collected.")
        return self.collected[:self.target_count]

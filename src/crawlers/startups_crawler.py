import asyncio
import time
from typing import AsyncIterator, List, Optional

from src.config import settings
from src.crawlers.base import TargetedCrawler, github_headers, is_github_quota_error
from src.schemas.entities import SourceMetadata, StartupContent, StartupContentData, StartupRecord
from src.resolution.normalizer import entity_resolver
from src.utils.logger import logger


class StartupsCrawler(TargetedCrawler):
    """Crawler for acquiring 1,000+ unique AI startups via Y Combinator, GitHub Orgs & Hugging Face."""

    YC_API_URL = "https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence"
    GITHUB_USERS_API = "https://api.github.com/search/users"
    HF_MODELS_API = "https://huggingface.co/api/models"  # ?limit= taken from HF_MODELS_LIMIT
    SEARCH_QUERIES = ["type:org ai", "type:org llm", 'type:org "machine learning"']

    CANONICAL_SEEDS = [
        ("OpenAI, Inc.", "https://openai.com"),
        ("Anthropic PBC", "https://anthropic.com"),
        ("Mistral AI SAS", "https://mistral.ai"),
        ("Stability AI Ltd", "https://stability.ai"),
        ("Hugging Face, Inc.", "https://huggingface.co"),
        ("Perplexity AI", "https://perplexity.ai"),
        ("eleven labs", "https://elevenlabs.io"),
        ("wandb", "https://wandb.ai"),
        ("pika", "https://pika.art"),
        ("Scale AI", "https://scale.com"),
        ("Cohere", "https://cohere.com"),
    ]

    def __init__(self, target_count: int = 1000, **kwargs):
        super().__init__(target_count=target_count, **kwargs)
        self._github_quota_blocked_until: float = 0.0

    def _github_blocked(self) -> bool:
        """True while the one-hour GitHub quota backoff window is active."""
        return time.monotonic() < self._github_quota_blocked_until

    async def _add_startup(
        self, raw_name: Optional[str], src_name: str, src_url: str, employee_count: Optional[int] = None
    ) -> bool:
        if not raw_name or self.is_full:
            return self.is_full
        canonical, _ = await entity_resolver.resolve_async(raw_name=raw_name, source_url=src_url, entity_type="STARTUP")
        return self.add(
            canonical,
            StartupRecord(
                source=SourceMetadata(name=src_name, url=src_url),
                content=StartupContent(entityName=canonical, data=StartupContentData(employeeCount=employee_count)),
            ),
        )


    async def _fetch_yc_page(self, page: int) -> List[tuple]:
        """Fetch a single YC companies page and extract startup tuples."""
        try:
            data = await self.fetch_json(f"{self.YC_API_URL}&page={page}")
        except Exception as exc:
            logger.warning(f"Error fetching YC companies (page {page}): {exc}")
            return []
        companies = (data or {}).get("companies", [])
        results = []
        for c in companies:
            team_size = c.get("teamSize")
            results.append((
                c.get("name"),
                "Y Combinator",
                c.get("website") or c.get("url") or f"https://www.ycombinator.com/companies/{c.get('slug', '')}",
                team_size if isinstance(team_size, int) and team_size >= 1 else None,
            ))
        return results

    async def _yc_candidates(self) -> AsyncIterator[tuple]:
        """Yield (name, source, url, employee_count) candidates from Y Combinator pages."""
        # Check page 1 first to respect tests and avoid querying empty endpoints
        p1_candidates = await self._fetch_yc_page(1)
        for cand in p1_candidates:
            yield cand
        if not p1_candidates:
            return

        # Fetch subsequent pages in parallel batches (bounded by YC_PARALLEL_PAGES)
        sem = asyncio.Semaphore(settings.yc_parallel_pages)

        async def _bounded_fetch(p: int) -> List[tuple]:
            async with sem:
                return await self._fetch_yc_page(p)

        for batch_start in range(settings.yc_start_page, settings.yc_max_page, settings.yc_parallel_pages):
            batch_pages = range(batch_start, min(batch_start + settings.yc_parallel_pages, settings.yc_max_page))
            results = await asyncio.gather(*(_bounded_fetch(p) for p in batch_pages))
            for page_candidates in results:
                for cand in page_candidates:
                    yield cand

    async def _hf_candidates(self) -> AsyncIterator[tuple]:
        """Yield (name, source, url, employee_count) candidates from Hugging Face model orgs."""
        try:
            hf_models = await self.fetch_json(f"{self.HF_MODELS_API}?limit={settings.hf_models_limit}")
        except Exception as exc:
            logger.warning(f"Error fetching HF models orgs: {exc}")
            return
        for m in (hf_models or []):
            if "/" in m.get("id", ""):
                org = m["id"].split("/")[0]
                yield org, "Hugging Face", f"https://huggingface.co/{org}", None

    async def _github_candidates(self) -> AsyncIterator[tuple]:
        """Yield (name, source, url, employee_count) candidates from GitHub AI org search."""
        for query in self.SEARCH_QUERIES:
            if self._github_blocked():
                break
            for page in range(1, settings.github_search_pages + 1):
                try:
                    data = await self.fetch_json(
                        self.GITHUB_USERS_API,
                        params={"q": query, "per_page": 100, "page": page},
                        headers=github_headers(self._pick_github_token(f"{query}:{page}")),
                        allow_tls_fallback=False,
                    )
                except Exception as exc:
                    logger.warning(f"Error fetching GitHub orgs for '{query}' (page {page}): {exc}")
                    if is_github_quota_error(exc):
                        # Drop the exhausted token; pause only when every token is gone,
                        # instead of burning 27 doomed requests (and browser escalations).
                        token = self._pick_github_token(f"{query}:{page}")
                        if token:
                            self._exhausted_github_tokens.add(token)
                        if self._pick_github_token("") is None:
                            self._github_quota_blocked_until = time.monotonic() + 3600.0
                            logger.warning("All GitHub tokens exhausted; stopping GitHub org scan for an hour.")
                        break
                    continue
                items = (data or {}).get("items", [])
                if not items:
                    break
                for item in items:
                    yield item.get("login"), "GitHub AI Orgs", item.get("html_url", ""), None

    async def _harvest_candidates(self, candidates: AsyncIterator[tuple]) -> None:
        """Add startup candidates until the target quota is reached."""
        async for raw_name, src_name, src_url, emp in candidates:
            if await self._add_startup(raw_name, src_name, src_url, employee_count=emp):
                return

    async def crawl(self) -> List[StartupRecord]:
        """Execute multi-source AI startups collection up to target count."""
        logger.info(f"Starting StartupsCrawler (Target: {self.target_count})...")
        recovered = self.recover_from_wal(model_cls=StartupRecord)
        if recovered:
            logger.info(f"Resumed {recovered} startups from WAL; continuing toward {self.target_count}.")
        for raw_name, url in self.CANONICAL_SEEDS:
            if await self._add_startup(raw_name, "Canonical Seed List (Internal)", url):
                break
        if not self.is_full:
            # Harvest all three sources concurrently; quota plumbing keeps the total at target.
            await asyncio.gather(
                self._harvest_candidates(self._yc_candidates()),
                self._harvest_candidates(self._hf_candidates()),
                self._harvest_candidates(self._github_candidates()),
            )
        self.reset_wal_if_complete()
        logger.info(f"Completed StartupsCrawler: {len(self.collected)} startups collected.")
        return self.collected[:self.target_count]



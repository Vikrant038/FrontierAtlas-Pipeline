from typing import List, Optional

from src.config import settings
from src.crawlers.base import TargetedCrawler
from src.schemas.entities import SourceMetadata, StartupContent, StartupContentData, StartupRecord
from src.resolution.normalizer import entity_resolver
from src.utils.logger import logger


class StartupsCrawler(TargetedCrawler):
    """Crawler for acquiring 1,000+ unique AI startups via Y Combinator, GitHub Orgs & Hugging Face."""

    YC_API_URL = "https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence"
    GITHUB_USERS_API = "https://api.github.com/search/users"
    HF_MODELS_API = "https://huggingface.co/api/models?limit=1000"
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
        self.github_token = settings.github_token

    def _add_startup(
        self, raw_name: Optional[str], src_name: str, src_url: str, employee_count: Optional[int] = None
    ) -> bool:
        if not raw_name or self.is_full:
            return self.is_full
        canonical, _ = entity_resolver.resolve(raw_name=raw_name, source_url=src_url, entity_type="STARTUP")
        return self.add(
            canonical,
            StartupRecord(
                source=SourceMetadata(name=src_name, url=src_url),
                content=StartupContent(entityName=canonical, data=StartupContentData(employeeCount=employee_count)),
            ),
        )

    def _harvest_seeds(self) -> None:
        """Harvest foundational AI startups with zero fabricated employee counts."""
        for raw_name, url in self.CANONICAL_SEEDS:
            if self._add_startup(raw_name, "Canonical Seed List (Internal)", url, employee_count=None):
                break

    async def _harvest_yc(self) -> None:
        """Harvest Y Combinator AI companies with authentic teamSize metadata."""
        for page in range(1, 25):
            if self.is_full:
                break
            try:
                data = await self.fetch_json(f"{self.YC_API_URL}&page={page}")
                companies = (data or {}).get("companies", [])
                if not companies:
                    break
                for c in companies:
                    name = c.get("name")
                    url = c.get("website") or c.get("url") or f"https://www.ycombinator.com/companies/{c.get('slug', '')}"
                    team_size = c.get("teamSize")
                    emp_count = team_size if (isinstance(team_size, int) and team_size >= 1) else None
                    if self._add_startup(name, "Y Combinator", url, employee_count=emp_count):
                        return
            except Exception as exc:
                logger.warning(f"Error fetching YC companies (page {page}): {exc}")
                break

    async def _harvest_hf(self) -> None:
        """Harvest leading AI organizations from Hugging Face model creators."""
        try:
            hf_models = await self.fetch_json(self.HF_MODELS_API)
            for m in (hf_models or []):
                if "/" in m.get("id", ""):
                    org = m["id"].split("/")[0]
                    if self._add_startup(org, "Hugging Face", f"https://huggingface.co/{org}"):
                        return
        except Exception as exc:
            logger.warning(f"Error fetching HF models orgs: {exc}")

    async def _harvest_github(self) -> None:
        """Harvest AI organizations from GitHub search API."""
        headers = {"Accept": "application/vnd.github+json"}
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"

        for query in self.SEARCH_QUERIES:
            for page in range(1, 11):
                if self.is_full:
                    return
                try:
                    data = await self.fetch_json(
                        self.GITHUB_USERS_API,
                        params={"q": query, "per_page": 100, "page": page},
                        headers=headers,
                    )
                    items = (data or {}).get("items", [])
                    if not items:
                        break
                    for item in items:
                        if self._add_startup(item.get("login"), "GitHub AI Orgs", item.get("html_url", "")):
                            return
                except Exception as exc:
                    logger.warning(f"Error fetching GitHub orgs for '{query}': {exc}")
                    break

    async def crawl(self) -> List[StartupRecord]:
        """Execute multi-source AI startups collection up to target count."""
        logger.info(f"Starting StartupsCrawler (Target: {self.target_count})...")
        self._harvest_seeds()
        if not self.is_full:
            await self._harvest_yc()
        if not self.is_full:
            await self._harvest_hf()
        if not self.is_full:
            await self._harvest_github()
        logger.info(f"Completed StartupsCrawler: {len(self.collected)} startups collected.")
        return self.collected[:self.target_count]



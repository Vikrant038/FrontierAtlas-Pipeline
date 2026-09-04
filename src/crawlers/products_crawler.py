"""
Products crawler for acquiring 1,000+ AI products from clean curated markdown directories.
Guarantees 0 list anchors, authentic product destination URLs, and grounded pricing classification.
"""

import asyncio
import json
import re
from typing import Dict, List, Optional, Tuple

from src.config import settings
from src.crawlers.base import TargetedCrawler
from src.llm.fallback_chain import llm_engine
from src.llm.prompts import PRODUCT_PRICING_PROMPT, ProductPricingSchema
from src.llm.rules import classify_pricing_by_keywords
from src.resolution.normalizer import entity_resolver, extract_domain
from src.schemas.entities import PricingModelEnum, ProductContent, ProductRecord, SourceMetadata
from src.utils.logger import logger

KNOWN_PRICING: Dict[str, PricingModelEnum] = {
    "openai api": PricingModelEnum.PAID,
    "claude code": PricingModelEnum.PAID,
    "github copilot": PricingModelEnum.PAID,
    "midjourney": PricingModelEnum.PAID,
    "chatgpt": PricingModelEnum.FREEMIUM,
    "claude": PricingModelEnum.FREEMIUM,
    "perplexity ai": PricingModelEnum.FREEMIUM,
    "autogpt": PricingModelEnum.FREE,
    "babyagi": PricingModelEnum.FREE,
    "langchain": PricingModelEnum.FREE,
    "llamaindex": PricingModelEnum.FREE,
    "vllm": PricingModelEnum.FREE,
    "ollama": PricingModelEnum.FREE,
}


def _known_pricing(name: str) -> Optional[PricingModelEnum]:
    """Return the KNOWN_PRICING override for a product name, or None."""
    return KNOWN_PRICING.get((name or "").strip().lower())


class ProductsCrawler(TargetedCrawler):
    """Crawler for acquiring 1,000+ unique AI products from clean curated markdown directories."""

    SOURCES: List[Tuple[str, str]] = [
        ("Awesome Generative AI", "https://raw.githubusercontent.com/steven2358/awesome-generative-ai/main/README.md"),
        ("Awesome AI Tools", "https://raw.githubusercontent.com/mahseema/awesome-ai-tools/main/README.md"),
        ("Awesome AI Agents", "https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md"),
        ("Awesome Production ML", "https://raw.githubusercontent.com/EthicalML/awesome-production-machine-learning/master/README.md"),
    ]

    def __init__(self, target_count: int = 1000, **kwargs):
        super().__init__(target_count=target_count, **kwargs)

    def _configured_sources(self) -> List[Tuple[str, str]]:
        """Resolve product sources: PRODUCT_SOURCES_JSON override (list of [name, url])
        or the built-in SOURCES table. Uses self.SOURCES so tests can patch it."""
        override = settings._parse_json_list(settings.product_sources_json, [])
        if override:
            normalized = []
            for s in override:
                if isinstance(s, dict):
                    normalized.append((s.get("name") or "", s.get("url") or ""))
                elif isinstance(s, list) and len(s) >= 2:
                    normalized.append((s[0], s[1]))
            if normalized:
                return normalized
        return self.SOURCES

    @staticmethod
    def classify_pricing(name: str, url: str, desc: str) -> PricingModelEnum:
        """Classify pricing model using known-product overrides, then shared keyword tiers."""
        known = _known_pricing(name)
        return known if known is not None else classify_pricing_by_keywords(name, url, desc)

    async def classify_pricing_async(self, name: str, url: str, desc: str) -> PricingModelEnum:
        """Classify pricing via LLM when a description exists, else known-override/keyword rules."""
        known = _known_pricing(name)
        if known is not None:
            return known

        desc_clean = (desc or "").strip()
        if len(desc_clean) >= 15:
            try:
                input_text = f"Product Name: {name}\nURL: {url}\nDescription: {desc_clean}"
                llm_out = await llm_engine.extract_structured(
                    raw_text=input_text,
                    schema_cls=ProductPricingSchema,
                    instruction=PRODUCT_PRICING_PROMPT,
                )
                if llm_out and isinstance(llm_out.pricingModel, PricingModelEnum):
                    return llm_out.pricingModel
            except Exception as exc:
                logger.debug(f"LLM pricing classification fallback for '{name}': {exc}")

        return self.classify_pricing(name, url, desc)

    @staticmethod
    def _extract_github_owner(url: str) -> Optional[str]:
        """Extract owner or organization from GitHub repository URL."""
        if "github.com/" in url:
            gh_m = re.search(r"github\.com/([^/\s?#]+)", url)
            if gh_m:
                owner = gh_m.group(1).strip()
                if owner and owner.lower() not in (
                    "topics", "trending", "features", "explore", "pricing",
                    "collections", "events", "readme", "about", "site"
                ):
                    return owner
        return None

    @classmethod
    def _get_raw_maker(cls, name: str, desc: str, url: str) -> str:
        m = re.search(r"\b(?:by|from)\s+([A-Z][A-Za-z0-9\s&.-]{1,30}?)(?:\s+(?:is|has|was|provides|\.|\,)|$)", desc)
        raw_maker = m.group(1).strip() if m else name
        # Sentence-like fallbacks (essay titles, citations) are not companies:
        # derive the org from GitHub repo owner or the product URL domain instead.
        if len(raw_maker.split()) > 5 or raw_maker.endswith((".", "!", "?")) or raw_maker.lower() in ("github", "github.com"):
            gh_owner = cls._extract_github_owner(url)
            if gh_owner:
                return gh_owner
            domain = extract_domain(url)
            if domain and domain.lower() not in ("github.com", "github"):
                raw_maker = domain.split(".")[0].capitalize()
            else:
                raw_maker = name
        return raw_maker

    @classmethod
    def _extract_maker(cls, name: str, desc: str, url: str) -> str:
        raw_maker = cls._get_raw_maker(name, desc, url)
        return entity_resolver.resolve(raw_name=raw_maker, source_url=url, entity_type="STARTUP")[0]

    @classmethod
    async def _extract_maker_async(cls, name: str, desc: str, url: str) -> str:
        raw_maker = cls._get_raw_maker(name, desc, url)
        canonical, _ = await entity_resolver.resolve_async(raw_name=raw_maker, source_url=url, entity_type="STARTUP")
        return canonical

    def _parse_awesome_agents(self, text: str) -> List[Tuple[str, str, str]]:
        """Parse structured '## [Product Name](URL)' sections from Awesome AI Agents."""
        items: List[Tuple[str, str, str]] = []
        sections = re.split(r"\n##\s+\[", text)
        for sec in sections[1:]:
            m = re.match(r"([^\]]+)\]\((https?://[^)#]+)\)(.*)", sec, re.DOTALL)
            if not m:
                continue
            name = m.group(1).strip()
            url = m.group(2).strip()
            rest = m.group(3).strip()

            web_m = re.search(r"-\s+\[(?:Web|Website|Home)\]\((https?://[^)#]+)\)", rest)
            if web_m:
                url = web_m.group(1).strip()

            desc_m = re.search(r"###\s+Description\s*\n(.*?)(?=\n###|\n</details>|$)", rest, re.DOTALL)
            tagline = rest.splitlines()[0] if rest.splitlines() else ""
            desc = (desc_m.group(1).strip() if desc_m else tagline)[:200]
            if not url.endswith((".png", ".svg", ".jpg", ".md")) and "#" not in url:
                items.append((name, url, desc))
        return items

    def _parse_markdown_list(self, text: str) -> List[Tuple[str, str, str]]:
        """Parse bulleted tool lists from standard markdown directories."""
        items: List[Tuple[str, str, str]] = []
        for m in re.finditer(r"^[\*\-]\s+\[([^\]]+)\]\((https?://[^)#]+)\)\s*(?:-\s*(.*))?", text, re.MULTILINE):
            name = m.group(1).strip()
            url = m.group(2).strip()
            desc = (m.group(3) or "").strip()
            if "awesome.re" in url or url.endswith((".md", ".png", ".jpg", ".svg")) or "#" in url:
                continue
            items.append((name, url, desc))
        return items

    async def _process_item(
        self, src_name: str, name: str, item_url: str, desc: str, sem: asyncio.Semaphore
    ) -> Optional[ProductRecord]:
        """Classify pricing and construct ProductRecord within concurrency limit."""
        async with sem:
            try:
                pricing = await self.classify_pricing_async(name, item_url, desc)
                maker = await self._extract_maker_async(name, desc, item_url)
                return ProductRecord(
                    source=SourceMetadata(name=src_name, url=item_url),
                    content=ProductContent(
                        startupName=maker,
                        productName=name,
                        productUrl=item_url,
                        pricingModel=pricing,
                    ),
                )
            except Exception as exc:
                logger.debug(f"Failed to process product '{name}': {exc}")
                return None

    async def crawl(self) -> List[ProductRecord]:
        """Collect 1,000+ unique AI products from clean curated markdown directories."""
        logger.info(f"Starting ProductsCrawler (Target: {self.target_count})...")
        recovered = self.recover_from_wal(model_cls=ProductRecord)
        if recovered:
            logger.info(f"Resumed {recovered} products from WAL; continuing toward {self.target_count}.")
        sources = self._configured_sources()
        # Fetch all source documents concurrently (independent downloads), then process each.
        fetched = await asyncio.gather(*(self.fetch(url) for _, url in sources), return_exceptions=True)

        for (src_name, url), content in zip(sources, fetched):
            if self.is_full:
                break
            if isinstance(content, Exception):
                logger.warning(f"Error crawling products from {src_name}: {repr(content)}")
                continue
            try:
                if "awesome-ai-agents" in url:
                    items = self._parse_awesome_agents(content)
                else:
                    items = self._parse_markdown_list(content)

                candidates = []
                for name, item_url, desc in items:
                    if not name or "#" in item_url or self.is_full:
                        continue
                    candidates.append((name, item_url, desc))
                    if len(candidates) >= self.remaining:
                        break

                sem = asyncio.Semaphore(settings.products_concurrency)
                tasks = [self._process_item(src_name, n, u, d, sem) for n, u, d in candidates]
                records = await asyncio.gather(*tasks)
                for rec in records:
                    if rec:
                        # add() registers the dedup key only on success, so failed
                        # extractions are not consumed and may be retried later.
                        self.add(rec.content.productName, rec)
            except Exception as exc:
                logger.warning(f"Error crawling products from {src_name}: {repr(exc)}")

        self.reset_wal_if_complete()
        logger.info(f"Completed ProductsCrawler: {len(self.collected)} products collected.")
        return self.collected[:self.target_count]

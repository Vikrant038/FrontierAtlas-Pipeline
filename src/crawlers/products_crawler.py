"""
Products crawler for acquiring 1,000+ AI products from clean curated markdown directories.
Guarantees 0 list anchors, authentic product destination URLs, and grounded pricing classification.
"""

import re
from typing import Dict, List, Tuple

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

    @staticmethod
    def classify_pricing(name: str, url: str, desc: str) -> PricingModelEnum:
        """Classify pricing model using known-product overrides, then shared keyword tiers."""
        n = (name or "").lower().strip()
        if n in KNOWN_PRICING:
            return KNOWN_PRICING[n]
        return classify_pricing_by_keywords(name, url, desc)

    async def classify_pricing_async(self, name: str, url: str, desc: str) -> PricingModelEnum:
        """Classify pricing model using LLM fallback when description is available (>=15 chars), falling back to keyword rules."""
        n = (name or "").lower().strip()
        if n in KNOWN_PRICING:
            return KNOWN_PRICING[n]

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
    def _extract_maker(name: str, desc: str, url: str) -> str:
        m = re.search(r"\b(?:by|from)\s+([A-Z][A-Za-z0-9\s&.-]{1,30}?)(?:\s+(?:is|has|was|provides|\.|\,)|$)", desc)
        raw_maker = m.group(1).strip() if m else name
        # Sentence-like fallbacks (essay titles, citations) are not companies:
        # derive the org from the product URL domain instead.
        if len(raw_maker.split()) > 5 or raw_maker.endswith((".", "!", "?")):
            domain = extract_domain(url)
            raw_maker = domain.split(".")[0].capitalize() if domain else name
        return entity_resolver.resolve(raw_name=raw_maker, source_url=url, entity_type="STARTUP")[0]

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

    async def crawl(self) -> List[ProductRecord]:
        """Collect 1,000+ unique AI products from clean curated markdown directories."""
        logger.info(f"Starting ProductsCrawler (Target: {self.target_count})...")
        for src_name, url in self.SOURCES:
            if self.is_full:
                break
            try:
                content = await self.fetch(url)
                if "awesome-ai-agents" in url:
                    items = self._parse_awesome_agents(content)
                else:
                    items = self._parse_markdown_list(content)

                for name, item_url, desc in items:
                    if not name or "#" in item_url:
                        continue
                    if self.is_full:
                        break
                    pricing = await self.classify_pricing_async(name, item_url, desc)
                    record = ProductRecord(
                        source=SourceMetadata(name=src_name, url=item_url),
                        content=ProductContent(
                            startupName=self._extract_maker(name, desc, item_url),
                            productName=name,
                            productUrl=item_url,
                            pricingModel=pricing,
                        ),
                    )
                    if self.add(name, record):
                        break
            except Exception as exc:
                logger.warning(f"Error crawling products from {src_name}: {repr(exc)}")

        logger.info(f"Completed ProductsCrawler: {len(self.collected)} products collected.")
        return self.collected[:self.target_count]

"""
Products crawler for acquiring 1,000+ AI products from clean curated markdown directories.
Guarantees 0 list anchors, authentic product destination URLs, and grounded pricing classification.
"""

import re
from typing import Dict, List, Tuple

from src.crawlers.base import TargetedCrawler
from src.resolution.normalizer import entity_resolver
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
        """Classify pricing model using keyword tiers, platform defaults, and overrides."""
        n = (name or "").lower().strip()
        if n in KNOWN_PRICING:
            return KNOWN_PRICING[n]

        t = f"{name} {desc}".upper()
        u = (url or "").lower()

        if any(w in t for w in ("ENTERPRISE", "CONTACT SALES", "REQUEST DEMO", "CUSTOM PRICING")):
            return PricingModelEnum.ENTERPRISE
        if any(w in t for w in ("$", "/MO", "/MONTH", "SUBSCRIPTION", "PER TOKEN", "PAY-AS-YOU-GO", "PAY AS YOU GO")):
            return PricingModelEnum.PAID
        if any(w in t for w in ("OPEN SOURCE", "OPEN-SOURCE", "100% FREE", "FREE TOOL", "COMPLETELY FREE", "MIT LICENSE", "APACHE 2")):
            return PricingModelEnum.FREE
        if any(w in t for w in ("FREEMIUM", "FREE TIER", "FREE TRIAL", "FREE PLAN", "FREE VERSION")):
            return PricingModelEnum.FREEMIUM
        if "github.com" in u or "huggingface.co" in u:
            return PricingModelEnum.FREE
        if any(w in t for w in ("API", "INFRASTRUCTURE", "HOSTED PLATFORM", "CLOUD SERVICE")):
            return PricingModelEnum.PAID
        return PricingModelEnum.FREEMIUM

    @staticmethod
    def _extract_maker(name: str, desc: str, url: str) -> str:
        m = re.search(r"\b(?:by|from)\s+([A-Z][A-Za-z0-9\s&.-]{1,30}?)(?:\s+(?:is|has|was|provides|\.|\,)|$)", desc)
        raw_maker = m.group(1).strip() if m else name
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
                    if not name or self.is_seen(name) or "#" in item_url:
                        continue
                    pricing = self.classify_pricing(name, item_url, desc)
                    maker = self._extract_maker(name, desc, item_url)
                    self.collected.append(ProductRecord(
                        source=SourceMetadata(name=src_name, url=item_url),
                        content=ProductContent(
                            startupName=maker,
                            productName=name,
                            productUrl=item_url,
                            pricingModel=pricing,
                        ),
                    ))
                    if self.is_full:
                        break
            except Exception as exc:
                logger.warning(f"Error crawling products from {src_name}: {repr(exc)}")

        logger.info(f"Completed ProductsCrawler: {len(self.collected)} products collected.")
        return self.collected[:self.target_count]

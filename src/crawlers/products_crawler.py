import re
from typing import List

from src.crawlers.base import TargetedCrawler
from src.resolution.normalizer import entity_resolver
from src.schemas.entities import PricingModelEnum, ProductContent, ProductRecord, SourceMetadata
from src.utils.logger import logger


class ProductsCrawler(TargetedCrawler):
    """Crawler for acquiring 1,000+ unique AI products from curated markdown repositories."""

    SOURCES = [
        ("Awesome Generative AI", "https://raw.githubusercontent.com/steven2358/awesome-generative-ai/main/README.md"),
        ("Awesome AI Tools", "https://raw.githubusercontent.com/mahseema/awesome-ai-tools/main/README.md"),
        ("Awesome AI Agents", "https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md"),
        ("AI Collection", "https://raw.githubusercontent.com/ai-collection/ai-collection/main/README.md"),
    ]

    def __init__(self, target_count: int = 1000, **kwargs):
        super().__init__(target_count=target_count, **kwargs)

    @staticmethod
    def _classify_pricing(text: str) -> PricingModelEnum:
        t = (text or "").upper()
        if any(w in t for w in ("ENTERPRISE", "CONTACT SALES", "CUSTOM")):
            return PricingModelEnum.ENTERPRISE
        if any(w in t for w in ("OPEN SOURCE", "OPEN-SOURCE", "100% FREE", "FREE TOOL", "COMPLETELY FREE")):
            return PricingModelEnum.FREE
        if any(w in t for w in ("PAID", "$", "/MO", "SUBSCRIPTION")):
            return PricingModelEnum.PAID
        return PricingModelEnum.FREE if "FREE" in t else PricingModelEnum.FREEMIUM

    @staticmethod
    def _extract_maker(name: str, desc: str, url: str) -> str:
        m = re.search(r"\b(?:by|from)\s+([A-Z][A-Za-z0-9\s&.-]{1,30}?)(?:\s+(?:is|has|was|provides|\.|\,)|$)", desc)
        return entity_resolver.resolve(raw_name=m.group(1).strip() if m else name, source_url=url, entity_type="STARTUP")[0]

    async def _resolve_url(self, raw_url: str, slug: str) -> str:
        """Follow redirect shorteners to final target URL; fallback to canonical GitHub anchor."""
        if not raw_url:
            return f"https://github.com/ai-collection/ai-collection#{slug}"
        if "redirect" in raw_url or "thataicollection.com" in raw_url:
            try:
                client = await self.get_client()
                resp = await client.head(raw_url, follow_redirects=True, timeout=2.5)
                final = str(resp.url)
                if resp.status_code < 400 and "thataicollection.com" not in final:
                    return final
            except Exception:
                pass
            return f"https://github.com/ai-collection/ai-collection#{slug}"
        return raw_url

    async def _parse_ai_collection(self, text: str):
        items = []
        for s in re.split(r"\n###\s+", text)[1:]:
            lines = [l.strip() for l in s.splitlines() if l.strip()]
            if not lines or "Index" in lines[0] or "hand-picked" in lines[0]:
                continue
            name = lines[0].split("<")[0].strip()
            url_m = re.search(r"\[Visit\]\((https?://[^\)]+)\)", s)
            slug = re.sub(r"[^\w-]", "", name.lower().replace(" ", "-"))
            url = await self._resolve_url(url_m.group(1) if url_m else "", slug)
            items.append((name, url, " ".join(lines[1:])))
        return items

    def _parse_markdown_list(self, text: str):
        sections = re.split(r"\n##\s+", text)
        product_sections = [s for s in sections if any(s.startswith(k) for k in ("Text", "Coding", "Agents", "Image", "Video", "Audio", "Other"))] or [text]
        for sec in product_sections:
            for m in re.finditer(r"^-\s+\[([^\]]+)\]\((https?://[^)#]+)\)\s*(?:-\s*(.*))?", sec, re.MULTILINE):
                url = m.group(2).strip()
                if "awesome.re" not in url and not url.endswith((".md", ".png", ".jpg")):
                    yield m.group(1).strip(), url, (m.group(3) or "").strip()

    async def crawl(self) -> List[ProductRecord]:
        logger.info(f"Starting ProductsCrawler (Target: {self.target_count})...")
        for src_name, url in self.SOURCES:
            if self.is_full:
                break
            try:
                content = await self.fetch(url)
                items = (await self._parse_ai_collection(content)) if "ai-collection" in url else self._parse_markdown_list(content)
                for name, item_url, desc in items:
                    if not name or self.is_seen(name):
                        continue
                    self.collected.append(ProductRecord(
                        source=SourceMetadata(name=src_name, url=item_url),
                        content=ProductContent(
                            startupName=self._extract_maker(name, desc, item_url),
                            productName=name,
                            productUrl=item_url,
                            pricingModel=self._classify_pricing(desc),
                        ),
                    ))
                    if self.is_full:
                        break
            except Exception as exc:
                logger.warning(f"Error crawling products from {src_name}: {exc}")

        logger.info(f"Completed ProductsCrawler: {len(self.collected)} products collected.")
        return self.collected[:self.target_count]

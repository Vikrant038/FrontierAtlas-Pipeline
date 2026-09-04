"""
Unit tests for ProductsCrawler markdown repository parsing and pricing classification.
Follows AAA pattern with offline respx mocking per CODING_STANDARDS.md.
"""

from pathlib import Path
import pytest
import respx
import httpx

from src.crawlers.products_crawler import ProductsCrawler
from src.schemas.entities import PricingModelEnum


def test_products_crawler_pricing_classification():
    # Arrange & Act & Assert
    assert ProductsCrawler._classify_pricing("Custom pricing for enterprise teams") == PricingModelEnum.ENTERPRISE
    assert ProductsCrawler._classify_pricing("Open source tool licensed under MIT") == PricingModelEnum.FREE
    assert ProductsCrawler._classify_pricing("Plans start at $20/mo subscription") == PricingModelEnum.PAID
    assert ProductsCrawler._classify_pricing("Free tier available with upgrades") == PricingModelEnum.FREE
    assert ProductsCrawler._classify_pricing("Cloud hosted AI copilot") == PricingModelEnum.FREEMIUM


def test_products_crawler_maker_extraction():
    # Arrange
    desc = "Conversational search engine by Perplexity AI."
    url = "https://perplexity.ai"

    # Act
    maker = ProductsCrawler._extract_maker("Perplexity", desc, url)

    # Assert
    assert maker == "Perplexity AI"


@pytest.mark.asyncio
@respx.mock
async def test_products_crawler_markdown_parsing_sections():
    # Arrange
    sample_markdown = """# Awesome Generative AI
## Contents
## Text
- [Claude](https://claude.ai) - Conversational AI by Anthropic.
## Image
- [Midjourney](https://midjourney.com) - Text-to-image generator.
- [awesome.re](https://awesome.re) - Badge link to ignore.
"""
    crawler = ProductsCrawler(target_count=2)
    crawler.SOURCES = [("Awesome GenAI", "https://example.com/genai.md")]
    respx.get("https://example.com/genai.md").mock(return_value=httpx.Response(200, text=sample_markdown))

    # Act
    products = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(products) == 2
    assert products[0].content.productName == "Claude"
    assert products[0].content.startupName == "Anthropic"
    assert products[1].content.productName == "Midjourney"


@pytest.mark.asyncio
@respx.mock
async def test_products_parser_against_real_ai_collection_fixture():
    # Arrange
    fixture_path = Path(__file__).parent / "fixtures" / "ai_collection_snapshot.md"
    content = fixture_path.read_text(encoding="utf-8")

    crawler = ProductsCrawler(target_count=10)
    crawler.SOURCES = [("AI Collection Fixture", "https://raw.githubusercontent.com/ai-collection/ai-collection/main/README.md")]
    respx.get("https://raw.githubusercontent.com/ai-collection/ai-collection/main/README.md").mock(
        return_value=httpx.Response(200, text=content)
    )

    # Act
    products = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(products) == 4
    names = [p.content.productName for p in products]
    assert "GPT-Zero" in names
    assert "Stable Diffusion XL" in names
    assert "Claude Enterprise" in names
    assert "Jasper AI" in names

    # Verify pricing model classifications
    sd_xl = next(p for p in products if p.content.productName == "Stable Diffusion XL")
    assert sd_xl.content.pricingModel == PricingModelEnum.FREE

    claude = next(p for p in products if p.content.productName == "Claude Enterprise")
    assert claude.content.pricingModel == PricingModelEnum.ENTERPRISE

    jasper = next(p for p in products if p.content.productName == "Jasper AI")
    assert jasper.content.pricingModel == PricingModelEnum.PAID

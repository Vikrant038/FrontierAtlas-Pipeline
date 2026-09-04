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
    assert ProductsCrawler.classify_pricing("Custom", "https://custom.com", "Custom pricing for enterprise teams") == PricingModelEnum.ENTERPRISE
    assert ProductsCrawler.classify_pricing("Tool", "https://tool.com", "Open source tool licensed under MIT") == PricingModelEnum.FREE
    assert ProductsCrawler.classify_pricing("Plan", "https://plan.com", "Plans start at $20/mo subscription") == PricingModelEnum.PAID
    assert ProductsCrawler.classify_pricing("App", "https://app.com", "Free tier available with upgrades") == PricingModelEnum.FREEMIUM
    assert ProductsCrawler.classify_pricing("GitHub Tool", "https://github.com/org/repo", "Cloud hosted AI copilot") == PricingModelEnum.FREE
    assert ProductsCrawler.classify_pricing("OpenAI API", "https://openai.com/api/", "Developer API platform") == PricingModelEnum.PAID


def test_products_crawler_maker_extraction():
    # Arrange
    desc = "Conversational search engine by Perplexity AI."
    url = "https://perplexity.ai"

    # Act
    maker = ProductsCrawler._extract_maker("Perplexity", desc, url)

    # Assert
    assert maker == "Perplexity AI"


def test_products_crawler_github_maker_attribution():
    # Arrange: Sentence-like tool name with GitHub repository URL
    name = "A Highly Scalable LLM Inference System for GPUs"
    desc = "High-throughput and memory-efficient LLM serving engine."
    gh_url = "https://github.com/vllm-project/vllm"

    # Act
    maker = ProductsCrawler._extract_maker(name, desc, gh_url)

    # Assert: Should attribute to vllm-project, NEVER to 'Github'
    assert maker != "Github"
    assert "vllm" in maker.lower()

    # Direct test of _extract_github_owner
    assert ProductsCrawler._extract_github_owner("https://github.com/microsoft/autogen") == "microsoft"
    assert ProductsCrawler._extract_github_owner("https://github.com/topics/ai") is None


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
async def test_awesome_ai_agents_header_parsing():
    # Arrange
    sample_agents = """# Awesome AI Agents
## [AgentForge](https://github.com/DataBassGit/AgentForge)
LLM-agnostic platform for agent building & testing

<details>
### Description
- A low-code framework for autonomous agents.
### Links
- [GitHub](https://github.com/DataBassGit/AgentForge)
- [Web](https://www.agentforge.net/)
</details>

## [AgentGPT](https://agentgpt.reworkd.ai/)
Browser-based autonomous agent
"""
    crawler = ProductsCrawler(target_count=2)
    crawler.SOURCES = [("Awesome AI Agents", "https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md")]
    respx.get("https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md").mock(
        return_value=httpx.Response(200, text=sample_agents)
    )

    # Act
    products = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(products) == 2
    assert products[0].content.productName == "AgentForge"
    assert products[0].content.productUrl == "https://www.agentforge.net/"
    assert products[1].content.productName == "AgentGPT"
    assert products[1].content.productUrl == "https://agentgpt.reworkd.ai/"

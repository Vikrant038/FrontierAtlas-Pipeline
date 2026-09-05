"""
Unit tests for ProductsCrawler markdown repository parsing and pricing classification.
Follows AAA pattern with offline respx mocking per CODING_STANDARDS.md.
"""

import asyncio

import pytest
import respx
import httpx

from src.crawlers.products_crawler import ProductsCrawler
from src.schemas.entities import (
    PricingModelEnum,
    ProductContent,
    ProductRecord,
    SourceMetadata,
)


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


# ---------------------------------------------------------------------------
# Tail branch coverage: source overrides, pricing classification edges,
# maker derivation matrix, agents-section parser variants, and crawl loop.
# ---------------------------------------------------------------------------

import json as _json

from types import SimpleNamespace as _NS

from src.config import settings as products_settings
from src.llm.fallback_chain import llm_engine as products_llm


def test_products_configured_sources_override(monkeypatch):
    crawler = ProductsCrawler(target_count=3)
    # list-of-dicts override replaces the built-ins
    monkeypatch.setattr(products_settings, "product_sources_json",
                       _json.dumps([{"name": "Src A", "url": "https://a/readme"}, {"name": "Broken"}]))
    # dict entries are kept even when a field is missing ('' placeholder)
    assert crawler._configured_sources() == [("Src A", "https://a/readme"), ("Broken", "")]
    # list-of-lists form is also accepted
    monkeypatch.setattr(products_settings, "product_sources_json",
                       _json.dumps([["Src B", "https://b/readme"]]))
    assert crawler._configured_sources() == [("Src B", "https://b/readme")]
    # Empty / non-list / unparseable fall back to the class table
    for bad in ("", "{oops", "[42]"):
        monkeypatch.setattr(products_settings, "product_sources_json", bad)
        assert crawler._configured_sources() == list(ProductsCrawler.SOURCES)


@pytest.mark.parametrize(
    "name, desc, url, expected_start",
    [
        # No 'by/from' match -> the product name itself is the maker candidate
        ("SimpleTool", "A simple tool for teams.", "https://simpletool.io", "SimpleTool"),
        # 'by <Maker> is/has' captures the maker
        ("Tool", "A great tool by OpenAI is widely used.", "https://tool.io", "OpenAI"),
        # 'from <Maker>' at the end of the description (end-of-string boundary)
        ("Tool2", "Built from Anthropic", "https://tool2.io", "Anthropic"),
        # Sentence-like (>5 words) falls back to the GitHub owner
        ("EssayTitleLike", "Made by Ai Lab Best New Startup Co", "https://github.com/openai/tool", "openai"),
        # Sentence-like fallback then derives the org from a non-GitHub domain
        ("EssayTitleLike2", "Made by Ai Lab Best New Startup Co", "https://openai.com/research/tool", "Openai"),
        # 'by github.com' explicit exclusion falls back to the product name
        ("Tool3", "Maintained by github.com directly here.", "https://github.com/org/tool", "Tool3"),
        # Multi-word maker without trailing punctuation stays as captured
        ("Tool4", "Made by Example Co", "https://example.co", "Example"),
    ],
)
def test_products_get_raw_maker_matrix(name, desc, url, expected_start):
    maker = ProductsCrawler._get_raw_maker(name, desc, url)
    assert maker.startswith(expected_start)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/openai/tool", "openai"),
        ("https://github.com/topics/agents", None),          # reserved path
        ("https://other.example.com/repo", None),            # not GitHub
        ("https://github.com/org", "org"),
    ],
)
def test_products_extract_github_owner_matrix(url, expected):
    assert ProductsCrawler._extract_github_owner(url) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("ChatGPT", PricingModelEnum.FREEMIUM),   # KNOWN_PRICING override
        ("AutoGPT", PricingModelEnum.FREE),
        ("Unknown Tool X", PricingModelEnum.FREE),  # keyword fallback path
    ],
)
def test_products_classify_pricing_known_overrides(name, expected):
    got = ProductsCrawler.classify_pricing(name, "https://x.io", "Open source software released under the MIT license for everyone to use freely.")
    assert got == expected


@pytest.mark.asyncio
async def test_products_classify_pricing_async_branches(monkeypatch):
    crawler = ProductsCrawler(target_count=3)
    long_desc = "A sufficiently detailed description for pricing classification to trigger."
    # Known product -> returns without any LLM call
    got = await crawler.classify_pricing_async("AutoGPT", "https://x.io", long_desc)
    assert got == PricingModelEnum.FREE
    # Short description -> skips the LLM tier
    got = await crawler.classify_pricing_async("SomethingNew", "https://x.io", "tiny")
    assert isinstance(got, PricingModelEnum)
    # LLM raises -> deterministic keyword fallback
    async def failing_llm(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(products_llm, "extract_structured", failing_llm)
    got = await crawler.classify_pricing_async("SomethingNew2", "https://x.io", long_desc)
    assert isinstance(got, PricingModelEnum)
    # LLM returning an invalid (non-enum) pricing model -> fallback
    async def bogus_llm(*a, **k):
        return _NS(pricingModel="NOT_A_MODEL")
    monkeypatch.setattr(products_llm, "extract_structured", bogus_llm)
    got = await crawler.classify_pricing_async("SomethingNew3", "https://x.io", long_desc)
    assert isinstance(got, PricingModelEnum)


def test_products_awesome_agents_parser_variants():
    crawler = ProductsCrawler(target_count=3)
    # Sections must be preceded by a newline to be split; asset URLs and in-page
    # anchors are excluded from the collected product list.
    text = (
        "\n## [AgentOne](https://agents.example/one)\n"
        "Some lead-in text.\n"
        "- [Web](https://agents.example/one/app)\n"
        "### Description\nDetailed description text here.\n"
        "### More\nother stuff\n"
        "\n## [ImageOnly](https://agents.example/logo.png)\n"
        "## Malformed section without url\n"
        "\n## [Anchor](https://agents.example/anchored#sec)\n"
    )
    items = crawler._parse_awesome_agents(text)
    names = [n for n, _, _ in items]
    assert "AgentOne" in names
    assert "ImageOnly" not in names      # image asset URLs are filtered
    assert "Anchor" not in names
    # The Web link overrides the header URL; the Description block wins over tagline
    one = [i for i in items if i[0] == "AgentOne"][0]
    assert one[1] == "https://agents.example/one/app"
    assert one[2] == "Detailed description text here."


def test_products_markdown_list_parser_exclusions():
    crawler = ProductsCrawler(target_count=3)
    text = (
        "- [Good](https://good.io) - Does things well.\n"
        "- [AwesomeList](https://awesome.re)\n"
        "- [Readme](https://repo/readme.md)\n"
        "- [Anchor](https://anchor.io#sec)\n"
        "- [Plain](https://plain.io)\n"
    )
    items = crawler._parse_markdown_list(text)
    assert [n for n, _, _ in items] == ["Good", "Plain"]
    good = items[0]
    assert good[2] == "Does things well."


@pytest.mark.asyncio
async def test_products_process_item_exception_returns_none(monkeypatch):
    crawler = ProductsCrawler(target_count=3)

    async def boom_pricing(name, url, desc):
        raise RuntimeError("pricing engine down")

    crawler.classify_pricing_async = boom_pricing  # type: ignore[method-assign]
    sem = asyncio.Semaphore(5)
    rec = await crawler._process_item("Src", "Tool", "https://tool.io", "some description text", sem)
    assert rec is None


@pytest.mark.asyncio
async def test_products_crawl_concurrent_sources_and_errors(monkeypatch):
    # One source succeeds, the second fetch fails (tolerated), the third parser raises
    # (tolerated) and processing stops once the target is reached.
    crawler = ProductsCrawler(target_count=5)
    good_md = "- [ToolA](https://toola.io) - First product description here.\n- [ToolB](https://toolb.io) - Second product description here.\n"
    calls = {"n": 0}

    async def fake_fetch(url, **kwargs):
        if "two" in url:
            raise RuntimeError("source two down")
        return good_md

    crawler.fetch = fake_fetch  # type: ignore[method-assign]

    def fake_parse(text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("parser broke")
        return [("ToolA", "https://toola.io", "First product description here."),
                ("ToolB", "https://toolb.io", "Second product description here.")]

    crawler._parse_markdown_list = fake_parse  # type: ignore[method-assign]
    monkeypatch.setattr(products_settings, "product_sources_json",
                        _json.dumps([["One", "https://one/readme"], ["Two", "https://two/readme"], ["Three", "https://three/readme"]]))

    records = await crawler.crawl()
    await crawler.close()

    assert len(records) == 2
    assert {r.content.productName for r in records} == {"ToolA", "ToolB"}


@pytest.mark.asyncio
async def test_crawl_skips_llm_classification_for_wal_recovered_names():
    # Regression (M6): names already recovered from WAL (registered in seen_keys) must be
    # filtered from candidates BEFORE processing, so the pricing LLM is not re-invoked
    # for records that add() would only discard.
    crawler = ProductsCrawler(target_count=5)
    recovered = ProductRecord(
        source=SourceMetadata(name="S", url="https://toola.io"),
        content=ProductContent(
            startupName="ToolA", productName="ToolA",
            productUrl="https://toola.io", pricingModel=PricingModelEnum.FREE,
        ),
    )
    crawler.add("ToolA", recovered)  # WAL recovery registers the key and the record
    assert crawler.seen_keys == {"tool a".replace(" ", "")}  # sanity: registered lowercased

    classify_calls = {"n": 0}

    async def _counting_classify(name, url, desc):
        classify_calls["n"] += 1
        return PricingModelEnum.FREEMIUM

    async def fake_fetch(url, **kwargs):
        return (
            "- [ToolA](https://toola.io) First product description here.\n"
            "- [ToolB](https://toolb.io) Second product description here."
        )

    crawler.fetch = fake_fetch  # type: ignore[method-assign]
    crawler.classify_pricing_async = _counting_classify  # type: ignore[method-assign]

    records = await crawler.crawl()
    await crawler.close()

    names = {r.content.productName for r in records}
    assert names == {"ToolA", "ToolB"}  # recovered record retained + ToolB newly collected
    assert classify_calls["n"] == 1  # only ToolB paid the LLM; ToolA was skipped

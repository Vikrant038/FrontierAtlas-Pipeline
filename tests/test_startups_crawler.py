"""
Unit tests for StartupsCrawler multi-source acquisition.
Follows AAA pattern with offline respx mocking per CODING_STANDARDS.md.
"""

import pytest
import respx
import httpx

from src.crawlers.startups_crawler import StartupsCrawler


@pytest.mark.asyncio
async def test_startups_crawler_canonical_seeds_ingestion():
    # Arrange
    crawler = StartupsCrawler(target_count=5)

    # Act
    startups = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(startups) == 5
    names = [s.content.entityName for s in startups]
    assert "OpenAI" in names
    assert "Anthropic" in names

    # Verify seed record has None employeeCount and honest source name
    openai_rec = next(s for s in startups if s.content.entityName == "OpenAI")
    assert openai_rec.content.data.employeeCount is None
    assert openai_rec.source.name == "Canonical Seed List (Internal)"


@pytest.mark.asyncio
@respx.mock
async def test_startups_crawler_yc_pagination_and_team_size():
    # Arrange
    crawler = StartupsCrawler(target_count=2)
    crawler.CANONICAL_SEEDS = []  # Bypass seeds to test YC endpoint

    yc_mock_data = {
        "companies": [
            {"name": "Anthropic PBC", "website": "https://anthropic.com", "teamSize": 800, "slug": "anthropic"},
            {"name": "OpenAI, Inc.", "website": "https://openai.com", "teamSize": 3500, "slug": "openai"},
        ]
    }
    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=1").mock(
        return_value=httpx.Response(200, json=yc_mock_data)
    )

    # Act
    startups = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(startups) == 2
    assert startups[0].content.entityName == "Anthropic"
    assert startups[0].content.data.employeeCount == 800
    assert startups[1].content.entityName == "OpenAI"
    assert startups[1].content.data.employeeCount == 3500


@pytest.mark.asyncio
@respx.mock
async def test_startups_crawler_hf_and_github_orgs():
    # Arrange
    crawler = StartupsCrawler(target_count=2)
    crawler.CANONICAL_SEEDS = []
    # Mock YC to return empty
    respx.get("https://api.ycombinator.com/v0.1/companies?category=artificial_intelligence&page=1").mock(
        return_value=httpx.Response(200, json={"companies": []})
    )
    # Mock Hugging Face models
    respx.get("https://huggingface.co/api/models?limit=1000").mock(
        return_value=httpx.Response(200, json=[
            {"id": "meta-llama/Llama-3.3-70B"},
            {"id": "mistralai/Mistral-Large"},
        ])
    )

    # Act
    startups = await crawler.crawl()
    await crawler.close()

    # Assert
    assert len(startups) == 2
    names = [s.content.entityName for s in startups]
    assert "meta-llama" in names
    assert "Mistral AI" in names

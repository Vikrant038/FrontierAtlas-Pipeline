"""
Unit tests for MultiTierLLMEngine fallback chain and zero-record-loss resilience (Phase III: 3A).
Follows AAA pattern per CODING_STANDARDS.md.
"""

import asyncio
from unittest.mock import AsyncMock
import pytest
from freezegun import freeze_time

from src.crawlers.jobs_crawler import JobsCrawler
from src.crawlers.news_crawler import NewsCrawler
from src.crawlers.products_crawler import ProductsCrawler
from src.llm.fallback_chain import (
    MultiTierLLMEngine,
    _clean_json_markdown,
    LLMRateLimitError,
    LLMTransientError,
    llm_engine,
)
from src.llm.prompts import (
    JOB_EXTRACTION_PROMPT,
    NEWS_SUMMARY_PROMPT,
    PRODUCT_PRICING_PROMPT,
    JobExtractionSchema,
    NewsSummarySchema,
    ProductPricingSchema,
)
from src.schemas.entities import PricingModelEnum, RoleFamilyEnum


def test_clean_json_markdown():
    # Arrange & Act & Assert
    assert _clean_json_markdown('{"a": 1}') == '{"a": 1}'
    assert _clean_json_markdown('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _clean_json_markdown('```\n{"a": 1}\n```') == '{"a": 1}'


@pytest.mark.no_auto_mock_llm
@pytest.mark.asyncio
async def test_llm_engine_tier1_success(monkeypatch):
    # Arrange
    engine = MultiTierLLMEngine()
    mock_gemini = AsyncMock(return_value='{"summary": "Gemini produced summary."}')
    monkeypatch.setattr(engine, "_call_gemini", mock_gemini)

    # Act
    res = await engine.extract_structured(
        raw_text="Some long article text here...",
        schema_cls=NewsSummarySchema,
        instruction=NEWS_SUMMARY_PROMPT,
    )

    # Assert
    assert res.summary == "Gemini produced summary."
    mock_gemini.assert_awaited_once()


@pytest.mark.no_auto_mock_llm
@pytest.mark.asyncio
async def test_llm_engine_tier1_fails_tier2_succeeds(monkeypatch):
    # Arrange
    engine = MultiTierLLMEngine()
    mock_gemini = AsyncMock(side_effect=LLMRateLimitError("429 rate limit"))
    mock_groq = AsyncMock(return_value='{"is_remote": true, "role_family": "Engineering"}')
    monkeypatch.setattr(engine, "_call_gemini", mock_gemini)
    monkeypatch.setattr(engine, "_call_openai_compat", mock_groq)

    # Act
    res = await engine.extract_structured(
        raw_text="Hiring senior python engineer. Fully remote work anywhere.",
        schema_cls=JobExtractionSchema,
        instruction=JOB_EXTRACTION_PROMPT,
    )

    # Assert
    assert res.is_remote is True
    assert res.role_family == RoleFamilyEnum.ENGINEERING
    mock_gemini.assert_awaited_once()
    mock_groq.assert_awaited_once()


@pytest.mark.no_auto_mock_llm
@pytest.mark.asyncio
async def test_llm_engine_all_tiers_fail_deterministic_fallback(monkeypatch):
    # Arrange
    engine = MultiTierLLMEngine()
    mock_gemini = AsyncMock(side_effect=LLMTransientError("Gemini down"))
    mock_openai = AsyncMock(side_effect=LLMTransientError("Groq down"))
    monkeypatch.setattr(engine, "_call_gemini", mock_gemini)
    monkeypatch.setattr(engine, "_call_openai_compat", mock_openai)

    # Act - News
    news_res = await engine.extract_structured(
        raw_text="OpenAI released GPT-5 today. It is widely considered a major step. Performance improved.",
        schema_cls=NewsSummarySchema,
        instruction=NEWS_SUMMARY_PROMPT,
    )
    # Act - Product
    prod_res = await engine.extract_structured(
        raw_text="Product Name: VectorDB\nURL: https://github.com/vectordb\nDescription: Open source vector index licensed under MIT",
        schema_cls=ProductPricingSchema,
        instruction=PRODUCT_PRICING_PROMPT,
    )
    # Act - Job
    job_res = await engine.extract_structured(
        raw_text="Job Title: AI Research Scientist\nLocation: Remote, Worldwide\nDescription: Conducting frontier model research.",
        schema_cls=JobExtractionSchema,
        instruction=JOB_EXTRACTION_PROMPT,
    )

    # Assert
    assert news_res.summary is not None
    assert "OpenAI released GPT-5" in news_res.summary
    assert prod_res.pricingModel == PricingModelEnum.FREE
    assert job_res.is_remote is True
    assert job_res.role_family == RoleFamilyEnum.RESEARCH


@pytest.mark.no_auto_mock_llm
@pytest.mark.asyncio
@freeze_time("2026-09-04T12:00:00Z")
async def test_crawler_resilience_zero_dropped_records_on_llm_failure(monkeypatch):
    # Arrange: force llm_engine.extract_structured to fail completely
    mock_extract = AsyncMock(side_effect=Exception("Catastrophic LLM Failure 500"))
    monkeypatch.setattr(llm_engine, "extract_structured", mock_extract)

    # Act - Products Crawler pricing fallback
    prod_crawler = ProductsCrawler(target_count=1)
    pricing = await prod_crawler.classify_pricing_async(
        name="AutoGPT",
        url="https://github.com/Significant-Gravitas/AutoGPT",
        desc="An open-source experimental autonomous agent framework.",
    )

    # Assert - Product crawler fell back to keyword rules without dropping
    assert pricing == PricingModelEnum.FREE

    # Act - Jobs Crawler classification fallback
    jobs_crawler = JobsCrawler()
    rec = jobs_crawler._build_record(
        raw_company="OpenAI",
        title="Senior Research Scientist",
        raw_date="2026-09-04T10:00:00Z",
        url="https://openai.com/careers/123",
        source_name="Test Jobs",
        remote=True,
    )

    # Assert - Job record cleanly produced with fallback role
    assert rec is not None
    assert rec.content.role_family == RoleFamilyEnum.RESEARCH
    assert rec.content.is_remote is True

    # Act - News Crawler summary fallback
    class MockEntry:
        title = "AI Breakthrough"
        link = "https://example.com/ai-breakthrough"
        summary = "RSS fallback summary text that describes the breakthrough."
        published = "2026-09-04T11:00:00Z"

    news_crawler = NewsCrawler()
    news_rec = await news_crawler._process_entry(MockEntry(), "TechCrunch AI")

    # Assert - News record cleanly produced with RSS summary fallback
    assert news_rec is not None
    assert news_rec.content.title == "AI Breakthrough"
    assert "RSS fallback summary text" in news_rec.content.summary


@pytest.mark.asyncio
async def test_provider_rate_limiter_pacing(monkeypatch):
    # Arrange: rate limiter with limit = 2
    import time
    from src.llm.rate_limiter import ProviderRateLimiter
    limiter = ProviderRateLimiter(rpm_limits={"test": 2})
    slept_durations = []
    fake_now = 1000.0

    def mock_monotonic():
        nonlocal fake_now
        return fake_now

    async def mock_sleep(d):
        nonlocal fake_now
        slept_durations.append(d)
        fake_now += d + 1.0

    monkeypatch.setattr(time, "monotonic", mock_monotonic)
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    # Act
    t1 = await limiter.acquire("test")
    t2 = await limiter.acquire("test")
    t3 = await limiter.acquire("test")

    # Assert: first two calls did not sleep, third call triggered pacing sleep
    assert t1 == 0.0
    assert t2 == 0.0
    assert len(slept_durations) == 1
    assert slept_durations[0] > 0.0


@pytest.mark.no_auto_mock_llm
@pytest.mark.asyncio
async def test_llm_engine_schema_invalid_output_falls_over_to_tier2(monkeypatch):
    # Arrange: Tier 1 returns invalid payload (invalid enum value)
    engine = MultiTierLLMEngine()
    mock_gemini = AsyncMock(return_value='{"pricingModel": "NOT_A_VALID_ENUM"}')
    mock_groq = AsyncMock(return_value='{"pricingModel": "FREE"}')
    monkeypatch.setattr(engine, "_call_gemini", mock_gemini)
    monkeypatch.setattr(engine, "_call_openai_compat", mock_groq)

    # Act
    res = await engine.extract_structured(
        raw_text="Product Name: Tool\nDescription: Completely free open source",
        schema_cls=ProductPricingSchema,
        instruction=PRODUCT_PRICING_PROMPT,
    )

    # Assert: Tier 1 schema failure triggered failover to Tier 2 without crashing
    assert res.pricingModel == PricingModelEnum.FREE
    mock_gemini.assert_awaited_once()
    mock_groq.assert_awaited_once()

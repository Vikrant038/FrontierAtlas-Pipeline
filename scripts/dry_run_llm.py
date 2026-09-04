#!/usr/bin/env python3
"""
Diagnostic dry-run script for FrontierAtlas Multi-Tier LLM Fallback Engine (Phase III: 3A).
Demonstrates structured extraction across all three pipeline integration points:
1. News summary extraction (NewsSummarySchema)
2. Job listing role & remote extraction (JobExtractionSchema)
3. Product pricing classification (ProductPricingSchema)
Demonstrates active fallback chain: Tier 1 failover to Tier 2, and all API failover to Tier 4 deterministic.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.llm.fallback_chain import (
    MultiTierLLMEngine,
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


SAMPLE_NEWS = """
San Francisco, CA — Anthropic today announced Claude 3.7 Sonnet, the company's first hybrid reasoning model capable of dynamically adjusting its thinking time.
The model produces instantaneous responses for conversational queries while scaling test-time compute for complex coding and mathematical proofs.
Anthropic stated that Claude 3.7 Sonnet is available immediately to all API customers and Pro subscribers.
"""

SAMPLE_JOB = """
Job Title: Staff AI Research Scientist - Post-Training
Location: Remote (US, Canada, UK, or Europe)
Company: Frontier AI Labs
Description: We are seeking a Staff Research Scientist to lead our post-training and RLHF initiatives for next-generation multi-modal foundation models.
You will design reinforcement learning from human and AI feedback algorithms, evaluate test-time scaling strategies, and publish frontier findings.
Benefits: 100% remote work flexibility, competitive equity package, comprehensive healthcare coverage.
"""

SAMPLE_PRODUCT = """
Product Name: Cursor
URL: https://www.cursor.com
Description: The AI Code Editor built for pair-programming. Features intelligent tab auto-complete, multi-file code editing, and repository semantic indexing.
Offers a free tier with 2,000 completions per month, Pro subscription at $20/month, and Business plans for teams.
"""


async def main():
    print("=" * 80)
    print("🌌 FRONTIERATLAS MULTI-TIER LLM FALLBACK ENGINE DRY-RUN (PHASE III: 3A)")
    print("=" * 80)
    print(f"Tier 1 (Gemini):   {settings.gemini_model} (configured: {bool(settings.gemini_api_key)})")
    print(f"Tier 2 (Groq):     {settings.groq_model} (configured: {bool(settings.groq_api_key)})")
    print(f"Tier 3 (Custom):   {settings.effective_tier3_model} (configured: {bool(settings.effective_tier3_api_key)})")
    print(f"Tier 4 (Rules):    Deterministic Zero-API Heuristics")
    print(f"Concurrency Limit: {settings.max_concurrent_llm_requests} parallel workers")
    print("-" * 80)

    # ------------------------------------------------------------------------
    # 1. News Article Summary Extraction
    # ------------------------------------------------------------------------
    print("\n[Use Case 1/3] News Article Summary Extraction")
    print("Input Text Snippet:", SAMPLE_NEWS.strip().splitlines()[0])
    try:
        news_out: NewsSummarySchema = await llm_engine.extract_structured(
            raw_text=SAMPLE_NEWS,
            schema_cls=NewsSummarySchema,
            instruction=NEWS_SUMMARY_PROMPT,
        )
        print("✅ Extracted Summary:")
        print(f"   \"{news_out.summary}\"")
    except Exception as exc:
        print(f"❌ Extraction failed: {exc}")
        return 1

    # ------------------------------------------------------------------------
    # 2. Job Listing Role & Remote Extraction
    # ------------------------------------------------------------------------
    print("\n[Use Case 2/3] Job Listing Role & Remote Extraction")
    print("Input Text Snippet: Staff AI Research Scientist (Remote)")
    try:
        job_out: JobExtractionSchema = await llm_engine.extract_structured(
            raw_text=SAMPLE_JOB,
            schema_cls=JobExtractionSchema,
            instruction=JOB_EXTRACTION_PROMPT,
        )
        print("✅ Extracted Job Attributes:")
        print(f"   Role Family: {job_out.role_family.value}")
        print(f"   Is Remote:   {job_out.is_remote}")
        assert isinstance(job_out.role_family, RoleFamilyEnum)
        assert isinstance(job_out.is_remote, bool)
    except Exception as exc:
        print(f"❌ Extraction failed: {exc}")
        return 1

    # ------------------------------------------------------------------------
    # 3. Product Pricing Model Classification
    # ------------------------------------------------------------------------
    print("\n[Use Case 3/3] Product Pricing Model Classification")
    print("Input Text Snippet: Cursor (AI Code Editor, free tier + $20/mo Pro)")
    try:
        prod_out: ProductPricingSchema = await llm_engine.extract_structured(
            raw_text=SAMPLE_PRODUCT,
            schema_cls=ProductPricingSchema,
            instruction=PRODUCT_PRICING_PROMPT,
        )
        print("✅ Extracted Pricing Model:")
        print(f"   Pricing Model: {prod_out.pricingModel.value}")
        assert isinstance(prod_out.pricingModel, PricingModelEnum)
    except Exception as exc:
        print(f"❌ Extraction failed: {exc}")
        return 1

    # ------------------------------------------------------------------------
    # 4. Fallback Chain Demonstration: Tier 1 Failure -> Tier 2 Success
    # ------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("🧪 DEMONSTRATION 1: Tier 1 (Gemini 429) -> Tier 2 (Groq Failover)")
    print("-" * 80)
    fallback_engine = MultiTierLLMEngine()
    fallback_engine._call_gemini = AsyncMock(side_effect=LLMRateLimitError("HTTP 429: Gemini Quota Exceeded"))

    fb_out = await fallback_engine.extract_structured(
        raw_text=SAMPLE_PRODUCT,
        schema_cls=ProductPricingSchema,
        instruction=PRODUCT_PRICING_PROMPT,
    )
    print(f"✅ Failover Successfully Handled: {fb_out.pricingModel.value}")

    # ------------------------------------------------------------------------
    # 5. Fallback Chain Demonstration: All APIs Fail -> Tier 4 Deterministic
    # ------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("🧪 DEMONSTRATION 2: All APIs Fail -> Tier 4 (Deterministic Zero-API Heuristics)")
    print("-" * 80)
    offline_engine = MultiTierLLMEngine()
    offline_engine._call_gemini = AsyncMock(side_effect=LLMTransientError("Gemini Down"))
    offline_engine._call_openai_compat = AsyncMock(side_effect=LLMTransientError("Provider Down"))

    det_news = await offline_engine.extract_structured(
        raw_text=SAMPLE_NEWS,
        schema_cls=NewsSummarySchema,
        instruction=NEWS_SUMMARY_PROMPT,
    )
    det_job = await offline_engine.extract_structured(
        raw_text=SAMPLE_JOB,
        schema_cls=JobExtractionSchema,
        instruction=JOB_EXTRACTION_PROMPT,
    )
    det_prod = await offline_engine.extract_structured(
        raw_text=SAMPLE_PRODUCT,
        schema_cls=ProductPricingSchema,
        instruction=PRODUCT_PRICING_PROMPT,
    )

    print(f"✅ Deterministic News Lead:     \"{det_news.summary[:80]}...\"")
    print(f"✅ Deterministic Job Role:     {det_job.role_family.value} (Remote: {det_job.is_remote})")
    print(f"✅ Deterministic Prod Pricing: {det_prod.pricingModel.value}")

    print("\n" + "=" * 80)
    print("🎉 ALL 3 USE CASES AND FALLBACK CHAINS VERIFIED SUCCESSFULLY!")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

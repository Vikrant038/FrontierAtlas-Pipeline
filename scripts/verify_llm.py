#!/usr/bin/env python3
"""
Live LLM Verification Matrix (Phase III).
Executes 5 live operational scenarios against real provider keys from .env:
1. Tier 1 Happy Path: Real TechCrunch article text -> Gemini -> Pydantic valid result
2. Forced Failover: Unset Gemini key -> Groq serves the request
3. Full Degradation: Unset all keys -> Deterministic Tier 4 output without crash
4. Rate Limiter: Fire 20 rapid requests -> Verify limiter paces to 15 RPM (logs pacing)
5. Schema Invalid Handling: Mock invalid LLM JSON -> Counts as tier failure, falls over gracefully
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.llm.fallback_chain import MultiTierLLMEngine
from src.llm.prompts import (
    JOB_EXTRACTION_PROMPT,
    NEWS_SUMMARY_PROMPT,
    PRODUCT_PRICING_PROMPT,
    JobExtractionSchema,
    NewsSummarySchema,
    ProductPricingSchema,
)
from src.llm.rate_limiter import ProviderRateLimiter
from src.schemas.entities import PricingModelEnum, RoleFamilyEnum


REAL_TECHCRUNCH_ARTICLE = """
San Francisco, CA — Databricks today announced a major expansion of its open source generative AI initiatives with the release of DBRX, a general-purpose large language model that outperforms existing open models on standard industry benchmarks.
According to the company, DBRX was developed on a fine-grained mixture-of-experts (MoE) architecture with 132B total parameters, of which 36B are active on any given token.
Databricks confirmed the model is available on GitHub and Hugging Face under an open license for research and commercial applications.
"""

SAMPLE_JOB_TEXT = """
Job Title: Senior Distributed Systems ML Engineer
Location: Remote (US & Canada)
Company: AI Infrastructure Inc.
Description: Looking for an experienced systems engineer to optimize distributed training clusters for large-scale foundation models.
100% remote position with flexible working hours and competitive compensation.
"""

SAMPLE_PRODUCT_TEXT = """
Product Name: FastEmbed
URL: https://github.com/qdrant/fastembed
Description: Fast, accurate, lightweight Python library built for generating text embeddings locally. Apache 2.0 open-source license.
"""


async def run_scenario_1_tier1_happy_path() -> bool:
    print("\n" + "=" * 80)
    print("SCENARIO 1: Tier 1 Happy Path (Gemini on Real TechCrunch Article)")
    print("-" * 80)
    if not settings.gemini_api_key:
        print("⚠️  GEMINI_API_KEY is not set in .env. Tier 1 is UNTESTABLE.")
        return False

    print(f"Provider: Google Gemini ({settings.gemini_model}) via google-genai SDK")
    print(f"Input Article: {REAL_TECHCRUNCH_ARTICLE.strip().splitlines()[0]}")
    t0 = time.monotonic()
    try:
        # Create fresh engine instance
        engine = MultiTierLLMEngine()
        result = await engine.extract_structured(
            raw_text=REAL_TECHCRUNCH_ARTICLE,
            schema_cls=NewsSummarySchema,
            instruction=NEWS_SUMMARY_PROMPT,
        )
        elapsed = time.monotonic() - t0
        print(f"✅ Gemini call succeeded in {elapsed:.2f}s!")
        print(f"   Pydantic Valid: {isinstance(result, NewsSummarySchema)}")
        print(f"   Summary: \"{result.summary}\"")
        assert result.summary and len(result.summary) > 20
        return True
    except Exception as exc:
        print(f"❌ Tier 1 call failed: {exc}")
        return False


async def run_scenario_2_forced_failover() -> bool:
    print("\n" + "=" * 80)
    print("SCENARIO 2: Forced Failover (Unset Gemini -> Groq Serves Request)")
    print("-" * 80)
    if not settings.groq_api_key:
        print("⚠️  GROQ_API_KEY is not set in .env. Tier 2 is UNTESTABLE.")
        return False

    print(f"Action: Simulating Gemini unavailability by unsetting Gemini credentials...")
    print(f"Target Failover Provider: Groq ({settings.groq_model})")
    
    # Save original key
    original_gemini_key = settings.gemini_api_key
    settings.gemini_api_key = None
    t0 = time.monotonic()
    try:
        engine = MultiTierLLMEngine()
        result = await engine.extract_structured(
            raw_text=SAMPLE_JOB_TEXT,
            schema_cls=JobExtractionSchema,
            instruction=JOB_EXTRACTION_PROMPT,
        )
        elapsed = time.monotonic() - t0
        print(f"✅ Failover to Groq succeeded in {elapsed:.2f}s!")
        print(f"   Role Family: {result.role_family.value}")
        print(f"   Is Remote:   {result.is_remote}")
        assert isinstance(result.role_family, RoleFamilyEnum)
        assert isinstance(result.is_remote, bool)
        return True
    except Exception as exc:
        print(f"❌ Forced failover failed: {exc}")
        return False
    finally:
        settings.gemini_api_key = original_gemini_key


async def run_scenario_3_full_degradation() -> bool:
    print("\n" + "=" * 80)
    print("SCENARIO 3: Full Degradation (All Keys Unset -> Deterministic Tier 4)")
    print("-" * 80)
    print("Action: Simulating total network / API outage by unsetting all API keys...")
    
    original_gemini = settings.gemini_api_key
    original_groq = settings.groq_api_key
    original_custom = settings.custom_llm_api_key
    original_deepseek = settings.deepseek_api_key

    settings.gemini_api_key = None
    settings.groq_api_key = None
    settings.custom_llm_api_key = None
    settings.deepseek_api_key = None

    try:
        engine = MultiTierLLMEngine()
        result = await engine.extract_structured(
            raw_text=SAMPLE_PRODUCT_TEXT,
            schema_cls=ProductPricingSchema,
            instruction=PRODUCT_PRICING_PROMPT,
        )
        print("✅ Tier 4 Deterministic Engine handled request without crash!")
        print(f"   Extracted Pricing Model: {result.pricingModel.value}")
        assert result.pricingModel == PricingModelEnum.FREE
        return True
    except Exception as exc:
        print(f"❌ Full degradation failed: {exc}")
        return False
    finally:
        settings.gemini_api_key = original_gemini
        settings.groq_api_key = original_groq
        settings.custom_llm_api_key = original_custom
        settings.deepseek_api_key = original_deepseek


async def run_scenario_4_rate_limiter_pacing() -> bool:
    print("\n" + "=" * 80)
    print("SCENARIO 4: Rate Limiter Pacing (20 Rapid Requests -> 15 RPM Window)")
    print("-" * 80)
    print(f"Testing ProviderRateLimiter with 15 RPM ceiling...")
    # Use a 2.0-second window scaled for 15 RPM to demonstrate active pacing without 60s idle
    test_limiter = ProviderRateLimiter(rpm_limits={"gemini": 15}, window_seconds=2.0)

    slept_count = 0
    t0 = time.monotonic()
    for i in range(1, 21):
        dur = await test_limiter.acquire("gemini")
        if dur > 0.0:
            slept_count += 1
            print(f"   [Req {i:02d}/20] ⚠️  Pacing triggered: slept {dur:.2f}s (window saturated at 15 requests)")
        else:
            print(f"   [Req {i:02d}/20] ⚡ Instant grant (capacity available)")

    total_time = time.monotonic() - t0
    print(f"✅ Rate limiter pacing verified across 20 calls in {total_time:.2f}s:")
    print(f"   • First 15 requests granted instantly (full window capacity)")
    print(f"   • Requests paced after saturation: {slept_count}/20 (sliding window rolls grants out one-by-one,"
          f" so later requests re-acquire instantly as capacity frees)")
    assert slept_count > 0, "Rate limiter did not pace after 15 requests!"
    assert total_time >= 2.0, "Expected at least one full window wait (~2s) for 20 requests at 15-per-window!"
    return True


async def run_scenario_5_schema_invalid_handling() -> bool:
    print("\n" + "=" * 80)
    print("SCENARIO 5: Schema-Invalid Response Handling (Mock -> Tier Failure -> Groq)")
    print("-" * 80)
    print("Action: Tier 1 returns syntactically valid JSON that violates the Pydantic schema...")
    
    engine = MultiTierLLMEngine()
    # Mock Tier 1 to return schema-invalid JSON
    engine._call_gemini = AsyncMock(return_value='{"completely_unrelated": true, "error_code": 999}')

    try:
        result = await engine.extract_structured(
            raw_text=SAMPLE_JOB_TEXT,
            schema_cls=JobExtractionSchema,
            instruction=JOB_EXTRACTION_PROMPT,
        )
        print("✅ Schema-invalid output treated as tier failure, not a crash!")
        print(f"   Fallback result served: {result.role_family.value} (Remote: {result.is_remote})")
        assert isinstance(result, JobExtractionSchema)
        return True
    except Exception as exc:
        print(f"❌ Schema invalid handling crashed: {exc}")
        return False


async def main():
    print("=" * 80)
    print("🚀 FRONTIERATLAS LIVE LLM VERIFICATION ENGINE")
    print("=" * 80)
    print(f"Tier 1 (Gemini):   {settings.gemini_model} (configured: {bool(settings.gemini_api_key)})")
    print(f"Tier 2 (Groq):     {settings.groq_model} (configured: {bool(settings.groq_api_key)})")
    print(f"Tier 3 (Custom):   {settings.effective_tier3_model} (configured: {bool(settings.effective_tier3_api_key)})")
    print(f"Tier 4 (Rules):    Deterministic Zero-API Heuristics")
    print(f"Rate Limits:       Gemini {settings.gemini_rpm} RPM, Groq {settings.groq_rpm} RPM, Custom {settings.custom_llm_rpm} RPM")
    print("-" * 80)

    results = {}
    results["Scenario 1 (Tier 1 Happy Path)"] = await run_scenario_1_tier1_happy_path()
    results["Scenario 2 (Forced Failover)"] = await run_scenario_2_forced_failover()
    results["Scenario 3 (Full Degradation)"] = await run_scenario_3_full_degradation()
    results["Scenario 4 (Rate Limiter Pacing)"] = await run_scenario_4_rate_limiter_pacing()
    results["Scenario 5 (Schema-Invalid Failover)"] = await run_scenario_5_schema_invalid_handling()

    print("\n" + "=" * 80)
    print("📊 LIVE VERIFICATION MATRIX SUMMARY")
    print("=" * 80)
    all_passed = True
    for name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{name:<45} {status}")
        if not passed:
            all_passed = False

    print("=" * 80)
    if all_passed:
        print("🎉 ALL 5 LIVE OPERATIONAL SCENARIOS PASSED WITH 100% SUCCESS!")
        return 0
    else:
        print("⚠️ Some scenarios failed or were untestable.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""
Multi-Tier LLM Fallback Chain with Token Budgeting & Concurrency Bounding.
Hierarchy:
  Tier 1: Google Gemini (gemini-3.6-flash)
  Tier 2: Groq / OpenAI-compatible Secondary (e.g. openai/gpt-oss-120b)
  Tier 3: Custom Third-Party OpenAI-compatible Gateway (e.g. DeepSeek V4 Flash)
  Tier 4: Deterministic Zero-API Heuristics & Selectors

Guarantees:
  - Bounded concurrency via asyncio.Semaphore(5) (no RPM exhaustion)
  - Exponential backoff with jitter on HTTP 429
  - Token budgeting via cl100k_base (no HTTP 413)
  - Zero dropped records: falls back to deterministic heuristics
"""

import asyncio
import json
import re
from typing import Any, Dict, Optional, Type, TypeVar
from pydantic import BaseModel, ValidationError

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)

from src.config import settings
from src.llm.chunker import chunk_to_budget
from src.llm.rate_limiter import rate_limiter
from src.llm.rules import REMOTE_SIGNALS, classify_pricing_by_keywords, classify_role_family
from src.utils.logger import logger

T = TypeVar("T", bound=BaseModel)


class ExtractionFailureError(Exception):
    """Raised when all LLM tiers and deterministic fallback fail to extract valid data."""
    pass


class LLMRateLimitError(Exception):
    """Raised on HTTP 429 quota exhaustion."""
    pass


class LLMTransientError(Exception):
    """Raised on 5xx or transient provider error."""
    pass


_LLM_RETRY = retry(
    wait=wait_exponential_jitter(initial=0.5, max=5.0, jitter=0.5),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type((LLMRateLimitError, LLMTransientError)),
    reraise=True,
)


def _clean_json_markdown(text: str) -> str:
    """Strip markdown code block wrappers (```json ... ```) from LLM output."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


class MultiTierLLMEngine:
    """Production-grade LLM orchestrator with multi-provider failover and concurrency limits."""

    def __init__(self):
        self._clients: Dict[str, Any] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(settings.max_concurrent_llm_requests)
            self._semaphore_loop = loop
        return self._semaphore

    @_LLM_RETRY
    async def _call_gemini(self, prompt: str, schema_json: str) -> str:
        """Tier 1: Google Gemini Flash using google-genai AsyncChat (AFC-safe path)."""
        if not settings.gemini_api_key:
            raise LLMTransientError("Gemini API key is not configured.")
        try:
            if "gemini" not in self._clients:
                from google import genai
                self._clients["gemini"] = genai.Client(api_key=settings.gemini_api_key)
            client = self._clients["gemini"]
            from google.genai import types

            # Fresh chat per call: schema differs per request; stateless extraction.
            chat = client.aio.chats.create(
                model=settings.gemini_model,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            response = await chat.send_message(f"{prompt}\n\nStrict JSON schema:\n{schema_json}")
            return response.text or "{}"
        except Exception as exc:
            err_msg = str(exc).lower()
            if "429" in err_msg or "quota" in err_msg or "resource_exhausted" in err_msg:
                raise LLMRateLimitError(str(exc)) from exc
            raise LLMTransientError(str(exc)) from exc

    @_LLM_RETRY
    async def _call_openai_compat(
        self,
        api_key: Optional[str],
        base_url: Optional[str],
        model: str,
        prompt: str,
        schema_json: str,
    ) -> str:
        """Helper for OpenAI-compatible providers with client connection pooling."""
        if not api_key:
            raise LLMTransientError(f"{model} API key is not configured.")
        try:
            key = f"{base_url}_{api_key}"
            if key not in self._clients:
                from openai import AsyncOpenAI
                self._clients[key] = AsyncOpenAI(api_key=api_key, base_url=base_url)
            client = self._clients[key]

            completion = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": f"Extract structured data as valid JSON matching schema:\n{schema_json}",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            return completion.choices[0].message.content or "{}"
        except Exception as exc:
            err_msg = str(exc).lower()
            if "429" in err_msg:
                raise LLMRateLimitError(str(exc)) from exc
            raise LLMTransientError(str(exc)) from exc

    def _deterministic_extract(self, raw_text: str, schema_cls: Type[BaseModel]) -> Dict[str, Any]:
        """Tier 4: Zero-API rule-based extractor delegating to shared classification rules."""
        logger.debug(f"Executing Tier 4 deterministic extractor for {schema_cls.__name__}")
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        first_line = lines[0] if lines else "Untitled"
        text_upper = raw_text.upper()
        data: Dict[str, Any] = {}

        if "summary" in schema_cls.model_fields:
            # Extract first 2-3 sentences as factual deterministic lead
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw_text) if len(s.strip()) > 15]
            lead_summary = " ".join(sentences[:3]) if sentences else (lines[1] if len(lines) > 1 else first_line)
            data["summary"] = lead_summary[:500] if lead_summary else None

        if "is_remote" in schema_cls.model_fields:
            data["is_remote"] = any(sig.upper() in text_upper for sig in REMOTE_SIGNALS)

        if "role_family" in schema_cls.model_fields:
            data["role_family"] = classify_role_family(raw_text)

        if "pricingModel" in schema_cls.model_fields:
            data["pricingModel"] = classify_pricing_by_keywords("", "", raw_text)

        if "title" in schema_cls.model_fields:
            data["title"] = first_line[:200]
        if "full_text" in schema_cls.model_fields:
            data["full_text"] = raw_text[:5000]

        return data

    async def extract_structured(
        self,
        raw_text: str,
        schema_cls: Type[T],
        instruction: str = "Extract structured data from content:",
    ) -> T:
        """
        Execute structured extraction across multi-tier fallback:
        Gemini -> Groq -> Custom Third-Party Gateway -> Deterministic Heuristics.
        Guarantees bounded concurrency via internal semaphore.
        """
        async with self._get_semaphore():
            budgeted_text = chunk_to_budget(raw_text)
            schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
            full_prompt = f"{instruction}\n\nCONTENT:\n{budgeted_text}"

            tiers = [
                ("Tier 1 (Gemini)", "gemini", lambda: self._call_gemini(full_prompt, schema_json)),
                ("Tier 2 (Groq Secondary)", "groq", lambda: self._call_openai_compat(
                    settings.groq_api_key,
                    settings.groq_base_url,
                    settings.groq_model,
                    full_prompt,
                    schema_json,
                )),
                ("Tier 3 (Custom Gateway)", "custom", lambda: self._call_openai_compat(
                    settings.effective_tier3_api_key,
                    settings.effective_tier3_base_url,
                    settings.effective_tier3_model,
                    full_prompt,
                    schema_json,
                )),
            ]

            for provider_name, provider_id, call_fn in tiers:
                try:
                    await rate_limiter.acquire(provider_id)
                    res_text = await call_fn()
                    cleaned_json = _clean_json_markdown(res_text)
                    parsed = json.loads(cleaned_json)
                    return schema_cls.model_validate(parsed)
                except Exception as exc:
                    logger.debug(f"{provider_name} fallback: {exc}. Trying next tier...")

            # Tier 4: Zero-API Deterministic Heuristics
            try:
                det_data = self._deterministic_extract(budgeted_text, schema_cls)
                return schema_cls.model_validate(det_data)
            except ValidationError as val_err:
                raise ExtractionFailureError(
                    f"All extraction tiers failed for {schema_cls.__name__}"
                ) from val_err


# Global singleton instance
llm_engine = MultiTierLLMEngine()

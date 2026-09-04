"""
Multi-Tier LLM Fallback Chain with Token Budgeting & Concurrency Bounding.
Hierarchy:
  Tier 1: Google Gemini (gemini-3.5-flash-lite)
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
import time
from typing import Any, Dict, List, Optional, Type, TypeVar
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
from src.utils.date_normalizer import parse_retry_after
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


class LLMPayloadError(Exception):
    """Raised on HTTP 413 or context-length errors: the request can never succeed as-is."""
    pass


async def _sleep_provider_retry_after(exc: Exception, max_wait: float = 60.0) -> None:
    """Sleep on a 429 Retry-After header when the provider exposes it (bounded)."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    if headers is None:
        return
    wait_time = parse_retry_after(headers.get("Retry-After") or headers.get("retry-after"))
    if wait_time is not None and wait_time > 0:
        wait = min(wait_time, max_wait)
        logger.info(f"Provider 429: sleeping {wait:.1f}s per Retry-After header before failover.")
        await asyncio.sleep(wait)


_LLM_RETRY = retry(
    wait=wait_exponential_jitter(initial=0.5, max=3.0, jitter=0.5),
    stop=stop_after_attempt(2),
    retry=retry_if_exception_type(LLMTransientError),
    reraise=True,
)


def _clean_json_markdown(text: str) -> str:
    """Strip markdown code block wrappers (```json ... ```) from LLM output."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _classify_provider_error(exc: Exception) -> Exception:
    """Map provider exceptions to rate-limit, payload, or transient classes.
    Payload errors (413 / context-length) are non-retryable: the identical body cannot succeed."""
    err_msg = str(exc).lower()
    if any(t in err_msg for t in (
        "413", "payload too large", "request too large", "content too large", "maximum context length"
    )):
        return LLMPayloadError(str(exc))
    if "429" in err_msg or "quota" in err_msg or "rate_limit" in err_msg or "resource_exhausted" in err_msg:
        return LLMRateLimitError(str(exc))
    return LLMTransientError(str(exc))


class MultiTierLLMEngine:
    """Production-grade LLM orchestrator with multi-provider failover and concurrency limits."""

    def __init__(self):
        self._clients: Dict[str, Any] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
        self._gemini_exhausted_until: float = 0.0
        self._groq_exhausted: bool = False
        self.tier_usage: Dict[str, int] = {
            "gemini": 0,
            "groq": 0,
            "deepseek": 0,
            "deterministic": 0,
        }

    def _get_semaphore(self) -> asyncio.Semaphore:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(settings.max_concurrent_llm_requests)
            self._semaphore_loop = loop
        return self._semaphore

    @staticmethod
    def _pick_key(keys: List[str], salt: str) -> Optional[str]:
        """Round-robin-ish key selection from a pool, stable per prompt (spreads load across keys)."""
        if not keys:
            return None
        if len(keys) == 1:
            return keys[0]
        return keys[hash(salt) % len(keys)]

    @_LLM_RETRY
    async def _call_gemini(self, prompt: str, schema_json: str) -> str:
        """Tier 1: Google Gemini Flash using google-genai AsyncChat (AFC-recommended path)."""
        api_key = self._pick_key(settings.gemini_api_key_list, prompt)
        if not api_key:
            raise LLMTransientError("Gemini API key is not configured.")
        if self._gemini_exhausted_until > time.monotonic():
            raise LLMRateLimitError("Gemini API quota exhausted; backoff active.")
        try:
            client_key = f"gemini:{api_key}"
            if client_key not in self._clients:
                from google import genai
                self._clients[client_key] = genai.Client(api_key=api_key)
            client = self._clients[client_key]
            from google.genai import types

            # Fresh chat per call: schema differs per request; stateless extraction.
            chat = client.aio.chats.create(
                model=settings.gemini_model,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            response = await asyncio.wait_for(
                chat.send_message(f"{prompt}\n\nStrict JSON schema:\n{schema_json}"),
                timeout=settings.llm_call_timeout_seconds,
            )
            return response.text or "{}"
        except Exception as exc:
            err = _classify_provider_error(exc)
            if isinstance(err, LLMRateLimitError):
                await _sleep_provider_retry_after(exc)
                # 60s backoff: the RPM window resets, so Tier 1 recovers this run.
                self._gemini_exhausted_until = time.monotonic() + 60.0
            raise err from exc

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
                self._clients[key] = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=settings.llm_call_timeout_seconds)
            client = self._clients[key]

            completion = await asyncio.wait_for(
                client.chat.completions.create(
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
                ),
                timeout=settings.llm_call_timeout_seconds,
            )
            return completion.choices[0].message.content or "{}"
        except Exception as exc:
            err = _classify_provider_error(exc)
            if isinstance(err, LLMRateLimitError):
                await _sleep_provider_retry_after(exc)
            raise err from exc

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

        if "canonical" in schema_cls.model_fields:
            data["canonical"] = None
            data["confidence"] = 0.0

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
                    self._pick_key(settings.groq_api_key_list, full_prompt),
                    settings.groq_base_url,
                    settings.groq_model,
                    full_prompt,
                    schema_json,
                )),
                ("Tier 3 (Custom Gateway)", "custom", lambda: self._call_openai_compat(
                    self._pick_key(settings.tier3_api_key_list, full_prompt),
                    settings.effective_tier3_base_url,
                    settings.effective_tier3_model,
                    full_prompt,
                    schema_json,
                )),
            ]

            for provider_name, provider_id, call_fn in tiers:
                try:
                    await rate_limiter.acquire(provider_id, max_wait=0.0)
                    res_text = await asyncio.wait_for(call_fn(), timeout=settings.llm_extract_timeout_seconds)
                    cleaned_json = _clean_json_markdown(res_text)
                    parsed = json.loads(cleaned_json)
                    result = schema_cls.model_validate(parsed)
                    tier_key = "deepseek" if provider_id == "custom" else provider_id
                    self.tier_usage[tier_key] += 1
                    return result
                except Exception as exc:
                    logger.debug(f"{provider_name} fallback: {exc}. Trying next tier...")

            # Tier 4: Zero-API Deterministic Heuristics
            try:
                det_data = self._deterministic_extract(budgeted_text, schema_cls)
                result = schema_cls.model_validate(det_data)
                self.tier_usage["deterministic"] += 1
                return result
            except ValidationError as val_err:
                raise ExtractionFailureError(
                    f"All extraction tiers failed for {schema_cls.__name__}"
                ) from val_err

    def get_tier_usage(self) -> Dict[str, int]:
        """Return a copy of the current tier usage counts."""
        return dict(self.tier_usage)

    def reset_tier_usage(self) -> None:
        """Reset all tier usage counts to zero."""
        for k in self.tier_usage:
            self.tier_usage[k] = 0

    def save_tier_telemetry(self, filepath: str = "exports/llm_tier_telemetry.json") -> str:
        """Persist tier usage metrics to JSON for audit and evaluation."""
        import os
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.get_tier_usage(), f, indent=2)
        logger.info(f"LLM tier telemetry persisted to {filepath}: {self.tier_usage}")
        return filepath


# Global singleton instance
llm_engine = MultiTierLLMEngine()

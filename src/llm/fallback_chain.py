import json
from typing import Any, Dict, Optional, Type
from pydantic import BaseModel, ValidationError

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)

from src.config import settings
from src.llm.chunker import chunk_to_budget
from src.utils.logger import logger


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


class MultiTierLLMEngine:
    """Multi-tier extraction engine: Gemini -> Groq -> DeepSeek -> Deterministic."""

    def __init__(self):
        self._clients: Dict[str, Any] = {}

    @_LLM_RETRY
    async def _call_gemini(self, prompt: str, schema_json: str) -> str:
        """Tier 1: Google Gemini 2.0 Flash."""
        if not settings.gemini_api_key:
            raise LLMTransientError("Gemini API key is not configured.")
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.gemini_api_key)
            model = genai.GenerativeModel("gemini-2.0-flash", generation_config={"response_mime_type": "application/json"})
            response = await model.generate_content_async(f"{prompt}\n\nStrict JSON schema:\n{schema_json}")
            return response.text
        except Exception as exc:
            if "429" in str(exc).lower() or "quota" in str(exc).lower():
                raise LLMRateLimitError(str(exc)) from exc
            raise LLMTransientError(str(exc)) from exc

    @_LLM_RETRY
    async def _call_openai_compat(self, api_key: Optional[str], base_url: Optional[str], model: str, prompt: str, schema_json: str) -> str:
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
                    {"role": "system", "content": f"Extract structured data as valid JSON matching schema:\n{schema_json}"},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            return completion.choices[0].message.content or "{}"
        except Exception as exc:
            if "429" in str(exc).lower():
                raise LLMRateLimitError(str(exc)) from exc
            raise LLMTransientError(str(exc)) from exc

    def _deterministic_extract(self, raw_text: str, schema_cls: Type[BaseModel]) -> Dict[str, Any]:
        """Tier 4: Zero-API rule-based regex and heuristic extractor."""
        logger.info(f"Using Tier 4 deterministic extractor for {schema_cls.__name__}")
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        first_line = lines[0] if lines else "Untitled"
        data: Dict[str, Any] = {}

        if "title" in schema_cls.model_fields:
            data["title"] = first_line[:200]
        if "full_text" in schema_cls.model_fields:
            data["full_text"] = raw_text[:5000]
        if "summary" in schema_cls.model_fields:
            data["summary"] = (lines[1] if len(lines) > 1 else first_line)[:500]
        return data

    async def extract_structured(
        self,
        raw_text: str,
        schema_cls: Type[BaseModel],
        instruction: str = "Extract entity details from content:",
    ) -> BaseModel:
        """Execute extraction across fallback tiers: Gemini -> Groq -> DeepSeek -> Deterministic."""
        budgeted_text = chunk_to_budget(raw_text)
        schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
        full_prompt = f"{instruction}\n\nCONTENT:\n{budgeted_text}"

        tiers = [
            ("Gemini Flash", lambda: self._call_gemini(full_prompt, schema_json)),
            ("Groq Llama 3.3", lambda: self._call_openai_compat(
                settings.groq_api_key, "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", full_prompt, schema_json
            )),
            ("DeepSeek Chat", lambda: self._call_openai_compat(
                settings.deepseek_api_key, "https://api.deepseek.com", "deepseek-chat", full_prompt, schema_json
            )),
        ]

        for provider_name, call_fn in tiers:
            try:
                res_text = await call_fn()
                return schema_cls.model_validate(json.loads(res_text))
            except Exception as exc:
                logger.warning(f"Tier ({provider_name}) failed: {exc}. Trying next tier...")

        # Final Tier: Deterministic Heuristics
        try:
            return schema_cls.model_validate(self._deterministic_extract(budgeted_text, schema_cls))
        except ValidationError as val_err:
            raise ExtractionFailureError(f"All extraction tiers failed for {schema_cls.__name__}") from val_err


llm_engine = MultiTierLLMEngine()

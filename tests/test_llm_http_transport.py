"""
Tests for LLM providers using real HTTP bodies via httpx.MockTransport and SDK error mapping.
Verifies offline-safe 429 Retry-After parsing, 413 payload error classification,
200 chat completion decoding, and multi-tier failover under real transport responses.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel

from src.config import settings
from src.llm import fallback_chain as fc
from src.llm.fallback_chain import (
    LLMPayloadError,
    LLMRateLimitError,
    MultiTierLLMEngine,
)
from src.schemas.entities import JobContent, RoleFamilyEnum

pytestmark = pytest.mark.no_auto_mock_llm


def _make_mock_client(base_url: str, handler) -> AsyncOpenAI:
    """Helper to construct an AsyncOpenAI SDK client bound to an httpx.MockTransport."""
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return AsyncOpenAI(api_key="fake_key", base_url=base_url, http_client=http_client)


@pytest.mark.asyncio
async def test_groq_http_mock_transport_429_rate_limit(monkeypatch):
    # Arrange
    sleeps: List[float] = []

    async def _record_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(fc.asyncio, "sleep", _record_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        error_body = {
            "error": {
                "message": (
                    "Rate limit reached for model `llama-3.3-70b-versatile` on TPM: "
                    "Limit 6000, Used 5980. Please try again in 1.5s."
                ),
                "type": "tokens",
                "code": "rate_limit_exceeded",
            }
        }
        return httpx.Response(429, headers={"Retry-After": "1.5"}, json=error_body, request=request)

    engine = MultiTierLLMEngine()
    base_url = "https://api.groq.com/openai/v1"
    engine._clients[f"{base_url}_mock_key"] = _make_mock_client(base_url, handler)

    # Act & Assert
    with pytest.raises(LLMRateLimitError):
        await engine._call_openai_compat(
            api_key="mock_key",
            base_url=base_url,
            model="llama-3.3-70b-versatile",
            prompt="Analyze job requirements",
            schema_json="{}",
        )

    # Assert Retry-After header was parsed and slept
    assert sleeps == [1.5]


@pytest.mark.asyncio
async def test_groq_http_mock_transport_413_payload_error(monkeypatch):
    # Arrange
    sleeps: List[float] = []

    async def _record_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(fc.asyncio, "sleep", _record_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        error_body = {
            "error": {
                "message": "Request entity too large: maximum context length exceeded",
                "type": "invalid_request_error",
                "code": "context_length_exceeded",
            }
        }
        return httpx.Response(413, json=error_body, request=request)

    engine = MultiTierLLMEngine()
    base_url = "https://api.groq.com/openai/v1"
    engine._clients[f"{base_url}_mock_key"] = _make_mock_client(base_url, handler)

    # Act & Assert: non-retryable payload error must raise immediately with zero sleep
    with pytest.raises(LLMPayloadError):
        await engine._call_openai_compat(
            api_key="mock_key",
            base_url=base_url,
            model="llama-3.3-70b-versatile",
            prompt="Analyze massive prompt",
            schema_json="{}",
        )

    assert sleeps == []


@pytest.mark.asyncio
async def test_openai_compat_http_mock_transport_200_chat_completion():
    # Arrange
    expected_content = {
        "company": "DeepMind",
        "title": "Staff Research Scientist",
        "date": "2026-09-04T12:00:00Z",
        "is_remote": True,
        "role_family": RoleFamilyEnum.RESEARCH.value,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        resp_json = {
            "id": "chatcmpl-test-abc",
            "object": "chat.completion",
            "created": 1772600000,
            "model": "deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(expected_content)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165},
        }
        return httpx.Response(200, json=resp_json, request=request)

    engine = MultiTierLLMEngine()
    base_url = "https://api.deepseek.com/v1"
    engine._clients[f"{base_url}_mock_key"] = _make_mock_client(base_url, handler)

    # Act
    result_text = await engine._call_openai_compat(
        api_key="mock_key",
        base_url=base_url,
        model="deepseek-chat",
        prompt="Extract job content",
        schema_json="{}",
    )

    # Assert
    parsed = json.loads(result_text)
    assert parsed["company"] == "DeepMind"
    assert parsed["is_remote"] is True
    assert parsed["role_family"] == RoleFamilyEnum.RESEARCH.value


@pytest.mark.asyncio
async def test_gemini_real_client_error_429_mapping_and_sleep(monkeypatch):
    # Arrange
    from google.genai import errors
    from types import SimpleNamespace

    sleeps: List[float] = []

    async def _record_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(fc.asyncio, "sleep", _record_sleep)

    req = httpx.Request("POST", "https://generativelanguage.googleapis.com")
    resp = httpx.Response(
        429,
        headers={"Retry-After": "2.0"},
        json={
            "error": {
                "code": 429,
                "message": "Resource has been exhausted (rate limit exceeded).",
                "status": "RESOURCE_EXHAUSTED",
            }
        },
        request=req,
    )
    sdk_err = errors.ClientError(429, resp.json(), resp)

    class _FailingChat:
        async def send_message(self, *args, **kwargs):
            raise sdk_err

    class _FailingGeminiClient:
        def __init__(self):
            self.aio = SimpleNamespace(chats=SimpleNamespace(create=lambda **kwargs: _FailingChat()))

    engine = MultiTierLLMEngine()
    engine._clients["gemini:gk"] = _FailingGeminiClient()

    # Act & Assert
    with pytest.raises(LLMRateLimitError):
        await engine._call_gemini("Extract data", "{}", api_key="gk")

    assert sleeps == [2.0]
    assert engine._gemini_exhausted_until > 0


@pytest.mark.asyncio
async def test_multi_tier_failover_across_real_http_transports(monkeypatch):
    # Arrange
    async def _instant_sleep(s):
        pass

    monkeypatch.setattr(fc.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(settings, "gemini_api_keys", "test-gemini-key")
    monkeypatch.setattr(settings, "groq_api_keys", "test-groq-key")
    monkeypatch.setattr(settings, "custom_llm_api_keys", "test-tier3-key")
    monkeypatch.setattr(settings, "custom_llm_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "custom_llm_model", "deepseek-chat")

    # Tier 1 (Gemini) fails with real 429
    from google.genai import errors
    from types import SimpleNamespace
    req = httpx.Request("POST", "https://generativelanguage.googleapis.com")
    resp_gemini = httpx.Response(
        429,
        headers={"Retry-After": "1"},
        json={"error": {"code": 429, "message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}},
        request=req,
    )
    sdk_err = errors.ClientError(429, resp_gemini.json(), resp_gemini)

    class _FailingGemini:
        def __init__(self):
            chat = SimpleNamespace(send_message=self._send)
            self.aio = SimpleNamespace(chats=SimpleNamespace(create=lambda **kwargs: chat))
        async def _send(self, *a, **kw):
            raise sdk_err

    # Tier 2 (Groq) fails with real HTTP 413
    def groq_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"error": {"message": "context length exceeded"}}, request=request)

    # Tier 3 (DeepSeek) succeeds with real HTTP 200 chat completion
    job_payload = {
        "company": "DeepMind",
        "title": "Staff AI Researcher",
        "date": "2026-09-04T12:00:00Z",
        "is_remote": True,
        "role_family": RoleFamilyEnum.RESEARCH.value,
    }
    def deepseek_handler(request: httpx.Request) -> httpx.Response:
        resp = {
            "choices": [{"message": {"content": json.dumps(job_payload)}, "finish_reason": "stop"}],
        }
        return httpx.Response(200, json=resp, request=request)

    engine = MultiTierLLMEngine()
    engine._clients["gemini:test-gemini-key"] = _FailingGemini()
    engine._clients[f"{settings.groq_base_url}_test-groq-key"] = _make_mock_client(settings.groq_base_url, groq_handler)
    engine._clients["https://api.deepseek.com/v1_test-tier3-key"] = _make_mock_client("https://api.deepseek.com/v1", deepseek_handler)

    # Act
    result = await engine.extract_structured(
        raw_text="DeepMind hiring Staff AI Researcher, fully remote.",
        schema_cls=JobContent,
    )

    # Assert: successfully navigated past Tier 1 (429) and Tier 2 (413) to Tier 3 (200)
    assert isinstance(result, JobContent)
    assert result.company == "DeepMind"
    assert result.role_family == RoleFamilyEnum.RESEARCH
    assert engine.tier_usage["deepseek"] == 1
    assert engine.tier_usage["gemini"] == 0
    assert engine.tier_usage["groq"] == 0
    assert engine.tier_usage["deterministic"] == 0

"""
Tests for the live provider bodies in fallback_chain: real _call_gemini /
_call_openai_compat code paths driven by injected fake SDK clients (offline),
plus the provider-error classifier, Retry-After sleeper, key picker, and the
deterministic-extraction tails. Marked no_auto_mock_llm where extract_structured
itself is the unit under test.
"""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from src.llm import fallback_chain as fc
from src.llm.fallback_chain import (
    ExtractionFailureError,
    LLMPayloadError,
    LLMRateLimitError,
    LLMTransientError,
    MultiTierLLMEngine,
    _classify_provider_error,
    _sleep_provider_retry_after,
)


class _ProviderError(Exception):
    """Provider-style exception carrying an optional .response (like httpx errors)."""

    def __init__(self, message: str, response=None):
        super().__init__(message)
        self.response = response


class _FakeGeminiClient:
    """Minimal stand-in for google.genai.Client: aio.chats.create -> chat.send_message.
    Mirrors the genai layout: client.aio.chats.create(...) returns a chat whose
    send_message(...) is awaited."""

    def __init__(self, send_result=None, send_error=None):
        self._result = send_result
        self._error = send_error
        self.aio = self
        self.chats = self

    def create(self, model=None, config=None):
        return self

    async def send_message(self, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(text=self._result)


class _FakeOpenAIClient:
    """Minimal stand-in for openai.AsyncOpenAI: chat.completions.create."""

    def __init__(self, content=None, error=None):
        self._content = content
        self._error = error
        self.chat = self

    async def completions_create(self, **kwargs):
        return None  # replaced by the property below

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


# ---------------------------------------------------------------------------
# 1. Provider error classification and Retry-After sleeper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message, expected",
    [
        ("413 Payload Too Large", LLMPayloadError),
        ("maximum context length exceeded", LLMPayloadError),
        ("request too large for model", LLMPayloadError),
        ("429 rate limit exceeded", LLMRateLimitError),
        ("quota exhausted for resource", LLMRateLimitError),
        ("resource_exhausted: out of quota", LLMRateLimitError),
        ("server error 503, try again later", LLMTransientError),
        ("connection reset by peer", LLMTransientError),
    ],
)
def test_classify_provider_error_maps_every_category(message, expected):
    assert type(_classify_provider_error(Exception(message))) is expected


@pytest.mark.asyncio
async def test_sleep_provider_retry_after_branches(monkeypatch):
    import httpx

    sleeps = []
    monkeypatch.setattr(fc.asyncio, "sleep", lambda s: _record_sleep(sleeps, s))

    # No response attached -> return immediately, no sleep
    await _sleep_provider_retry_after(LLMRateLimitError("429"))
    assert sleeps == []

    # Response without headers -> no sleep
    exc = _ProviderError("429", response=SimpleNamespace(headers=None))
    await _sleep_provider_retry_after(exc)
    assert sleeps == []

    # Retry-After present -> sleep bounded by max_wait
    resp = httpx.Response(429, headers={"Retry-After": "2"}, request=httpx.Request("GET", "http://x"))
    await _sleep_provider_retry_after(_ProviderError("429", response=resp), max_wait=1.5)
    assert sleeps == [1.5]

    # Unparseable Retry-After -> parse_retry_after returns None -> no sleep
    resp = httpx.Response(429, headers={"Retry-After": "not-a-date"}, request=httpx.Request("GET", "http://x"))
    await _sleep_provider_retry_after(_ProviderError("429", response=resp))
    assert sleeps == [1.5]


async def _record_sleep(sleeps, seconds):
    sleeps.append(seconds)


def test_pick_key_branches():
    pick = MultiTierLLMEngine._pick_key
    assert pick([], "salt") is None
    assert pick(["only"], "salt") == "only"
    pool = [f"k{i}" for i in range(4)]
    # Deterministic selection and stable for a given salt
    assert pick(pool, "prompt-a") == pool[fc._stable_hash("prompt-a") % 4]
    assert pick(pool, "prompt-a") == pick(pool, "prompt-a")


@pytest.mark.asyncio
async def test_acquire_tier_slot_three_paths(monkeypatch):
    engine = MultiTierLLMEngine()
    acquired = []

    class StubLimiter:
        async def acquire(self, provider_id, max_wait=0.0, key=None):
            acquired.append((provider_id, key))
            if key == "saturated":
                from src.llm.rate_limiter import RateLimitExceededError
                raise RateLimitExceededError("window full")

    monkeypatch.setattr(fc, "rate_limiter", StubLimiter())

    # No keys configured -> provider-level window, returns None
    assert await engine._acquire_tier_slot("gemini", [], "salt") is None
    # Pool with an unsaturated key -> that key returned
    assert await engine._acquire_tier_slot("gemini", ["a", "b"], "salt") in ("a", "b")
    # Every key saturated -> raises (triggers tier failover)
    with pytest.raises(Exception, match="saturated"):
        await engine._acquire_tier_slot("groq", ["saturated"], "salt")


# ---------------------------------------------------------------------------
# 2. Real _call_gemini body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_gemini_success_and_empty_text():
    engine = MultiTierLLMEngine()
    engine._clients["gemini:gk"] = _FakeGeminiClient(send_result='{"summary": "ok"}')
    out = await engine._call_gemini("p", "{}", api_key="gk")
    assert json.loads(out) == {"summary": "ok"}

    # Provider returning an empty body defaults to '{}' instead of crashing the parser
    engine._clients["gemini:gk2"] = _FakeGeminiClient(send_result="")
    out = await engine._call_gemini("p", "{}", api_key="gk2")
    assert out == "{}"


@pytest.mark.asyncio
async def test_call_gemini_rate_limit_sleeps_and_backs_off(monkeypatch):
    import httpx

    sleeps = []
    monkeypatch.setattr(fc.asyncio, "sleep", lambda s: _record_sleep(sleeps, s))
    engine = MultiTierLLMEngine()
    resp = httpx.Response(429, headers={"Retry-After": "3"}, request=httpx.Request("GET", "http://g"))
    err = _ProviderError("429 Too Many Requests", response=resp)
    engine._clients["gemini:gk"] = _FakeGeminiClient(send_error=err)

    with pytest.raises(LLMRateLimitError):
        await engine._call_gemini("p", "{}", api_key="gk")
    assert sleeps == [3.0]  # honored Retry-After (capped), not the full 60s
    assert engine._gemini_exhausted_until > 0

    # Second call while the backoff window is active fails fast without retry
    with pytest.raises(LLMRateLimitError):
        await engine._call_gemini("p", "{}", api_key="gk")


@pytest.mark.asyncio
async def test_call_gemini_payload_error_not_retried(monkeypatch):
    monkeypatch.setattr(fc.asyncio, "sleep", lambda s: _record_sleep([], s))
    engine = MultiTierLLMEngine()
    engine._clients["gemini:gk"] = _FakeGeminiClient(send_error=Exception("413 Request Entity Too Large"))
    with pytest.raises(LLMPayloadError):
        await engine._call_gemini("p", "{}", api_key="gk")


@pytest.mark.asyncio
async def test_call_gemini_missing_key_and_transient_retry(monkeypatch):
    monkeypatch.setattr(fc.asyncio, "sleep", lambda s: _record_sleep([], s))
    # Force an empty key pool so the no-key branch is exercised (never the live key)
    monkeypatch.setattr(fc.settings, "gemini_api_keys", None)
    monkeypatch.setattr(fc.settings, "gemini_api_key", None)
    engine = MultiTierLLMEngine()
    # No configured key -> transient error raised by the function itself
    with pytest.raises(LLMTransientError, match="not configured"):
        await engine._call_gemini("p", "{}", api_key=None)
    # Transient provider failure -> classified, retried once by _LLM_RETRY, then raised
    engine._clients["gemini:gk"] = _FakeGeminiClient(send_error=Exception("upstream timeout"))
    with pytest.raises(LLMTransientError):
        await engine._call_gemini("p", "{}", api_key="gk")


# ---------------------------------------------------------------------------
# 3. Real _call_openai_compat body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_openai_compat_success_and_missing_key():
    engine = MultiTierLLMEngine()
    engine._clients["https://api.groq.com_ok"] = _FakeOpenAIClient(content='{"is_remote": true}')
    out = await engine._call_openai_compat("ok", "https://api.groq.com", "model-x", "p", "{}")
    assert json.loads(out) == {"is_remote": True}

    with pytest.raises(LLMTransientError, match="not configured"):
        await engine._call_openai_compat(None, "https://api.groq.com", "model-x", "p", "{}")


@pytest.mark.asyncio
async def test_call_openai_compat_rate_limit_sleeps(monkeypatch):
    import httpx

    sleeps = []
    monkeypatch.setattr(fc.asyncio, "sleep", lambda s: _record_sleep(sleeps, s))
    engine = MultiTierLLMEngine()
    resp = httpx.Response(429, headers={"Retry-After": "2"}, request=httpx.Request("GET", "http://o"))
    engine._clients["https://api.groq.com_k"] = _FakeOpenAIClient(error=_ProviderError("429", response=resp))
    with pytest.raises(LLMRateLimitError):
        await engine._call_openai_compat("k", "https://api.groq.com", "model-x", "p", "{}")
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_call_openai_compat_payload_error_no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fc.asyncio, "sleep", lambda s: _record_sleep(sleeps, s))
    engine = MultiTierLLMEngine()
    engine._clients["https://api.groq.com_k"] = _FakeOpenAIClient(error=Exception("maximum context length"))
    with pytest.raises(LLMPayloadError):
        await engine._call_openai_compat("k", "https://api.groq.com", "model-x", "p", "{}")
    assert sleeps == []


# ---------------------------------------------------------------------------
# 4. Semaphore, deterministic-extraction tails, and hard-failure path
# ---------------------------------------------------------------------------


def test_get_semaphore_reused_and_sync_creation():
    engine = MultiTierLLMEngine()
    sem = engine._get_semaphore()  # no running loop -> created with loop=None
    assert engine._get_semaphore() is sem  # cached on the same (absent) loop


@pytest.mark.asyncio
async def test_get_semaphore_async_loop_recreation():
    engine = MultiTierLLMEngine()
    engine._semaphore = None
    sem = engine._get_semaphore()
    assert sem is not None
    assert engine._get_semaphore() is sem


class _TitleFullTextModel(BaseModel):
    summary: str = "x"
    title: str = "t"
    full_text: str = "f"


def test_deterministic_extract_title_and_full_text_tails():
    engine = MultiTierLLMEngine()
    data = engine._deterministic_extract("First line of the article.\n\nBody sentence here.", _TitleFullTextModel)
    assert data["title"] == "First line of the article."[:200]
    assert data["full_text"].startswith("First line")


@pytest.mark.no_auto_mock_llm
@pytest.mark.asyncio
async def test_extract_structured_hard_failure_raises_extraction_error(monkeypatch):
    # No tiers at all + a schema the deterministic extractor cannot satisfy: the
    # only correct outcome is ExtractionFailureError (zero silent data loss).
    engine = MultiTierLLMEngine()

    class _RequiresInt(BaseModel):
        value: int

    monkeypatch.setattr(engine, "_tier_descriptors", lambda *a: [])
    with pytest.raises(ExtractionFailureError):
        await engine.extract_structured(raw_text="no integers here", schema_cls=_RequiresInt)

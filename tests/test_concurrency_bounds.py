"""
Concurrency-bound assertions for HTTP crawlers and LLM extraction.
Verifies that active concurrent requests never exceed configured limits
under parallel load. Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

import asyncio
from types import SimpleNamespace
from typing import List
import httpx
import pytest
from pydantic import BaseModel

from src.config import settings
from src.crawlers.base import AsyncBaseCrawler
from src.llm.fallback_chain import MultiTierLLMEngine


class _ConcreteCrawler(AsyncBaseCrawler):
    """Concrete crawler implementation for testing base transport mechanisms."""

    async def crawl(self, *args, **kwargs):
        return []


class _DummySchema(BaseModel):
    summary: str


@pytest.mark.asyncio
async def test_crawler_request_concurrency_bounded():
    # Arrange
    limit = 3
    active_requests = 0
    max_active = 0
    lock = asyncio.Lock()

    async def mock_get(url: str, **kwargs) -> httpx.Response:
        nonlocal active_requests, max_active
        async with lock:
            active_requests += 1
            if active_requests > max_active:
                max_active = active_requests
        await asyncio.sleep(0.02)
        async with lock:
            active_requests -= 1
        return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    crawler = _ConcreteCrawler(concurrency_limit=limit)
    mock_client = SimpleNamespace(get=mock_get, is_closed=False)
    crawler._client = mock_client

    # Act: launch 20 concurrent requests
    tasks = [
        crawler._request("GET", f"https://example.com/item/{i}")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)

    # Assert
    assert len(results) == 20
    assert all(r.status_code == 200 for r in results)
    assert max_active <= limit, f"Max concurrent requests ({max_active}) exceeded limit ({limit})"
    assert max_active > 1, "Expected concurrency to be exercised under load"


@pytest.mark.asyncio
async def test_crawler_fetch_tls_concurrency_bounded():
    # Arrange
    limit = 4
    active_tls = 0
    max_active_tls = 0
    lock = asyncio.Lock()

    async def mock_curl_get(url: str, **kwargs) -> SimpleNamespace:
        nonlocal active_tls, max_active_tls
        async with lock:
            active_tls += 1
            if active_tls > max_active_tls:
                max_active_tls = active_tls
        await asyncio.sleep(0.02)
        async with lock:
            active_tls -= 1
        return SimpleNamespace(status_code=200, text="<html><body>TLS Content</body></html>")

    crawler = _ConcreteCrawler(concurrency_limit=limit)
    mock_session = SimpleNamespace(get=mock_curl_get, _closed=False)
    crawler._curl_session = mock_session

    # Act: launch 20 concurrent fetch_tls requests
    tasks = [
        crawler.fetch_tls(f"https://example.com/tls/{i}")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)

    # Assert
    assert len(results) == 20
    assert all("TLS Content" in r for r in results)
    assert max_active_tls <= limit, f"Max concurrent TLS ({max_active_tls}) exceeded limit ({limit})"
    assert max_active_tls > 1, "Expected concurrency to be exercised under load"


@pytest.mark.no_auto_mock_llm
@pytest.mark.asyncio
async def test_llm_engine_extract_concurrency_bounded(monkeypatch):
    # Arrange
    limit = 3
    monkeypatch.setattr(settings, "max_concurrent_llm_requests", limit)
    active_llm = 0
    max_active_llm = 0
    lock = asyncio.Lock()

    async def mock_call(key: str) -> str:
        nonlocal active_llm, max_active_llm
        async with lock:
            active_llm += 1
            if active_llm > max_active_llm:
                max_active_llm = active_llm
        await asyncio.sleep(0.02)
        async with lock:
            active_llm -= 1
        return '{"summary": "bounded"}'

    engine = MultiTierLLMEngine()
    engine._semaphore = asyncio.Semaphore(limit)

    def mock_descriptors(prompt: str, schema_json: str) -> List:
        return [("Tier 1", "gemini", ["key1"], lambda k: mock_call(k))]

    monkeypatch.setattr(engine, "_tier_descriptors", mock_descriptors)
    monkeypatch.setattr(engine, "_acquire_tier_slot", lambda *a, **kw: asyncio.sleep(0, result="key1"))

    # Act: launch 18 concurrent extract_structured requests
    tasks = [
        engine.extract_structured(f"Input text payload number {i}", _DummySchema)
        for i in range(18)
    ]
    results = await asyncio.gather(*tasks)

    # Assert
    assert len(results) == 18
    assert all(r.summary == "bounded" for r in results)
    assert max_active_llm <= limit, f"Max active LLM calls ({max_active_llm}) exceeded limit ({limit})"
    assert max_active_llm > 1, "Expected concurrency to be exercised under load"

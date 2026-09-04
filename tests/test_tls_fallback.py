"""
Unit tests for anti-bot TLS impersonation fallback in AsyncBaseCrawler.
Follows AAA pattern with offline respx mocking per CODING_STANDARDS.md.
"""

import json

import httpx
import pytest
import respx

from src.crawlers.base import AsyncBaseCrawler


class DummyCrawler(AsyncBaseCrawler):
    async def crawl(self):
        return []


@pytest.mark.asyncio
@respx.mock
async def test_fetch_escalates_to_tls_on_403_block():
    # Arrange
    test_url = "https://example.com/blocked-page"
    respx.get(test_url).mock(return_value=httpx.Response(403))
    crawler = DummyCrawler()
    fallback_called = False

    async def fake_fetch_tls(url, params=None, timeout=None):
        nonlocal fallback_called
        fallback_called = True
        assert url == test_url
        return "TLS-fetched content"

    crawler.fetch_tls = fake_fetch_tls  # type: ignore[method-assign]

    # Act
    content = await crawler.fetch(test_url)
    await crawler.close()

    # Assert
    assert fallback_called is True
    assert content == "TLS-fetched content"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_json_escalates_to_tls_on_403_block():
    # Arrange
    test_url = "https://example.com/blocked-api"
    expected_payload = {"status": "ok", "value": 42}
    respx.get(test_url).mock(return_value=httpx.Response(403))
    crawler = DummyCrawler()

    async def fake_fetch_tls(url, params=None, timeout=None):
        assert url == test_url
        return json.dumps(expected_payload)

    crawler.fetch_tls = fake_fetch_tls  # type: ignore[method-assign]

    # Act
    parsed_payload = await crawler.fetch_json(test_url)
    await crawler.close()

    # Assert
    assert parsed_payload == expected_payload


@pytest.mark.asyncio
@respx.mock
async def test_fetch_does_not_escalate_on_other_status_codes():
    # Arrange
    test_url = "https://example.com/server-error"
    route = respx.get(test_url).mock(return_value=httpx.Response(500))
    crawler = DummyCrawler()
    fallback_called = False

    async def fake_fetch_tls(url, params=None, timeout=None):
        nonlocal fallback_called
        fallback_called = True
        return "should not happen"

    crawler.fetch_tls = fake_fetch_tls  # type: ignore[method-assign]

    # Act & Assert - 500 triggers TransientNetworkError retry path, not TLS escalation
    with pytest.raises(Exception):
        await crawler.fetch(test_url)
    await crawler.close()
    assert fallback_called is False
    assert route.call_count >= 1

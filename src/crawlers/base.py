"""
Base asynchronous crawler infrastructure.
Provides connection pooling, concurrency throttling, tenacity exponential backoff jitter,
SSRF URL sanitization, and curl-cffi TLS impersonation fallback.
"""

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from datetime import datetime
import feedparser
import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
)

from src.config import settings
from src.utils.date_normalizer import validate_freshness_24h
from src.utils.logger import logger
from src.utils.security import validate_url_safe

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}


class TransientNetworkError(Exception):
    """Raised on 429, 500, 502, 503, 504, or network disconnects."""
    pass


class BotBlockedError(Exception):
    """Raised on HTTP 403: target rejected the client fingerprint (anti-bot wall)."""
    pass


_CRAWLER_RETRY = retry(
    wait=wait_exponential_jitter(initial=1.0, max=30.0, exp_base=2, jitter=1.0),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(TransientNetworkError),
    reraise=True,
)


async def _handle_retry_after(headers: Any, url: str) -> None:
    """Inspect and sleep on HTTP 429 Retry-After header, capped at 30 seconds."""
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            wait_time = min(float(retry_after), 30.0)
            logger.warning(f"HTTP 429 for {url}. Sleeping {wait_time}s per Retry-After header.")
            await asyncio.sleep(wait_time)
        except ValueError:
            pass


class AsyncBaseCrawler(ABC):
    """Abstract base class for all asynchronous data crawlers."""

    def __init__(
        self,
        concurrency_limit: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.concurrency_limit = concurrency_limit or settings.max_concurrent_requests
        self.semaphore = asyncio.Semaphore(self.concurrency_limit)
        self.timeout = timeout_seconds or settings.default_request_timeout_seconds
        self.headers = headers or DEFAULT_HEADERS.copy()
        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        """Lazily initialize connection-pooled httpx async client."""
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=self.concurrency_limit * 2)
            self._client = httpx.AsyncClient(
                headers=self.headers,
                timeout=self.timeout,
                limits=limits,
                follow_redirects=True,
                http2=True,
            )
        return self._client

    async def close(self) -> None:
        """Gracefully close HTTP client connections."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("AsyncBaseCrawler httpx client session closed.")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit with guaranteed resource cleanup."""
        await self.close()

    @_CRAWLER_RETRY
    async def _request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        """Core HTTP request with SSRF validation, semaphore bounds, and retry backoff."""
        safe_url = await asyncio.to_thread(validate_url_safe, url)
        client = await self.get_client()
        req_headers = {**self.headers, **(headers or {})}
        req_timeout = timeout if timeout is not None else self.timeout

        async with self.semaphore:
            try:
                resp = await client.get(safe_url, params=params, headers=req_headers, timeout=req_timeout)
                if resp.status_code == 403:
                    raise BotBlockedError(f"HTTP 403 for {safe_url}")
                if resp.status_code in (429, 500, 502, 503, 504):
                    if resp.status_code == 429:
                        await _handle_retry_after(resp.headers, safe_url)
                    raise TransientNetworkError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as net_err:
                raise TransientNetworkError(repr(net_err)) from net_err

    async def fetch(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Fetch URL content as text string; escalates to TLS impersonation on 403 anti-bot blocks."""
        try:
            return (await self._request(url, params=params, headers=headers, timeout=timeout)).text
        except BotBlockedError:
            logger.warning(f"Anti-bot block (403) on {url}. Escalating to curl-cffi TLS impersonation.")
            return await self.fetch_tls(url, params=params)

    async def fetch_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        allow_tls_fallback: bool = True,
    ) -> Any:
        """Fetch URL and return parsed JSON; escalates to TLS impersonation on 403 anti-bot blocks."""
        try:
            return (await self._request(url, params=params, headers=headers, timeout=timeout)).json()
        except BotBlockedError:
            if not allow_tls_fallback:
                raise
            logger.warning(f"Anti-bot block (403) on {url}. Escalating to curl-cffi TLS impersonation.")
            return json.loads(await self.fetch_tls(url, params=params))

    @_CRAWLER_RETRY
    async def fetch_tls(self, url: str, params: Optional[Dict[str, Any]] = None) -> str:
        """Fetch using curl-cffi with Chrome124 TLS fingerprint impersonation."""
        safe_url = validate_url_safe(url)
        async with self.semaphore:
            try:
                async with CurlAsyncSession(impersonate="chrome124") as curl_session:
                    resp = await curl_session.get(
                        safe_url, params=params, headers=self.headers, timeout=int(self.timeout)
                    )
                    if resp.status_code == 403:
                        raise BotBlockedError(f"curl-cffi HTTP 403 for {safe_url}")
                    if resp.status_code in (429, 500, 502, 503, 504):
                        if resp.status_code == 429:
                            await _handle_retry_after(resp.headers, safe_url)
                        raise TransientNetworkError(f"curl-cffi HTTP {resp.status_code}")
                    return resp.text
            except (BotBlockedError, TransientNetworkError):
                raise
            except Exception as exc:
                raise TransientNetworkError(str(exc)) from exc

    async def fetch_feed(self, url: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
        """Fetch XML content and parse Atom/RSS feed entries with SSRF protection."""
        feed = feedparser.parse(await self.fetch(url, params=params))
        return getattr(feed, "entries", [])

    @staticmethod
    def check_freshness(date_val: Any) -> Optional[datetime]:
        """Parse date and enforce 24h freshness gate via centralized date_normalizer."""
        return validate_freshness_24h(date_val)

    @abstractmethod
    async def crawl(self) -> List[Any]:
        """Execute crawler workload. Must be implemented by concrete subclasses."""
        pass


class TargetedCrawler(AsyncBaseCrawler):
    """Base class for crawlers collecting up to a target quota with deduplication."""

    def __init__(self, target_count: int = 1000, **kwargs):
        super().__init__(**kwargs)
        self.target_count = target_count
        self.seen_keys: set = set()
        self.collected: List[Any] = []

    async def crawl(self) -> List[Any]:
        """Default crawl returning collected records up to target quota."""
        return self.collected[:self.target_count]

    @property
    def is_full(self) -> bool:
        """Check if collected items reached the target quota."""
        return len(self.collected) >= self.target_count

    @property
    def remaining(self) -> int:
        """Remaining slots to reach target quota."""
        return max(0, self.target_count - len(self.collected))

    def is_seen(self, key: Optional[str]) -> bool:
        """Check if key was already seen, or register and return False."""
        if not key:
            return True
        k = key.strip().lower()
        if not k or k in self.seen_keys:
            return True
        self.seen_keys.add(k)
        return False

    def add(self, key: Optional[str], item: Any) -> bool:
        """Add item to collected list if not seen and quota not reached. Returns True if full."""
        if self.is_full or self.is_seen(key):
            return self.is_full
        self.collected.append(item)
        return self.is_full

    @property
    def seen_names(self) -> set:
        return self.seen_keys

    @seen_names.setter
    def seen_names(self, val: set) -> None:
        self.seen_keys = val

    @property
    def seen_products(self) -> set:
        return self.seen_keys

    @seen_products.setter
    def seen_products(self, val: set) -> None:
        self.seen_keys = val

    @property
    def _seen_urls(self) -> set:
        return self.seen_keys

    @_seen_urls.setter
    def _seen_urls(self, val: set) -> None:
        self.seen_keys = val


"""
Base asynchronous crawler infrastructure.
Provides connection pooling, concurrency throttling, tenacity exponential backoff jitter,
SSRF URL sanitization, and curl-cffi TLS impersonation fallback.
"""

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import email.utils
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


def github_headers(token: Optional[str]) -> Dict[str, str]:
    """Build GitHub REST API headers, authenticating when a token is available."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


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
    """Inspect and sleep on HTTP 429 Retry-After header (seconds or RFC 7231 HTTP-date), capped at 30 seconds."""
    retry_after = headers.get("Retry-After") if headers else None
    if not retry_after:
        return
    wait_time: Optional[float] = None
    try:
        wait_time = float(retry_after)
    except (ValueError, TypeError):
        try:
            target_dt = email.utils.parsedate_to_datetime(str(retry_after))
            if target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=timezone.utc)
            now_dt = datetime.now(timezone.utc)
            wait_time = (target_dt - now_dt).total_seconds()
        except Exception as exc:
            logger.debug(f"Could not parse Retry-After HTTP-date '{retry_after}': {exc}")

    if wait_time is not None:
        capped_wait = max(0.0, min(wait_time, 30.0))
        logger.warning(
            f"HTTP 429 for {url}. Sleeping {capped_wait:.1f}s per Retry-After header ('{retry_after}')."
        )
        await asyncio.sleep(capped_wait)


class AsyncBaseCrawler(ABC):
    """Abstract base class for all asynchronous data crawlers."""

    escalation_attempts: int = 0
    escalation_successes: int = 0

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
        self._curl_session: Optional[CurlAsyncSession] = None

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

    async def get_curl_session(self) -> CurlAsyncSession:
        """Lazily initialize connection-pooled curl-cffi async session."""
        if self._curl_session is None or getattr(self._curl_session, "_closed", False):
            self._curl_session = CurlAsyncSession(impersonate="chrome124")
        return self._curl_session

    async def close(self) -> None:
        """Gracefully close HTTP and curl-cffi client connections."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.debug("AsyncBaseCrawler httpx client session closed.")
        if self._curl_session is not None:
            try:
                await self._curl_session.close()
            except Exception as exc:
                logger.debug(f"Error closing curl-cffi session: {exc}")
            self._curl_session = None
            logger.debug("AsyncBaseCrawler curl-cffi session closed.")

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
        allow_retry: bool = True,
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
                    if not allow_retry:
                        raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    raise TransientNetworkError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as net_err:
                if not allow_retry:
                    raise net_err
                raise TransientNetworkError(repr(net_err)) from net_err

    async def _escalate_camoufox(
        self,
        url: str,
        as_json: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        """Tier 3 fallback: Launch hardened headless browser to bypass Cloudflare/bot challenges."""
        safe_url = validate_url_safe(url)
        browser_timeout = float(timeout or 45.0)
        logger.info(f"Escalating to Tier 3 Camoufox headless browser for {safe_url} (timeout={browser_timeout}s)")
        try:
            from camoufox.async_api import AsyncCamoufox

            async def _run_camoufox():
                async with AsyncCamoufox(headless=True, geoip=True) as browser:
                    page = await browser.new_page()
                    await page.goto(safe_url, timeout=int(browser_timeout * 1000))
                    return await page.content()

            content = await asyncio.wait_for(_run_camoufox(), timeout=browser_timeout + 15.0)
            AsyncBaseCrawler.escalation_successes += 1
            return json.loads(content) if as_json else content
        except ImportError as imp_err:
            logger.error(f"Camoufox not installed: {imp_err}")
            raise BotBlockedError(f"Anti-bot block on {safe_url}, and Camoufox is not installed.") from imp_err
        except Exception as exc:
            logger.error(f"Camoufox browser tier failed for {safe_url}: {exc}")
            raise BotBlockedError(f"Anti-bot block on {safe_url}, Camoufox failed: {exc}") from exc

    async def _escalate_tls(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        as_json: bool = False,
    ) -> Any:
        """Escalate an anti-bot 403 to curl-cffi TLS impersonation, falling back to Camoufox if blocked."""
        AsyncBaseCrawler.escalation_attempts += 1
        logger.warning(f"Anti-bot block (403) on {url}. Escalating to curl-cffi TLS impersonation.")
        try:
            res_raw = await self.fetch_tls(url, params=params, timeout=timeout)
            AsyncBaseCrawler.escalation_successes += 1
            return json.loads(res_raw) if as_json else res_raw
        except BotBlockedError:
            logger.warning(f"curl-cffi TLS impersonation blocked for {url}. Escalating to Tier 3 Camoufox.")
            return await self._escalate_camoufox(url, as_json=as_json, timeout=timeout)

    async def fetch(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        allow_tls_fallback: bool = True,
        allow_retry: bool = True,
    ) -> str:
        """Fetch URL content as text string; escalates to TLS impersonation on 403 anti-bot blocks."""
        try:
            return (await self._request(url, params=params, headers=headers, timeout=timeout, allow_retry=allow_retry)).text
        except BotBlockedError:
            if not allow_tls_fallback:
                raise
            return await self._escalate_tls(url, params=params, timeout=timeout)

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
            return await self._escalate_tls(url, params=params, timeout=timeout, as_json=True)

    @_CRAWLER_RETRY
    async def fetch_tls(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Fetch using pooled curl-cffi with Chrome124 TLS fingerprint impersonation."""
        safe_url = validate_url_safe(url)
        req_timeout = int(timeout or self.timeout)
        async with self.semaphore:
            try:
                curl_session = await self.get_curl_session()
                resp = await curl_session.get(
                    safe_url, params=params, headers=self.headers, timeout=req_timeout
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
    """Base class for crawlers collecting up to a target quota with deduplication and append-only WAL."""

    def __init__(
        self,
        target_count: int = 1000,
        wal_enabled: bool = False,
        wal_path: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.target_count = target_count
        self.github_token = settings.github_token
        self.seen_keys: set = set()
        self.collected: List[Any] = []
        self.wal_enabled: bool = (
            wal_enabled or getattr(settings, "enable_wal", False) or (wal_path is not None)
        )
        self.wal_path: Optional[str] = wal_path or (
            str(Path("exports/wal") / f"{self.__class__.__name__.lower()}_wal.jsonl")
            if self.wal_enabled
            else None
        )
        self._wal_file = None

    def _get_wal_file(self):
        """Lazily initialize and open append-only WAL file."""
        if self._wal_file is None and self.wal_enabled and self.wal_path:
            p = Path(self.wal_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._wal_file = open(p, "a", encoding="utf-8")
        return self._wal_file

    def close_wal(self) -> None:
        """Flush and close WAL file handle."""
        if self._wal_file and not self._wal_file.closed:
            try:
                self._wal_file.flush()
                self._wal_file.close()
            except Exception as exc:
                logger.debug(f"Error closing WAL file: {exc}")
            finally:
                self._wal_file = None

    async def close(self) -> None:
        """Gracefully close HTTP client connections and WAL handle."""
        self.close_wal()
        await super().close()

    def __del__(self):
        """Safety cleanup for WAL file descriptor upon garbage collection."""
        self.close_wal()

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

    def _write_wal(self, key: Optional[str], item: Any) -> None:
        """Stream serialized record to append-only WAL."""
        try:
            f = self._get_wal_file()
            if not f:
                return
            if hasattr(item, "model_dump"):
                payload = {"key": key, "data": item.model_dump(mode="json")}
            elif hasattr(item, "dict"):
                payload = {"key": key, "data": item.dict()}
            else:
                payload = {"key": key, "data": item}
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            f.flush()
        except Exception as exc:
            logger.warning(f"WAL write failed for key '{key}': {exc}")

    def add(self, key: Optional[str], item: Any, already_seen: bool = False) -> bool:
        """Add item to collected list if not seen and quota not reached. Returns True if full."""
        if self.is_full:
            return True
        if not already_seen:
            if self.is_seen(key):
                return self.is_full
        else:
            if key:
                self.seen_keys.add(key.strip().lower())
        self.collected.append(item)
        if self.wal_enabled:
            self._write_wal(key, item)
        return self.is_full

    def recover_from_wal(self, model_cls: Optional[Any] = None) -> int:
        """Recover collected records and deduplication keys from WAL. Returns count of recovered items."""
        if not self.wal_path or not Path(self.wal_path).exists():
            return 0
        recovered_count = 0
        try:
            with open(self.wal_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(f"Skipping corrupt WAL entry: {line[:50]}...")
                        continue
                    key = record.get("key")
                    data = record.get("data")
                    if self.is_seen(key):
                        continue
                    if model_cls and isinstance(data, dict):
                        try:
                            item = (
                                model_cls.model_validate(data)
                                if hasattr(model_cls, "model_validate")
                                else model_cls(**data)
                            )
                        except Exception as parse_exc:
                            logger.warning(f"WAL item validation failed: {parse_exc}")
                            item = data
                    else:
                        item = data
                    self.collected.append(item)
                    recovered_count += 1
                    if self.is_full:
                        break
            logger.info(f"Recovered {recovered_count} items from WAL ({self.wal_path}).")
        except Exception as exc:
            logger.error(f"Error recovering from WAL {self.wal_path}: {exc}")
        return recovered_count

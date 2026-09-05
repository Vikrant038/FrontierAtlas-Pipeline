"""
Base asynchronous crawler infrastructure.
Provides connection pooling, concurrency throttling, tenacity exponential backoff jitter,
SSRF URL sanitization, and curl-cffi TLS impersonation fallback.
"""

import asyncio
import json
import zlib
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
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
from src.crawlers.anti_bot import (
    _block_cooldown_until,
    _block_history,
    _breaker_is_open,
    _host_of,
    _looks_like_challenge,
    _record_block,
    _record_escalation_success,
)
from src.utils.date_normalizer import parse_retry_after, validate_freshness_24h
from src.utils.logger import logger
from src.utils.security import validate_url_safe

MAX_RETRY_AFTER_SECONDS = 300.0  # honor provider backoffs up to 5 minutes per attempt

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


def is_github_quota_error(exc: Exception) -> bool:
    """True if a GitHub API exception signals rate-limit/quota exhaustion.
    429 is always quota; 403 counts only when the body mentions rate limits or quota
    (a plain 403 may be a blocked repo or a WAF rejection, which must not disable enrichment)."""
    text = str(exc).lower()
    if "429" in text:
        return True
    return "403" in text and ("rate limit" in text or "quota" in text)


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
    """Inspect and sleep on HTTP 429 Retry-After header (seconds or RFC 7231 HTTP-date), capped at 5 minutes."""
    retry_after = headers.get("Retry-After") if headers else None
    wait_time = parse_retry_after(retry_after)
    if wait_time is not None:
        capped_wait = max(0.0, min(wait_time, MAX_RETRY_AFTER_SECONDS))
        logger.warning(
            f"HTTP 429 for {url}. Sleeping {capped_wait:.1f}s per Retry-After header ('{retry_after}')."
        )
        await asyncio.sleep(capped_wait)


def anti_bot_snapshot(active_crawlers: Optional[List["AsyncBaseCrawler"]] = None) -> Dict[str, Any]:
    """Telemetry for the run report: escalation counters (summed across the provided
    live crawler instances; callers pass their registry since crawlers are instance-
    stateful) and per-host breaker state."""
    crawlers = list(active_crawlers or [])
    return {
        "escalation_attempts": sum(c.escalation_attempts for c in crawlers),
        "escalation_successes": sum(c.escalation_successes for c in crawlers),
        "open_circuits": sorted(_block_cooldown_until.keys()),
        "block_counts": {host: len(hist) for host, hist in _block_history.items() if hist},
    }


class AsyncBaseCrawler(ABC):
    """Abstract base class for all asynchronous data crawlers."""

    def __init__(
        self,
        concurrency_limit: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        # Instance-level escalation telemetry: class attributes were shared across
        # every crawler instance, so one instance's counter read another's blocks.
        self.escalation_attempts: int = 0
        self.escalation_successes: int = 0
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
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as net_err:
                if not allow_retry:
                    raise net_err
                raise TransientNetworkError(repr(net_err)) from net_err

        # Handle status codes OUTSIDE the semaphore slot: honoring a long Retry-After
        # while holding a concurrency slot stalls every other request under 429 storms.
        if resp.status_code == 403:
            try:
                body_hint = resp.text[:200]
            except Exception:
                body_hint = ""
            raise BotBlockedError(f"HTTP 403 for {safe_url}: {body_hint}")
        if resp.status_code in (429, 500, 502, 503, 504):
            if resp.status_code == 429:
                await _handle_retry_after(resp.headers, safe_url)
            if not allow_retry:
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
            raise TransientNetworkError(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp

    async def _escalate_camoufox(
        self,
        url: str,
        as_json: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        """Tier 3 fallback: Launch hardened headless browser to bypass Cloudflare/bot challenges."""
        safe_url = await asyncio.to_thread(validate_url_safe, url)
        if as_json:
            # Camoufox renders HTML (page.content()); JSON parsing of a browser page
            # always fails. A browser launch here would burn ~45s for a guaranteed
            # JSONDecodeError, so fail fast instead.
            raise BotBlockedError(
                f"Anti-bot block on {safe_url}: JSON APIs cannot use the browser tier (HTML-only response)."
            )
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
            if _looks_like_challenge(content):
                raise BotBlockedError(f"Bot challenge page returned by Camoufox for {safe_url}")
            self.escalation_successes += 1
            return content
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
        self.escalation_attempts += 1
        logger.warning(f"Anti-bot block (403) on {url}. Escalating to curl-cffi TLS impersonation.")
        try:
            res_raw = await self.fetch_tls(url, params=params, timeout=timeout)
            if _looks_like_challenge(res_raw):
                # 200-with-challenge is a block, not data; let the Camoufox tier try.
                raise BotBlockedError(f"Bot challenge page returned by TLS tier for {url}")
            self.escalation_successes += 1
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
            return await self._handle_bot_block(url, params=params, timeout=timeout, as_json=False)

    async def _handle_bot_block(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        as_json: bool = False,
    ) -> Any:
        """Circuit-breaker-aware escalation: hosts that keep returning 403 skip the
        TLS/browser tiers (each costs a browser launch) and fail fast to the caller's
        fallback for a cooldown window. Successful escalation resets the host's history."""
        host = _host_of(url)
        if _breaker_is_open(host):
            logger.warning(f"Anti-bot circuit open for {host}; skipping escalation for {url}.")
            raise BotBlockedError(f"Circuit open for {host}; escalation skipped (repeated blocks).")
        if _record_block(host):
            raise BotBlockedError(f"Circuit tripped for {host}; escalation skipped this request.")
        result = await self._escalate_tls(url, params=params, timeout=timeout, as_json=as_json)
        _record_escalation_success(host)
        return result

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
            return await self._handle_bot_block(url, params=params, timeout=timeout, as_json=True)

    @_CRAWLER_RETRY
    async def fetch_tls(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Fetch using pooled curl-cffi with Chrome124 TLS fingerprint impersonation."""
        safe_url = await asyncio.to_thread(validate_url_safe, url)
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
        reset_wal: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.target_count = target_count
        self.github_token = settings.github_token
        self.seen_keys: set = set()
        self.collected: List[Any] = []
        self.reset_wal: bool = reset_wal
        self.wal_enabled: bool = (
            wal_enabled or getattr(settings, "enable_wal", False) or (wal_path is not None)
        )
        self.wal_path: Optional[str] = wal_path or (
            str(Path(settings.wal_dir) / f"{self.__class__.__name__.lower()}_wal.jsonl")
            if (self.wal_enabled or self.reset_wal)
            else None
        )
        self._wal_file = None
        if self.reset_wal and self.wal_path:
            self.truncate_wal()
        # GitHub token pool: GITHUB_TOKENS for scale, GITHUB_TOKEN as the single-key fallback.
        self.github_tokens: List[str] = settings.github_token_list
        self._exhausted_github_tokens: set = set()

    def truncate_wal(self) -> None:
        """Truncate the WAL file so crawler starts completely fresh (ignoring prior runs)."""
        if not self.wal_path:
            return
        try:
            self.close_wal()
            p = Path(self.wal_path)
            if p.exists():
                with open(p, "w", encoding="utf-8") as f:
                    f.truncate(0)
                logger.info(f"Truncated existing WAL {self.wal_path} (reset_wal=True).")
        except OSError as exc:
            logger.warning(f"Could not truncate WAL {self.wal_path}: {exc}")

    def _pick_github_token(self, key: str = "") -> Optional[str]:
        """Choose a non-exhausted GitHub token (stable per key) or None for anonymous mode."""
        available = [t for t in self.github_tokens if t not in self._exhausted_github_tokens]
        if not available:
            return None
        if len(available) == 1:
            return available[0]
        # crc32 (not builtin hash): PYTHONHASHSEED randomizes hash() per process,
        # which would reshuffle token↔repo pairing on every run and break any
        # per-key quota telemetry correlation.
        return available[zlib.crc32((key or "default").encode("utf-8")) % len(available)]

    def _get_wal_file(self):
        """Lazily initialize and open append-only WAL file."""
        if self._wal_file is None and self.wal_enabled and self.wal_path:
            p = Path(self.wal_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # Deliberate lifecycle: held open for streaming WAL appends; closed via close_wal().
            self._wal_file = open(p, "a", encoding="utf-8")  # noqa: SIM115
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

    def reset_wal_if_complete(self) -> None:
        """Truncate the WAL when the run reached its target, so a later run starts fresh
        instead of resuming stale records. Interrupted runs keep the WAL for recovery."""
        if self.wal_enabled and self.is_full and self.wal_path:
            try:
                self.close_wal()
                open(self.wal_path, "w", encoding="utf-8").close()
                logger.info(f"Run complete; truncated WAL {self.wal_path}.")
            except OSError as exc:
                logger.warning(f"Could not truncate WAL {self.wal_path}: {exc}")

    def recover_from_wal(self, model_cls: Optional[Any] = None) -> int:
        """Recover collected records and deduplication keys from WAL. Returns count of recovered items.
        Compacts the WAL to the recovered remainder so replay cost stays bounded across repeated interruptions."""
        if self.reset_wal or not self.wal_path or not Path(self.wal_path).exists():
            return 0
        recovered_count = 0
        remainder: List[Dict[str, Any]] = []
        try:
            with open(self.wal_path, "r", encoding="utf-8") as f:
                for line in f:
                    if self.is_full:
                        break
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
                    remainder.append({"key": key, "data": data})
                    self.collected.append(item)
                    recovered_count += 1
            logger.info(f"Recovered {recovered_count} items from WAL ({self.wal_path}).")
        except Exception as exc:
            logger.error(f"Error recovering from WAL {self.wal_path}: {exc}")
        if self.wal_enabled and recovered_count > 0:
            self._rewrite_wal(remainder)
        return recovered_count

    def _rewrite_wal(self, entries: List[Dict[str, Any]]) -> None:
        """Rewrite the WAL with the given entries (compaction after recovery)."""
        try:
            self.close_wal()
            with open(self.wal_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
        except OSError as exc:
            logger.warning(f"WAL compaction failed for {self.wal_path}: {exc}")

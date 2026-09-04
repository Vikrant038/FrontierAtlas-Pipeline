"""
Provider-level rate limiter enforcing requests-per-minute (RPM) windows across LLM providers.
Protects free-tier API quotas (Gemini 15 RPM, Groq 30 RPM, DeepSeek configurable)
using an async sliding-window queue with explicit pacing telemetry.
"""

import asyncio
import time
from typing import Dict, List, Optional

from src.config import settings
from src.utils.logger import logger


class RateLimitExceededError(Exception):
    """Raised when request would exceed maximum permitted rate-limit wait time."""
    pass


class ProviderRateLimiter:
    """Thread/task-safe sliding-window rate limiter per LLM provider."""

    def __init__(self, rpm_limits: Optional[Dict[str, int]] = None, window_seconds: float = 60.0):
        self._limits: Dict[str, int] = rpm_limits or {
            "gemini": settings.gemini_rpm,
            "groq": settings.groq_rpm,
            "custom": settings.custom_llm_rpm,
            "deepseek": settings.custom_llm_rpm,
        }
        self._window_seconds: float = window_seconds
        self._history: Dict[str, List[float]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, provider: str) -> asyncio.Lock:
        p = provider.lower().strip()
        if p not in self._locks:
            self._locks[p] = asyncio.Lock()
        return self._locks[p]

    def get_limit(self, provider: str) -> int:
        """Get the configured RPM limit for a provider."""
        return self._limits.get(provider.lower().strip(), 15)

    def _try_grant(self, window_id: str, rpm: int) -> Optional[float]:
        """Under the window lock: prune stale timestamps, then either record a grant
        (returns None) or return the seconds until the oldest call expires."""
        if window_id not in self._history:
            self._history[window_id] = []
        now = time.monotonic()
        # Prune timestamps older than window duration
        self._history[window_id] = [t for t in self._history[window_id] if now - t < self._window_seconds]
        if len(self._history[window_id]) < rpm:
            # Capacity available: record call and grant access
            self._history[window_id].append(now)
            return None
        # Window saturated: calculate exact sleep until the oldest call expires
        oldest = self._history[window_id][0]
        return max(0.01, self._window_seconds - (now - oldest) + 0.05)

    async def acquire(
        self,
        provider: str,
        max_wait: Optional[float] = None,
        key: Optional[str] = None,
    ) -> float:
        """
        Request permission to dispatch a call to the specified provider.
        The RPM window is per provider by default; when a pooled API key is supplied,
        each key gets its own independent window, so N keys provide N x the limit.
        If the sliding window is saturated and the required wait exceeds max_wait,
        raises RateLimitExceededError so the caller can fail over immediately;
        otherwise sleeps until capacity frees. Returns the duration slept.
        """
        p = provider.lower().strip()
        window_id = f"{p}:{key}" if key else p
        rpm = self.get_limit(p)
        lock = self._get_lock(window_id)

        slept_total = 0.0
        while True:
            async with lock:
                wait_time = self._try_grant(window_id, rpm)
                if wait_time is None:
                    return slept_total
                if max_wait is not None and wait_time > max_wait:
                    logger.debug(
                        f"Rate limiter: window '{window_id}' saturated ({len(self._history[window_id])}/{rpm} RPM, wait {wait_time:.1f}s > max {max_wait:.1f}s). "
                        "Triggering tier failover."
                    )
                    raise RateLimitExceededError(
                        f"Provider '{window_id}' rate limit window saturated ({len(self._history[window_id])}/{rpm} RPM), "
                        f"required wait {wait_time:.1f}s exceeds max {max_wait:.1f}s"
                    )

            # Sleep outside the lock so other coroutines are not blocked from checking/acquiring
            logger.info(
                f"Rate limiter: pacing {window_id} ({len(self._history[window_id])} requests in last {self._window_seconds:.0f}s, "
                f"limit {rpm} RPM), sleeping {wait_time:.2f}s..."
            )
            await asyncio.sleep(wait_time)
            slept_total += wait_time


# Global rate limiter instance
rate_limiter = ProviderRateLimiter()

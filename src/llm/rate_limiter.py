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

    async def acquire(self, provider: str, max_wait: Optional[float] = None) -> float:
        """
        Request permission to dispatch a call to the specified provider.
        If the sliding 60-second window is saturated:
        - If max_wait is provided and wait_time > max_wait, raises RateLimitExceededError
          to allow caller to failover to secondary providers immediately.
        - Otherwise, sleeps until capacity is freed.
        Returns the duration slept (0.0 if not paced).
        """
        p = provider.lower().strip()
        rpm = self.get_limit(p)
        lock = self._get_lock(p)

        slept_total = 0.0
        while True:
            wait_time = 0.0
            async with lock:
                if p not in self._history:
                    self._history[p] = []

                now = time.monotonic()
                # Prune timestamps older than window duration
                self._history[p] = [t for t in self._history[p] if now - t < self._window_seconds]

                if len(self._history[p]) < rpm:
                    # Capacity available: record call and grant access
                    self._history[p].append(now)
                    return slept_total

                # Window saturated: calculate exact sleep until oldest call expires
                oldest = self._history[p][0]
                wait_time = max(0.01, self._window_seconds - (now - oldest) + 0.05)

                if max_wait is not None and wait_time > max_wait:
                    logger.debug(
                        f"Rate limiter: provider '{p}' saturated ({len(self._history[p])}/{rpm} RPM, wait {wait_time:.1f}s > max {max_wait:.1f}s). "
                        "Triggering tier failover."
                    )
                    raise RateLimitExceededError(
                        f"Provider '{p}' rate limit window saturated ({len(self._history[p])}/{rpm} RPM), "
                        f"required wait {wait_time:.1f}s exceeds max {max_wait:.1f}s"
                    )

            # Sleep outside the lock so other coroutines are not blocked from checking/acquiring
            logger.info(
                f"Rate limiter: pacing {p} ({len(self._history[p])} requests in last {self._window_seconds:.0f}s, "
                f"limit {rpm} RPM), sleeping {wait_time:.2f}s..."
            )
            await asyncio.sleep(wait_time)
            slept_total += wait_time


# Global rate limiter instance
rate_limiter = ProviderRateLimiter()

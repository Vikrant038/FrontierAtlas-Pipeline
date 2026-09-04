"""
Per-host anti-bot circuit breaker and challenge-page detection shared by the
crawler escalation tiers. A host that keeps returning 403 burns one TLS
impersonation + one full browser launch per request; after a threshold of
blocks the escalation tiers are skipped for a cooldown and callers fail fast
to their fallbacks.
"""

import time
from collections import defaultdict, deque
from typing import Any, Dict
from urllib.parse import urlparse

from src.utils.logger import logger

_BLOCK_WINDOW_SECONDS = 600.0
_BLOCK_THRESHOLD = 3
_BLOCK_COOLDOWN_SECONDS = 1800.0

_block_history: Dict[str, deque] = defaultdict(deque)
_block_cooldown_until: Dict[str, float] = {}

# Bot-challenge interstitials (Cloudflare/DDoS-Guard/CAPTCHA) can come back as HTTP 200;
# they are short pages carrying verification markers. Treat them as blocks instead of data.
_CHALLENGE_MARKERS = (
    "captcha",
    "cf-challenge",
    "cf-chl-",
    "challenge-platform",
    "verify you are human",
    "unusual traffic",
    "attention required",
    "are you a robot",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "ddos-guard",
)
_MAX_CHALLENGE_PAGE_CHARS = 5000


def _host_of(url: str) -> str:
    """Extract the lowercased host from a URL for breaker bookkeeping."""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return url or ""


def _breaker_is_open(host: str) -> bool:
    """True while the host is in cooldown after tripping the breaker."""
    return time.monotonic() < _block_cooldown_until.get(host, 0.0)


def _record_block(host: str) -> bool:
    """Record an anti-bot block; returns True if this block trips the breaker."""
    now = time.monotonic()
    hist = _block_history[host]
    hist.append(now)
    while hist and now - hist[0] > _BLOCK_WINDOW_SECONDS:
        hist.popleft()
    if len(hist) >= _BLOCK_THRESHOLD:
        _block_cooldown_until[host] = now + _BLOCK_COOLDOWN_SECONDS
        hist.clear()
        logger.warning(
            f"Anti-bot circuit breaker tripped for {host}: {_BLOCK_THRESHOLD} blocks within "
            f"{_BLOCK_WINDOW_SECONDS:.0f}s; skipping escalation for {_BLOCK_COOLDOWN_SECONDS / 60:.0f} min."
        )
        return True
    return False


def _record_escalation_success(host: str) -> None:
    """A successful escalation means the block was transient; reset the host's history."""
    _block_history[host].clear()


def _looks_like_challenge(content: Any) -> bool:
    """Heuristic: bot-challenge interstitials are short pages carrying verification markers."""
    if not isinstance(content, str) or not content:
        return False
    if len(content) > _MAX_CHALLENGE_PAGE_CHARS:
        return False
    lower = content.lower()
    return any(marker in lower for marker in _CHALLENGE_MARKERS)

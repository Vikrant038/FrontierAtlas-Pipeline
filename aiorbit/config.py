"""
Configuration loader for AIOrbit (Stage 2).
Reads only AIORBIT_* environment variables from .env.
Completely isolated; zero imports from src/.
"""

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Load local .env file
env_path = Path(".env")
if env_path.exists():
    load_dotenv(dotenv_path=env_path)


class AIOrbitSettings:
    """Self-contained settings for AIOrbit crawler and LLM scoring chain."""

    def __init__(self) -> None:
        # GitHub credentials
        self.github_token: Optional[str] = (
            os.getenv("AIORBIT_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        )
        # GitHub token POOL: AIORBIT_GITHUB_TOKENS / GITHUB_TOKENS (comma-separated)
        # falls back to the single token. Deduped, order-preserving.
        _gh_raw = (
            (os.getenv("AIORBIT_GITHUB_TOKENS") or "")
            + ","
            + (os.getenv("GITHUB_TOKENS") or "")
            + ","
            + (self.github_token or "")
        )
        _gh_seen: set = set()
        _gh_list: list = []
        for _t in _gh_raw.split(","):
            _t = _t.strip()
            if _t and _t not in _gh_seen:
                _gh_seen.add(_t)
                _gh_list.append(_t)
        self.github_tokens: list[str] = _gh_list

        # Groq credentials & key pool
        raw_keys = (
            (os.getenv("AIORBIT_GROQ_API_KEYS") or "")
            + ","
            + (os.getenv("GROQ_API_KEYS") or "")
            + ","
            + (os.getenv("AIORBIT_GROQ_API_KEY") or "")
            + ","
            + (os.getenv("GROQ_API_KEY") or "")
        )
        seen_keys = set()
        keys_list = []
        for k in raw_keys.split(","):
            k_clean = k.strip()
            if k_clean and k_clean not in seen_keys and k_clean.startswith("gsk_"):
                seen_keys.add(k_clean)
                keys_list.append(k_clean)
        self.groq_api_keys: list[str] = keys_list
        self.groq_api_key: Optional[str] = self.groq_api_keys[0] if self.groq_api_keys else None

        self.groq_base_url: str = os.getenv(
            "AIORBIT_GROQ_BASE_URL", "https://api.groq.com/openai/v1"
        )
        self.groq_model_tier1: str = os.getenv(
            "AIORBIT_GROQ_MODEL_TIER1", "groq/compound"
        )
        self.groq_model_tier2: str = os.getenv(
            "AIORBIT_GROQ_MODEL_TIER2", "openai/gpt-oss-120b"
        )
        self.groq_model_tier3: str = os.getenv(
            "AIORBIT_GROQ_MODEL_TIER3", "openai/gpt-oss-20b"
        )
        self.groq_model_tier4: str = os.getenv(
            "AIORBIT_GROQ_MODEL_TIER4", "qwen/qwen3.6-27b"
        )
        self.groq_model_tier5: str = os.getenv(
            "AIORBIT_GROQ_MODEL_TIER5", "qwen/qwen3.8-27b"
        )

        # Fallback Gateway credentials & models
        self.fallback_api_key: Optional[str] = (
            os.getenv("AIORBIT_FALLBACK_API_KEY") or os.getenv("CUSTOM_LLM_API_KEY")
        )
        self.fallback_base_url: str = os.getenv(
            "AIORBIT_FALLBACK_BASE_URL", "https://api.b.ai/v1"
        )
        self.fallback_model: str = os.getenv(
            "AIORBIT_FALLBACK_MODEL", "glm-5.3-flash"
        )


settings = AIOrbitSettings()


class GitHubTokenPool:
    """Round-robin rotation across GitHub tokens with per-token 429 cooldown.
    Multiplies the 5,000 req/hr core-API quota linearly across tokens."""

    def __init__(self, tokens: list) -> None:
        self.tokens: list = [t for t in tokens if t]
        self._index: int = 0
        self._cooldown_until: dict = {t: 0.0 for t in self.tokens}
        self.usage: dict = {f"gh_token_{i}": 0 for i in range(len(self.tokens))}

    def next_token(self) -> Optional[str]:
        """Returns the next ready token (round-robin, skipping cooling tokens).
        Returns None only if the pool is empty."""
        if not self.tokens:
            return None
        import time as _time
        n = len(self.tokens)
        for i in range(n):
            idx = (self._index + i) % n
            token = self.tokens[idx]
            if self._cooldown_until.get(token, 0.0) <= _time.time():
                self._index = (idx + 1) % n
                self.usage[f"gh_token_{idx}"] = self.usage.get(f"gh_token_{idx}", 0) + 1
                return token
        # All cooling: return the one with the earliest expiry (caller may wait)
        import time as _time
        best = min(self.tokens, key=lambda t: self._cooldown_until.get(t, 0.0))
        return best

    def set_cooldown(self, token: str, seconds: float) -> None:
        import time as _time
        self._cooldown_until[token] = _time.time() + max(seconds, 1.0)

    def headers_for(self, token: Optional[str] = None) -> dict:
        """Auth headers using the next ready token."""
        t = token or self.next_token()
        h = {"Accept": "application/vnd.github.v3+json"}
        if t:
            h["Authorization"] = f"token {t}"
        return h


github_pool = GitHubTokenPool(settings.github_tokens)

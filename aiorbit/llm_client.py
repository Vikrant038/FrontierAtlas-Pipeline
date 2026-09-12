"""
Standalone LLM client for AIOrbit (Stage 2).
Executes 4-tier Groq fallback chain for MCP curation & scoring:
  Tier 1: Groq gpt-oss-120b (Primary)
  Tier 2: Groq gpt-oss-20b (Secondary fallback on 429/5xx)
  Tier 3: Groq qwen/qwen3.6-27b (Tertiary fallback)
  Tier 4: Groq qwen/qwen3.8-27b (Quaternary fallback)
  Tier 5: Custom Gateway glm-5.3-flash (Optional fallback)
  Terminal: UNSCORED (never hallucinate or fabricate scores)

Features:
- Completely standalone; zero imports from src/
- Per-request failure isolation (429 on Tier 1 does not disable Tier 1 globally)
- Exponential backoff with jitter and Retry-After header parsing
- Full telemetry counters for reporting
"""

import asyncio
import json
import random
import re
import time
from typing import Any, Dict, Optional
import httpx

from aiorbit.config import settings
from aiorbit.schema import MCPEntry

SYSTEM_PROMPT = """You are the Lead Technical Curator for AIOrbit, an enterprise knowledge graph of Model Context Protocol (MCP) servers and clients.
Your job is to evaluate MCP projects against the AIOrbit 100-Point Quality Rubric and extract structured metadata.

You must score the following 3 factors:
1. usefulness_score (float, 0.0 to 30.0): Real-world developer/enterprise value. Does it connect to a critical service (DB, Cloud, Browser, DevTools)? Toy demos get <= 10.
2. quality_score (float, 0.0 to 25.0): Functional completeness, exposed tools/resources, robust design, clear documentation.
3. differentiation_score (float, 0.0 to 5.0): Uniqueness vs generic copies.

Also extract:
- key_capabilities: List of 2 to 5 specific tools, resources, or capabilities provided.
- supported_ai_clients: List of compatible clients (e.g., ["Claude Desktop", "Cursor", "Continue", "Goose", "Windsurf"]).
- transport: Transport protocol, one of ["stdio", "sse", "http", "websocket"].
- pricing_type: One of ["Open Source", "Freemium", "Paid", "Proprietary"].
- curation_notes: A 1-2 sentence justification for the scores.

You MUST respond strictly with a valid JSON object matching this structure:
{
  "usefulness_score": 25.0,
  "quality_score": 20.0,
  "differentiation_score": 4.0,
  "key_capabilities": ["Query MySQL databases", "Schema introspection"],
  "supported_ai_clients": ["Claude Desktop", "Cursor"],
  "transport": "stdio",
  "pricing_type": "Open Source",
  "curation_notes": "Production-grade connector with active maintenance and clean tool abstractions."
}
"""


class KeyPoolManager:
    """Load balances requests across multiple API keys with cooldown tracking
    and proactive per-key sliding-window RPM limiting."""

    RPM_WINDOW_S = 60.0
    RPM_LIMIT = 28  # safety margin under Groq free-tier 30 RPM

    def __init__(self, keys: list[str]) -> None:
        self.keys: list[str] = keys if keys else []
        self._index: int = 0
        self._cooldowns: Dict[str, float] = {k: 0.0 for k in self.keys}
        # Sliding window of recent request timestamps per key (proactive RPM cap)
        self._request_times: Dict[str, list] = {k: [] for k in self.keys}
        self.key_usage: Dict[str, int] = {f"key_{i}": 0 for i in range(len(self.keys))}

    def _rpm_ready(self, key: str, now: float) -> bool:
        """True if this key has RPM budget left in its sliding window."""
        window = self._request_times.setdefault(key, [])
        cutoff = now - self.RPM_WINDOW_S
        while window and window[0] < cutoff:
            window.pop(0)
        return len(window) < self.RPM_LIMIT

    def _record_request(self, key: str, now: float) -> None:
        self._request_times.setdefault(key, []).append(now)

    def get_candidate_keys(self) -> list[tuple[int, str]]:
        """Returns keys ordered starting from next round-robin index.
        Keys with RPM budget exhausted are deprioritized behind cooling keys."""
        if not self.keys:
            return []
        n = len(self.keys)
        start = self._index
        self._index = (self._index + 1) % n
        ordered = [( (start + i) % n, self.keys[(start + i) % n] ) for i in range(n)]
        now = time.time()
        ready = [item for item in ordered
                 if self._cooldowns.get(item[1], 0.0) <= now and self._rpm_ready(item[1], now)]
        cooling = [item for item in ordered if item not in ready]
        return ready + cooling

    async def get_candidate_keys_async(self) -> list[tuple[int, str]]:
        """Returns ready keys, or pauses politely for the shortest cooldown if all keys are cooling."""
        if not self.keys:
            return []
        n = len(self.keys)
        start = self._index
        self._index = (self._index + 1) % n
        ordered = [( (start + i) % n, self.keys[(start + i) % n] ) for i in range(n)]
        now = time.time()
        ready = [item for item in ordered
                 if self._cooldowns.get(item[1], 0.0) <= now and self._rpm_ready(item[1], now)]
        if ready:
            return ready
        # All keys cooling or RPM-capped: wait for the nearest recovery moment
        # (min of cooldown expiry and the time the fullest RPM window drains enough)
        import asyncio
        wait_candidates = [self._cooldowns[k] - now for k in self.keys]
        for k in self.keys:
            window = self._request_times.get(k, [])
            if window:
                # oldest request leaves the window (freeing one slot) at window[0] + 60
                wait_candidates.append(window[0] + self.RPM_WINDOW_S - now)
        positive = [w for w in wait_candidates if w > 0]
        wait_s = min(positive) if positive else 0.0
        if 0 < wait_s <= 5.0:
            await asyncio.sleep(wait_s + random.uniform(0.1, 0.4))
            now = time.time()
            ready_after = [item for item in ordered
                           if self._cooldowns.get(item[1], 0.0) <= now and self._rpm_ready(item[1], now)]
            return ready_after or ordered
        return ordered

    def set_cooldown(self, key: str, duration_s: float) -> None:
        self._cooldowns[key] = time.time() + duration_s

    def record_usage(self, key_idx: int) -> None:
        k_name = f"key_{key_idx}"
        self.key_usage[k_name] = self.key_usage.get(k_name, 0) + 1


key_pool = KeyPoolManager(settings.groq_api_keys)


class LLMTelemetry:
    """Telemetry counters tracking multi-tier LLM usage and key distribution."""

    def __init__(self) -> None:
        self.tier1_calls: int = 0
        self.tier2_calls: int = 0
        self.tier3_calls: int = 0
        self.tier4_calls: int = 0
        self.tier5_calls: int = 0
        self.gateway_calls: int = 0
        self.unscored_failures: int = 0

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "tier1_compound": self.tier1_calls,
            "tier2_groq_120b": self.tier2_calls,
            "tier3_groq_20b": self.tier3_calls,
            "tier4_groq_qwen36": self.tier4_calls,
            "tier5_groq_qwen38": self.tier5_calls,
            "tier6_custom_gateway": self.gateway_calls,
            "unscored_failures": self.unscored_failures,
        }
        for k_name, count in key_pool.key_usage.items():
            data[k_name] = count
        return data


telemetry = LLMTelemetry()


def parse_retry_after(headers: httpx.Headers) -> float:
    """Extract Retry-After in seconds from response headers, defaulting to 2.0s."""
    val = headers.get("retry-after")
    if val:
        try:
            return max(float(val), 1.0)
        except ValueError:
            pass
    return 2.0


def is_valid_evaluation_response(res: Optional[Dict[str, Any]]) -> bool:
    """
    Strictly validates LLM response payload to prevent silent zero-score corruption.
    Rejects as failure when:
      - usefulness_score == 0 and quality_score == 0 (degenerate)
      - any factor score is negative or exceeds its max: usefulness [0..30], quality [0..25], diff [0..5]
      - curation_notes is empty, missing, or whitespace
    """
    if not res or not isinstance(res, dict):
        return False

    if "usefulness_score" not in res or "quality_score" not in res or "differentiation_score" not in res:
        return False

    try:
        u = float(res["usefulness_score"])
        q = float(res["quality_score"])
        d = float(res["differentiation_score"])
    except (ValueError, TypeError):
        return False

    # Check factor score bounds: usefulness [0..30], quality [0..25], diff [0..5]
    if not (0.0 <= u <= 30.0 and 0.0 <= q <= 25.0 and 0.0 <= d <= 5.0):
        return False

    # Degenerate zero-both check: reject if both usefulness and quality are 0
    if u == 0.0 and q == 0.0:
        return False

    # curation_notes must be present, non-empty string
    notes = res.get("curation_notes")
    if not notes or not isinstance(notes, str) or not notes.strip():
        return False

    return True


def call_openai_compatible(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    user_prompt: str,
    timeout_s: float = 15.0,
    enforce_json_format: bool = True,
    max_tokens: int = 450,
) -> tuple[Optional[Dict[str, Any]], bool, float]:
    """
    Execute a single completion call to an OpenAI-compatible API.
    Returns: (result_dict_or_None, is_rate_limited, retry_after_seconds)
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if model.startswith("qwen/"):
        payload["reasoning_effort"] = "none"
        payload["max_tokens"] = min(max_tokens, 250)
    elif model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"
        payload["max_tokens"] = min(max_tokens, 350)
    elif model.startswith("groq/compound"):
        payload["max_tokens"] = min(max_tokens, 350)

    if enforce_json_format:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = client.post(url, headers=headers, json=payload, timeout=timeout_s)
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\n?|\n?```$", "", content, flags=re.MULTILINE).strip()
            return json.loads(content), False, 0.0
        elif resp.status_code == 429:
            retry_s = parse_retry_after(resp.headers)
            return None, True, retry_s
        return None, False, 0.0
    except Exception:
        return None, False, 0.0


def evaluate_mcp_candidate(
    entry: MCPEntry,
    client: Optional[httpx.Client] = None,
) -> Optional[Dict[str, Any]]:
    """
    Evaluate an MCP candidate through the multi-key Groq fallback chain:
      Tier 1: groq/compound
      Tier 2: openai/gpt-oss-120b
      Tier 3: openai/gpt-oss-20b
      Tier 4: qwen/qwen3.6-27b
      Tier 5: qwen/qwen3.8-27b
      Tier 6: Custom Gateway glm-5.3-flash
      Terminal: None (UNSCORED)
    """
    http_client = client or httpx.Client()
    user_prompt = f"""Evaluate this Model Context Protocol project and return the JSON evaluation:
- Name: {entry.name}
- Type: {entry.mcp_type}
- Category: {entry.category or 'Unknown'}
- GitHub Repo: {entry.github_owner_repo or 'None'}
- Stars: {entry.github_stars or 0}
- Description: {entry.description or 'None'}
- Official URL: {entry.official_url or 'None'}
- License: {entry.license or 'None'}
"""
    groq_tiers = [
        (settings.groq_model_tier4, "tier4_groq_qwen36", 10.0),
        (settings.groq_model_tier5, "tier5_groq_qwen38", 10.0),
        (settings.groq_model_tier2, "tier2_groq_120b", 12.0),
        (settings.groq_model_tier3, "tier3_groq_20b", 10.0),
        (settings.groq_model_tier1, "tier1_compound", 12.0),
    ]

    # Keys OUTER, model-tiers INNER (mirrors the async path): every key gets its
    # full tier ladder before falling to the next key.
    for key_idx, api_key in key_pool.get_candidate_keys():
        for model_name, tier_label, timeout_s in groq_tiers:
            res, is_429, retry_s = call_openai_compatible(
                http_client,
                base_url=settings.groq_base_url,
                api_key=api_key,
                model=model_name,
                user_prompt=user_prompt,
                timeout_s=timeout_s,
                enforce_json_format=True,
            )
            if is_valid_evaluation_response(res):
                key_pool.record_usage(key_idx)
                key_pool._record_request(api_key, time.time())
                if tier_label == "tier1_compound":
                    telemetry.tier1_calls += 1
                elif tier_label == "tier2_groq_120b":
                    telemetry.tier2_calls += 1
                elif tier_label == "tier3_groq_20b":
                    telemetry.tier3_calls += 1
                elif tier_label == "tier4_groq_qwen36":
                    telemetry.tier4_calls += 1
                elif tier_label == "tier5_groq_qwen38":
                    telemetry.tier5_calls += 1
                res["serving_key"] = f"key_{key_idx}"
                res["serving_tier"] = model_name
                return res
            if is_429:
                key_pool.set_cooldown(api_key, retry_s + random.uniform(0.2, 0.8))
                break  # key cooling — next key, not next model

    # Tier 6: Custom Gateway
    if settings.fallback_api_key:
        res, _, _ = call_openai_compatible(
            http_client,
            base_url=settings.fallback_base_url,
            api_key=settings.fallback_api_key,
            model=settings.fallback_model,
            user_prompt=user_prompt,
            timeout_s=12.0,
            enforce_json_format=False,
        )
        if is_valid_evaluation_response(res):
            telemetry.gateway_calls += 1
            res["serving_key"] = "gateway"
            res["serving_tier"] = settings.fallback_model
            return res

    # Terminal Fallback: UNSCORED
    telemetry.unscored_failures += 1
    return None


async def call_openai_compatible_async(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    user_prompt: str,
    timeout_s: float = 15.0,
    enforce_json_format: bool = True,
    max_tokens: int = 450,
) -> tuple[Optional[Dict[str, Any]], bool, float]:
    """Execute an asynchronous completion call to an OpenAI-compatible API."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if model.startswith("qwen/"):
        payload["reasoning_effort"] = "none"
        payload["max_tokens"] = min(max_tokens, 250)
    elif model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"
        payload["max_tokens"] = min(max_tokens, 350)
    elif model.startswith("groq/compound"):
        payload["max_tokens"] = min(max_tokens, 350)

    if enforce_json_format:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=timeout_s)
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\n?|\n?```$", "", content, flags=re.MULTILINE).strip()
            return json.loads(content), False, 0.0
        elif resp.status_code == 429:
            retry_s = parse_retry_after(resp.headers)
            return None, True, retry_s
        # Fatal statuses: bad key or model unavailable — caller must not retry this pair
        if resp.status_code in (401, 403, 404):
            return None, False, 0.0
        return None, False, 0.0
    except Exception:
        return None, False, 0.0


async def evaluate_mcp_candidate_async(
    entry: MCPEntry,
    client: httpx.AsyncClient,
) -> Optional[Dict[str, Any]]:
    """
    Asynchronously evaluate an MCP candidate through the multi-key Groq fallback chain:
      Tier 1: groq/compound
      Tier 2: openai/gpt-oss-120b
      Tier 3: openai/gpt-oss-20b
      Tier 4: qwen/qwen3.6-27b
      Tier 5: qwen/qwen3.8-27b
      Tier 6: Custom Gateway glm-5.3-flash
      Terminal: None (UNSCORED)
    """
    user_prompt = f"""Evaluate this Model Context Protocol project and return the JSON evaluation:
- Name: {entry.name}
- Type: {entry.mcp_type}
- Category: {entry.category or 'Unknown'}
- GitHub Repo: {entry.github_owner_repo or 'None'}
- Stars: {entry.github_stars or 0}
- Description: {entry.description or 'None'}
- Official URL: {entry.official_url or 'None'}
- License: {entry.license or 'None'}
"""
    groq_tiers = [
        (settings.groq_model_tier4, "tier4_groq_qwen36", 10.0),
        (settings.groq_model_tier5, "tier5_groq_qwen38", 10.0),
        (settings.groq_model_tier2, "tier2_groq_120b", 12.0),
        (settings.groq_model_tier3, "tier3_groq_20b", 10.0),
        (settings.groq_model_tier1, "tier1_compound", 12.0),
    ]

    # Keys OUTER, model-tiers INNER: every key gets its full tier ladder before
    # we fall to the next key. A 429 cools that key and continues down the same
    # key's models, then moves on — no record dies while any key has quota left.
    #
    # WAIT-AND-RETRY: when every key×model attempt 429s (transient quota window),
    # sleep for the Retry-After window and re-walk the ladder instead of failing
    # the record. Terminal UNSCORED only after MAX_LADDER_ROUNDS exhausted rounds.
    MAX_LADDER_ROUNDS = 4
    round_delays = [0.0, 6.0, 15.0, 30.0]  # base sleep before each retry round

    for round_idx in range(MAX_LADDER_ROUNDS):
        if round_idx > 0:
            await asyncio.sleep(round_delays[min(round_idx, len(round_delays) - 1)] + random.uniform(0.5, 2.0))
        candidate_keys = await key_pool.get_candidate_keys_async()
        for key_idx, api_key in candidate_keys:
            for model_name, tier_label, timeout_s in groq_tiers:
                res, is_429, retry_s = await call_openai_compatible_async(
                    client,
                    base_url=settings.groq_base_url,
                    api_key=api_key,
                    model=model_name,
                    user_prompt=user_prompt,
                    timeout_s=timeout_s,
                    enforce_json_format=True,
                )
                if is_valid_evaluation_response(res):
                    key_pool.record_usage(key_idx)
                    key_pool._record_request(api_key, time.time())
                    if tier_label == "tier1_compound":
                        telemetry.tier1_calls += 1
                    elif tier_label == "tier2_groq_120b":
                        telemetry.tier2_calls += 1
                    elif tier_label == "tier3_groq_20b":
                        telemetry.tier3_calls += 1
                    elif tier_label == "tier4_groq_qwen36":
                        telemetry.tier4_calls += 1
                    elif tier_label == "tier5_groq_qwen38":
                        telemetry.tier5_calls += 1
                    res["serving_key"] = f"key_{key_idx}"
                    res["serving_tier"] = model_name
                    return res
                if is_429:
                    # Per-minute quota: cooldown just past the Retry-After window,
                    # then keep trying OTHER models on this same key within this round.
                    key_pool.set_cooldown(api_key, max(retry_s, 2.0) + random.uniform(0.2, 0.8))
                    continue
        # Round exhausted without a valid response — loop to next round (wait + retry).
        # If every key was 429-cooling and none were even attempted, get_candidate_keys_async
        # already waited; the round delay above adds the patient backoff.

    # Tier 6: Custom Gateway (after all Groq rounds exhausted)
    if settings.fallback_api_key:
        res, _, _ = await call_openai_compatible_async(
            client,
            base_url=settings.fallback_base_url,
            api_key=settings.fallback_api_key,
            model=settings.fallback_model,
            user_prompt=user_prompt,
            timeout_s=25.0,
            enforce_json_format=False,
        )
        if is_valid_evaluation_response(res):
            telemetry.gateway_calls += 1
            res["serving_key"] = "gateway"
            res["serving_tier"] = settings.fallback_model
            return res

    # Terminal Fallback: UNSCORED — only after 4 full ladder rounds + gateway.
    # With wait-and-retry this should be rare (persistent daily-cap exhaustion).
    telemetry.unscored_failures += 1
    return None

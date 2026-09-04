"""
Cross-run novelty state for Phase II freshness heuristics.
Persists the set of collected record keys per crawler so a source lacking a strict
date can still be classified: key present in the previous run = not new,
key absent = new since last run.
Also persists per-source freshness history so a source that stops producing
fresh items (dead feed) can be surfaced across runs.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Set


from src.utils.logger import logger

STATE_PATH = os.path.join("exports", "run_state.json")

# Keep the last N per-source fresh-item counts for staleness detection.
_FRESHNESS_HISTORY_LEN = 5


def _read_state(state_path: str) -> Optional[dict]:
    """Read and parse run-state JSON. Returns {} when absent, None when corrupt."""
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)
            return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _persist(crawler_name: str, mutator: Callable[[dict], None], state_path: str) -> None:
    """Atomically mutate run-state under an advisory file lock (fcntl.flock) with
    tempfile + os.replace, preventing lost updates across concurrent crawlers."""
    export_dir = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(export_dir, exist_ok=True)
    lock_path = state_path + ".lock"

    try:
        import fcntl
        has_fcntl = True
    except ImportError:
        has_fcntl = False

    try:
        with open(lock_path, "w", encoding="utf-8") as lock_file:
            if has_fcntl:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                state = _read_state(state_path) or {}
                mutator(state)

                temp_fd, temp_path = tempfile.mkstemp(dir=export_dir, prefix="run_state_", suffix=".tmp")
                try:
                    with open(temp_fd, "w", encoding="utf-8") as temp_file:
                        json.dump(state, temp_file, indent=2)
                        temp_file.flush()
                        os.fsync(temp_file.fileno())
                    os.replace(temp_path, state_path)
                except Exception:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    raise
            finally:
                if has_fcntl:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as state_exc:
        logger.error(f"Failed to persist run state atomically for {crawler_name}: {state_exc}")


def load_seen_keys(crawler_name: str, state_path: str = STATE_PATH) -> Set[str]:
    """Load the seen-key set recorded by the previous run for this crawler."""
    state = _read_state(state_path)
    if state is None:
        logger.warning(f"Run state unreadable; novelty heuristic disabled for {crawler_name}.")
        return set()
    return set(state.get(crawler_name, []))


def save_seen_keys(crawler_name: str, seen_keys: Set[str], state_path: str = STATE_PATH) -> None:
    """
    Persist this run's collected keys for the next run's novelty comparison.
    Atomic and lock-protected; preserves any other crawlers' state in the same file.
    """

    def _mutate(state: dict) -> None:
        state[crawler_name] = sorted(list(seen_keys))

    _persist(crawler_name, _mutate, state_path)


def load_source_freshness(crawler_name: str, state_path: str = STATE_PATH) -> Dict[str, Dict]:
    """Load per-source freshness stamps ({source: {"last_run_utc", "recent_fresh_counts"}})."""
    state = _read_state(state_path)
    if state is None:
        return {}
    data = state.get(f"{crawler_name}_freshness", {})
    return data if isinstance(data, dict) else {}


def save_source_freshness(
    crawler_name: str,
    source_counts: Dict[str, int],
    state_path: str = STATE_PATH,
) -> None:
    """
    Record this run's fresh-item count per source, appending to each source's
    recent history (capped at _FRESHNESS_HISTORY_LEN) so consecutive zero-fresh
    runs can be detected. Atomic and lock-protected like save_seen_keys.
    """

    def _mutate(state: dict) -> None:
        key = f"{crawler_name}_freshness"
        existing = state.get(key, {})
        if not isinstance(existing, dict):
            existing = {}
        now = datetime.now(timezone.utc).isoformat()
        for source, count in source_counts.items():
            entry = existing.get(source, {})
            history = entry.get("recent_fresh_counts", []) if isinstance(entry, dict) else []
            if not isinstance(history, list):
                history = []
            history = (history + [int(count)])[-_FRESHNESS_HISTORY_LEN:]
            existing[source] = {"last_run_utc": now, "recent_fresh_counts": history}
        state[key] = existing

    _persist(crawler_name, _mutate, state_path)
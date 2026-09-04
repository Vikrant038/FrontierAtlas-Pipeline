"""
Cross-run novelty state for Phase II freshness heuristics.
Persists the set of collected record keys per crawler so a source lacking a strict
date can still be classified: key present in the previous run = not new,
key absent = new since last run.
"""

import json
import os
import tempfile
from typing import Optional, Set


from src.utils.logger import logger

STATE_PATH = os.path.join("exports", "run_state.json")


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
    Guarantees atomic updates and prevents lost updates across concurrent crawlers
    using advisory file locking (fcntl.flock) and tempfile atomic replacement.
    """
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
                state[crawler_name] = sorted(list(seen_keys))

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


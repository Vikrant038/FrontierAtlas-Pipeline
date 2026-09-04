"""
Cross-run novelty state for Phase II freshness heuristics.
Persists the set of collected record keys per crawler so a source lacking a strict
date can still be classified: key present in the previous run = not new,
key absent = new since last run.
"""

import json
import os
from typing import Set

from src.utils.logger import logger

STATE_PATH = os.path.join("exports", "run_state.json")


def load_seen_keys(crawler_name: str, state_path: str = STATE_PATH) -> Set[str]:
    """Load the seen-key set recorded by the previous run for this crawler."""
    if not os.path.exists(state_path):
        return set()
    try:
        with open(state_path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        return set(state.get(crawler_name, []))
    except (json.JSONDecodeError, OSError) as state_exc:
        logger.warning(f"Run state unreadable ({state_exc}); novelty heuristic disabled for {crawler_name}.")
        return set()


def save_seen_keys(crawler_name: str, seen_keys: Set[str], state_path: str = STATE_PATH) -> None:
    """Persist this run's collected keys for the next run's novelty comparison."""
    state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (json.JSONDecodeError, OSError):
            state = {}
    state[crawler_name] = sorted(seen_keys)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(state_path)), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file)
    except OSError as state_exc:
        logger.error(f"Failed to persist run state for {crawler_name}: {state_exc}")

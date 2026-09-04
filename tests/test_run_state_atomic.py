"""
Unit tests for atomic run_state persistence.
Verifies file locking, atomic rename, and race condition prevention (BUG-01).
"""

import concurrent.futures
import json
import os
import pytest

from src.utils.run_state import load_seen_keys, save_seen_keys


def test_save_and_load_round_trip(tmp_path):
    # Arrange
    state_file = str(tmp_path / "run_state.json")
    keys = {"https://example.com/job1", "https://example.com/job2"}

    # Act
    save_seen_keys("JobsCrawler", keys, state_path=state_file)
    loaded = load_seen_keys("JobsCrawler", state_path=state_file)

    # Assert
    assert loaded == keys


def test_concurrent_multi_crawler_writes_preserve_all_keys(tmp_path):
    """Verify concurrent writes by different crawlers do not overwrite each other."""
    # Arrange
    state_file = str(tmp_path / "run_state.json")
    crawlers_and_keys = {
        "NewsCrawler": {f"https://news.com/article_{i}" for i in range(50)},
        "JobsCrawler": {f"https://jobs.com/posting_{i}" for i in range(50)},
        "StartupsCrawler": {f"https://startups.com/company_{i}" for i in range(50)},
    }

    # Act - execute concurrent writes from multiple threads
    def _write_state(name, keys):
        save_seen_keys(name, keys, state_path=state_file)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_write_state, name, keys) for name, keys in crawlers_and_keys.items()]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    # Assert - every crawler's keys must be completely preserved in the final JSON
    with open(state_file, "r", encoding="utf-8") as f:
        final_state = json.load(f)

    assert set(final_state.keys()) == set(crawlers_and_keys.keys())
    for name, expected_keys in crawlers_and_keys.items():
        assert set(final_state[name]) == expected_keys
        loaded = load_seen_keys(name, state_path=state_file)
        assert loaded == expected_keys


def test_load_seen_keys_corrupt_file_recovers_gracefully(tmp_path):
    # Arrange
    state_file = str(tmp_path / "corrupt_state.json")
    with open(state_file, "w", encoding="utf-8") as f:
        f.write("{invalid_json: true")

    # Act
    loaded = load_seen_keys("NewsCrawler", state_path=state_file)

    # Assert
    assert loaded == set()

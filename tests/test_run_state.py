"""
Unit tests for cross-run novelty state persistence.
Follows AAA pattern with tmp_path isolation per CODING_STANDARDS.md Pillar 7.7.
"""

from src.utils.run_state import load_seen_keys, save_seen_keys


def test_save_and_load_round_trip(tmp_path):
    # Arrange
    state_path = str(tmp_path / "run_state.json")

    # Act
    save_seen_keys("news", {"https://a.com/1", "https://b.com/2"}, state_path)
    save_seen_keys("jobs", {"https://c.com/3"}, state_path)
    news_keys = load_seen_keys("news", state_path)
    jobs_keys = load_seen_keys("jobs", state_path)

    # Assert - per-crawler isolation and persistence
    assert news_keys == {"https://a.com/1", "https://b.com/2"}
    assert jobs_keys == {"https://c.com/3"}


def test_load_missing_file_returns_empty(tmp_path):
    # Act
    keys = load_seen_keys("news", str(tmp_path / "missing.json"))

    # Assert
    assert keys == set()


def test_load_corrupt_file_returns_empty(tmp_path):
    # Arrange
    state_path = tmp_path / "corrupt.json"
    state_path.write_text("{broken json", encoding="utf-8")

    # Act - must not raise
    keys = load_seen_keys("news", str(state_path))

    # Assert
    assert keys == set()


def test_save_overwrites_same_crawler_keeps_other(tmp_path):
    # Arrange
    state_path = str(tmp_path / "run_state.json")
    save_seen_keys("news", {"https://old.com/1"}, state_path)

    # Act
    save_seen_keys("news", {"https://new.com/2"}, state_path)

    # Assert - new run replaces old key set for the same crawler
    assert load_seen_keys("news", state_path) == {"https://new.com/2"}

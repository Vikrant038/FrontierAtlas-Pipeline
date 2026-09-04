"""
Unit tests for cross-run novelty state persistence.
Follows AAA pattern with tmp_path isolation per CODING_STANDARDS.md Pillar 7.7.
"""

from src.utils.run_state import (
    load_seen_keys,
    load_source_freshness,
    save_seen_keys,
    save_source_freshness,
)


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


def test_source_freshness_round_trip(tmp_path):
    # Arrange
    state_path = str(tmp_path / "run_state.json")

    # Act
    save_source_freshness("news", {"TechCrunch AI": 5, "DeadFeed": 0}, state_path)
    freshness = load_source_freshness("news", state_path)

    # Assert
    assert set(freshness) == {"TechCrunch AI", "DeadFeed"}
    assert freshness["TechCrunch AI"]["recent_fresh_counts"] == [5]
    assert freshness["DeadFeed"]["recent_fresh_counts"] == [0]
    assert "last_run_utc" in freshness["TechCrunch AI"]


def test_freshness_history_appends_and_caps_at_five(tmp_path):
    # Arrange
    state_path = str(tmp_path / "run_state.json")

    # Act: 7 runs -> only the last 5 counts survive
    for i in range(7):
        save_source_freshness("news", {"TechCrunch AI": i}, state_path)

    # Assert
    history = load_source_freshness("news", state_path)["TechCrunch AI"]["recent_fresh_counts"]
    assert history == [2, 3, 4, 5, 6]


def test_freshness_history_tracks_consecutive_zero_runs(tmp_path):
    # Arrange
    state_path = str(tmp_path / "run_state.json")

    # Act: a source with two zero-fresh runs and one source that dipped once
    save_source_freshness("news", {"DeadFeed": 0, "HealthyFeed": 4}, state_path)
    save_source_freshness("news", {"DeadFeed": 0, "HealthyFeed": 0}, state_path)

    # Assert - only DeadFeed shows two consecutive zeros
    assert load_source_freshness("news", state_path)["DeadFeed"]["recent_fresh_counts"] == [0, 0]
    assert load_source_freshness("news", state_path)["HealthyFeed"]["recent_fresh_counts"] == [4, 0]


def test_freshness_and_seen_keys_coexist(tmp_path):
    # Arrange
    state_path = str(tmp_path / "run_state.json")

    # Act
    save_seen_keys("news", {"https://a.com/1"}, state_path)
    save_source_freshness("news", {"TechCrunch AI": 3}, state_path)

    # Assert - neither write clobbers the other
    assert load_seen_keys("news", state_path) == {"https://a.com/1"}
    assert load_source_freshness("news", state_path)["TechCrunch AI"]["recent_fresh_counts"] == [3]


def test_load_source_freshness_missing_or_corrupt_returns_empty(tmp_path):
    # Arrange
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{broken json", encoding="utf-8")

    # Assert
    assert load_source_freshness("news", str(tmp_path / "missing.json")) == {}
    assert load_source_freshness("news", str(corrupt)) == {}

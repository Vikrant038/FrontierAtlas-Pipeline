"""
Unit tests for cross-run novelty state persistence.
Follows AAA pattern with tmp_path isolation per CODING_STANDARDS.md Pillar 7.7.
"""

import os

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


def test_crawler_freshness_writes_injected_path_not_production(tmp_path, monkeypatch):
    # Arrange - regression guard: NewsCrawler/JobsCrawler freshness persistence must
    # land on the Settings-injected path, never the production exports/run_state.json.
    monkeypatch.setattr("src.config.settings.run_state_path", str(tmp_path / "injected.json"))
    prod_path = "exports/run_state.json"
    prod_before = open(prod_path).read() if os.path.exists(prod_path) else None

    from src.crawlers.news_crawler import NewsCrawler
    NewsCrawler(sources=[{"name": "IsolationFeed", "feed_url": "https://feeds.example.com/iso.xml"}])

    # Act - save freshness the way crawl() does
    from src.utils.run_state import save_source_freshness, load_source_freshness
    save_source_freshness("news", {"IsolationFeed": 3})

    # Assert - state went to injected path only
    assert os.path.exists(str(tmp_path / "injected.json"))
    assert load_source_freshness("news")["IsolationFeed"]["recent_fresh_counts"] == [3]
    # Production file untouched byte-for-byte
    prod_after = open(prod_path).read() if os.path.exists(prod_path) else None
    assert prod_after == prod_before


def test_reset_source_freshness_all_and_targeted(tmp_path):
    # Arrange
    state_path = str(tmp_path / "run_state.json")
    save_seen_keys("news", {"https://news.example/1"}, state_path)
    save_source_freshness("news", {"TechCrunch AI": 0, "DeadFeed": 0}, state_path)
    save_source_freshness("jobs", {"RemoteOK AI": 0}, state_path)

    assert len(load_source_freshness("news", state_path)) == 2
    assert len(load_source_freshness("jobs", state_path)) == 1

    # Act 1 - reset only news_freshness
    from src.utils.run_state import reset_source_freshness
    reset_source_freshness("news", state_path=state_path)

    # Assert 1 - news_freshness cleared, jobs_freshness and seen keys intact
    assert load_source_freshness("news", state_path) == {}
    assert len(load_source_freshness("jobs", state_path)) == 1
    assert load_seen_keys("news", state_path) == {"https://news.example/1"}

    # Act 2 - reset all freshness records
    reset_source_freshness(state_path=state_path)

    # Assert 2 - all freshness cleared, seen keys still intact
    assert load_source_freshness("jobs", state_path) == {}
    assert load_seen_keys("news", state_path) == {"https://news.example/1"}


"""
Unit tests for 24-hour signal freshness validation and date normalization.
Uses freezegun for deterministic temporal verification per CODING_STANDARDS.md.
"""

from datetime import datetime, timezone
from freezegun import freeze_time

from src.utils.date_normalizer import is_fresh_24h, parse_datetime_to_utc


@freeze_time("2026-09-03 12:00:00")
def test_relative_date_within_24h_is_accepted():
    # Arrange
    raw_date_str = "2 hours ago"

    # Act
    parsed_dt = parse_datetime_to_utc(raw_date_str)
    assert parsed_dt is not None
    is_fresh, age_hours = is_fresh_24h(parsed_dt)

    # Assert
    assert is_fresh is True
    assert 1.9 <= age_hours <= 2.1


@freeze_time("2026-09-03 12:00:00")
def test_stale_date_older_than_24h_is_rejected():
    # Arrange
    raw_date_str = "3 days ago"

    # Act
    parsed_dt = parse_datetime_to_utc(raw_date_str)
    assert parsed_dt is not None
    is_fresh, age_hours = is_fresh_24h(parsed_dt)

    # Assert
    assert is_fresh is False
    assert age_hours > 24.0


@freeze_time("2026-09-03 12:00:00")
def test_iso_utc_string_parsing():
    # Arrange
    iso_str = "2026-09-03T10:30:00Z"

    # Act
    parsed_dt = parse_datetime_to_utc(iso_str)

    # Assert
    assert parsed_dt is not None
    assert parsed_dt.year == 2026
    assert parsed_dt.month == 9
    assert parsed_dt.day == 3
    assert parsed_dt.hour == 10
    assert parsed_dt.minute == 30
    assert parsed_dt.tzinfo == timezone.utc

    is_fresh, age_hours = is_fresh_24h(parsed_dt)
    assert is_fresh is True
    assert 1.4 <= age_hours <= 1.6


@freeze_time("2026-09-03 12:00:00")
def test_epoch_timestamp_and_validate_freshness():
    # Arrange
    from src.utils.date_normalizer import validate_freshness_24h
    now_ts = datetime(2026, 9, 3, 11, 0, 0, tzinfo=timezone.utc).timestamp()
    stale_ts = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc).timestamp()

    # Act & Assert
    fresh_dt = validate_freshness_24h(now_ts)
    assert fresh_dt is not None
    assert fresh_dt.hour == 11

    stale_dt = validate_freshness_24h(stale_ts)
    assert stale_dt is None

    empty_dt = validate_freshness_24h(None)
    assert empty_dt is None

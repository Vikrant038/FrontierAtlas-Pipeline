"""
Tests for 10-minute clock drift leniency in 24-hour freshness validation (BUG-04).
"""

from datetime import datetime, timedelta, timezone
from freezegun import freeze_time

from src.utils.date_normalizer import (
    MAX_CLOCK_DRIFT_HOURS,
    is_fresh_24h,
    validate_freshness_24h,
)


@freeze_time("2026-09-04 12:00:00")
def test_clock_drift_future_timestamps_within_tolerance():
    # Arrange: Reference is 12:00:00 UTC
    now_utc = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    t_plus_5m = now_utc + timedelta(minutes=5)
    t_plus_9m_30s = now_utc + timedelta(minutes=9, seconds=30)
    t_plus_10m = now_utc + timedelta(minutes=10)

    # Act
    fresh_5m, age_5m = is_fresh_24h(t_plus_5m, reference_now=now_utc)
    fresh_9m30, age_9m30 = is_fresh_24h(t_plus_9m_30s, reference_now=now_utc)
    fresh_10m, age_10m = is_fresh_24h(t_plus_10m, reference_now=now_utc)

    # Assert
    assert fresh_5m is True
    assert age_5m < 0.0  # negative age indicates slight future
    assert fresh_9m30 is True
    assert fresh_10m is True
    assert validate_freshness_24h(t_plus_5m.isoformat()) is not None


@freeze_time("2026-09-04 12:00:00")
def test_clock_drift_future_timestamps_exceeding_tolerance():
    # Arrange: Beyond 10 minutes in future
    now_utc = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    t_plus_11m = now_utc + timedelta(minutes=11)
    t_plus_1h = now_utc + timedelta(hours=1)

    # Act
    fresh_11m, age_11m = is_fresh_24h(t_plus_11m, reference_now=now_utc)
    fresh_1h, age_1h = is_fresh_24h(t_plus_1h, reference_now=now_utc)

    # Assert
    assert fresh_11m is False
    assert fresh_1h is False
    assert validate_freshness_24h(t_plus_11m.isoformat()) is None
    assert validate_freshness_24h(t_plus_1h.isoformat()) is None


@freeze_time("2026-09-04 12:00:00")
def test_freshness_past_boundaries():
    # Arrange
    now_utc = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    t_minus_23h = now_utc - timedelta(hours=23, minutes=50)
    t_minus_24h_5m = now_utc - timedelta(hours=24, minutes=5)

    # Act
    fresh_23h, _ = is_fresh_24h(t_minus_23h, reference_now=now_utc)
    fresh_24h5m, _ = is_fresh_24h(t_minus_24h_5m, reference_now=now_utc)

    # Assert
    assert fresh_23h is True
    assert fresh_24h5m is False
    assert validate_freshness_24h(t_minus_23h.isoformat()) is not None
    assert validate_freshness_24h(t_minus_24h_5m.isoformat()) is None

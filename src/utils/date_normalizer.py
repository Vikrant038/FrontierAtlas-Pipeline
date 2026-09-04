"""
Temporal normalization and 24-hour signal freshness validation engine.
Enforces Phase II freshness requirements from PROJECT_CONTEXT.md.
"""

from datetime import datetime, timezone
from typing import Any, Optional, Tuple
import dateparser
from dateutil import parser as dateutil_parser

from src.utils.logger import logger


def parse_datetime_to_utc(date_val: Any) -> Optional[datetime]:
    """Parse arbitrary date string, datetime object, or epoch timestamp into timezone-aware UTC datetime."""
    if not date_val:
        return None

    if isinstance(date_val, (int, float)):
        try:
            ts = date_val / 1000.0 if date_val > 1e11 else float(date_val)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None

    if isinstance(date_val, datetime):
        return date_val.replace(tzinfo=timezone.utc) if date_val.tzinfo is None else date_val.astimezone(timezone.utc)

    cleaned = str(date_val).strip()
    if not cleaned:
        return None

    # Fast path: dateutil for ISO / RFC structured strings
    try:
        dt = dateutil_parser.parse(cleaned)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass

    # Natural language & relative parsing ("2 hours ago", "yesterday")
    try:
        parsed = dateparser.parse(
            cleaned,
            settings={"TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": True, "PREFER_DATES_FROM": "past"}
        )
        if parsed:
            return parsed.astimezone(timezone.utc)
    except Exception as exc:
        logger.debug(f"Date parsing failed for '{cleaned}': {exc}")

    return None


def is_fresh_24h(
    published_date: datetime,
    reference_now: Optional[datetime] = None,
    max_age_hours: float = 24.0
) -> Tuple[bool, float]:
    """Validate whether published datetime is within the 24-hour freshness boundary."""
    now_utc = reference_now or datetime.now(timezone.utc)
    dt_utc = (
        published_date.replace(tzinfo=timezone.utc)
        if published_date.tzinfo is None
        else published_date.astimezone(timezone.utc)
    )

    age_hours = (now_utc - dt_utc).total_seconds() / 3600.0

    # Allow small 5-minute clock drift, reject future timestamps beyond that
    if age_hours < -0.08:
        logger.warning(f"Future timestamp rejected ({age_hours:.2f}h in future): {published_date}")
        return False, age_hours

    return (0.0 <= age_hours <= max_age_hours), age_hours


def validate_freshness_24h(date_val: Any) -> Optional[datetime]:
    """Parse date and enforce strict 24-hour freshness gate. Returns UTC datetime if fresh, None otherwise."""
    dt = parse_datetime_to_utc(date_val)
    return dt if (dt and is_fresh_24h(dt)[0]) else None

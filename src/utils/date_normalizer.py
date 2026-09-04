"""
Temporal normalization and 24-hour signal freshness validation engine.
Enforces Phase II freshness requirements from PROJECT_CONTEXT.md.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional, Tuple
import dateparser
from bs4 import BeautifulSoup
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


MAX_CLOCK_DRIFT_HOURS: float = 10.0 / 60.0  # 10 minutes tolerance for publisher clock skew


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

    # Allow 10-minute clock drift for publisher server skew, reject future timestamps beyond that
    if age_hours < -MAX_CLOCK_DRIFT_HOURS:
        logger.warning(f"Future timestamp rejected ({age_hours:.2f}h in future): {published_date}")
        return False, age_hours

    return (-MAX_CLOCK_DRIFT_HOURS <= age_hours <= max_age_hours), age_hours


def validate_freshness_24h(date_val: Any) -> Optional[datetime]:
    """Parse date and enforce strict 24-hour freshness gate with 10-min drift tolerance. Returns UTC datetime if fresh, None otherwise."""
    dt = parse_datetime_to_utc(date_val)
    return dt if (dt and is_fresh_24h(dt)[0]) else None


def extract_date_from_html(raw_html: str, page_url: str = "") -> Optional[datetime]:
    """Extract and normalize publication date from HTML metadata tags, JSON-LD, or URL patterns."""
    if not raw_html:
        return None

    # 1. URL pattern match: /YYYY/MM/DD/ or /YYYY-MM-DD/
    if page_url:
        url_match = re.search(r"/(\d{4})[/-](\d{1,2})[/-](\d{1,2})/", page_url)
        if url_match:
            y, m, d = url_match.groups()
            parsed_url_date = parse_datetime_to_utc(f"{y}-{int(m):02d}-{int(d):02d}")
            if parsed_url_date:
                return parsed_url_date

    soup = BeautifulSoup(raw_html[:25000], "html.parser")

    # 2. Meta tags (OpenGraph, Article, Dublin Core)
    meta_keys = [
        ("property", "article:published_time"),
        ("property", "og:article:published_time"),
        ("name", "article:published_time"),
        ("name", "publish_date"),
        ("name", "pubdate"),
        ("name", "date"),
        ("name", "sailthru.date"),
        ("name", "dc.date"),
        ("name", "dc.date.issued"),
    ]
    for attr, key in meta_keys:
        tag = soup.find("meta", attrs={attr: re.compile(f"^{re.escape(key)}$", re.IGNORECASE)})
        if tag and tag.get("content"):
            parsed = parse_datetime_to_utc(tag["content"])
            if parsed:
                return parsed

    # 3. JSON-LD structured data
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "")
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if isinstance(item, dict):
                    for field in ("datePublished", "dateCreated", "uploadDate"):
                        if item.get(field):
                            parsed = parse_datetime_to_utc(item[field])
                            if parsed:
                                return parsed
        except Exception:
            continue

    # 4. <time> tags with datetime attribute
    for time_tag in soup.find_all("time"):
        dt_val = time_tag.get("datetime") or time_tag.get_text()
        if dt_val:
            parsed = parse_datetime_to_utc(dt_val)
            if parsed:
                return parsed

    return None


def infer_content_freshness(content: str, fallback_now: Optional[datetime] = None) -> Optional[datetime]:
    """Intelligently infer publication timestamp from relative recency expressions in text."""
    if not content:
        return None

    lead_text = content[:2000].lower()

    # 1. Specific relative offsets: e.g. "3 hours ago", "45 minutes ago"
    offset_match = re.search(r"\b(\d+\s*(?:hours?|minutes?|secs?|seconds?)\s*ago)\b", lead_text)
    if offset_match:
        parsed = parse_datetime_to_utc(offset_match.group(1))
        if parsed:
            return parsed

    # 2. Generic recency terms
    word_match = re.search(r"\b(just\s+now|yesterday|today)\b", lead_text)
    if word_match:
        phrase = word_match.group(1)
        if phrase == "just now":
            return fallback_now or datetime.now(timezone.utc)
        parsed = parse_datetime_to_utc(phrase)
        if parsed:
            return parsed

    # Strong freshness indicator tokens: treat as "now" — the caller's 24h freshness
    # gate re-validates, so an over-eager guess can only err toward strict rejection.
    if any(tok in lead_text for tok in ("breaking news", "just announced", "today announced")):
        return fallback_now or datetime.now(timezone.utc)

    return None


"""
Unit tests for 24-hour signal freshness validation and date normalization.
Uses freezegun for deterministic temporal verification per CODING_STANDARDS.md.
"""

from datetime import datetime, timezone

from bs4 import BeautifulSoup
from freezegun import freeze_time

from src.utils.date_normalizer import (
    _date_from_json_ld,
    _date_from_meta_tags,
    _date_from_time_tags,
    _date_from_url,
    extract_date_from_html,
    is_fresh_24h,
    parse_datetime_to_utc,
)


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


@freeze_time("2026-09-04 12:00:00")
def test_extract_date_from_html_meta_and_json_ld():
    # Arrange
    from src.utils.date_normalizer import extract_date_from_html

    og_html = """
    <html><head>
    <meta property="og:article:published_time" content="2026-09-04T10:00:00Z" />
    </head><body><p>Article body</p></body></html>
    """
    json_ld_html = """
    <html><head>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "NewsArticle", "datePublished": "2026-09-04T08:30:00+00:00"}
    </script>
    </head><body><p>Article body</p></body></html>
    """
    time_tag_html = """
    <html><body><time datetime="2026-09-04T07:15:00Z">Sep 4, 2026</time></body></html>
    """

    # Act
    og_dt = extract_date_from_html(og_html)
    ld_dt = extract_date_from_html(json_ld_html)
    time_dt = extract_date_from_html(time_tag_html)
    url_dt = extract_date_from_html("<html></html>", page_url="https://example.com/2026/09/04/new-ai-model")

    # Assert
    assert og_dt is not None and og_dt.hour == 10
    assert ld_dt is not None and ld_dt.hour == 8 and ld_dt.minute == 30
    assert time_dt is not None and time_dt.hour == 7 and time_dt.minute == 15
    assert url_dt is not None and url_dt.day == 4


def _soup(html: str):
    return BeautifulSoup(html, "html.parser")


def test_date_from_url_matches_path_date_patterns():
    # Act & Assert: full and single-digit path dates resolve; non-matching or absent do not
    assert _date_from_url("https://example.com/2026/09/03/story") == parse_datetime_to_utc("2026-09-03")
    assert _date_from_url("https://example.com/2026/9/3/story") == parse_datetime_to_utc("2026-09-03")
    assert _date_from_url("https://example.com/no-date-here") is None
    assert _date_from_url("") is None


def test_date_from_meta_tags_og_article_and_dc_date():
    # Arrange
    og_html = '<meta property="article:published_time" content="2026-09-03T10:00:00Z"/>'
    dc_html = '<meta name="dc.date" content="2026-09-03"/>'

    # Act & Assert
    assert _date_from_meta_tags(_soup(og_html)) == parse_datetime_to_utc("2026-09-03T10:00:00Z")
    assert _date_from_meta_tags(_soup(dc_html)) == parse_datetime_to_utc("2026-09-03")
    assert _date_from_meta_tags(_soup("<html></html>")) is None


def test_date_from_json_ld_single_list_and_invalid_payloads():
    # Arrange
    single = '<script type="application/ld+json">{"datePublished": "2026-09-03"}</script>'
    listing = '<script type="application/ld+json">[{"name": "x"}, {"dateCreated": "2026-09-04"}]</script>'
    invalid = '<script type="application/ld+json">{broken json</script>'

    # Act & Assert: datePublished and list items resolve; malformed JSON is skipped
    assert _date_from_json_ld(_soup(single)) == parse_datetime_to_utc("2026-09-03")
    assert _date_from_json_ld(_soup(listing)) == parse_datetime_to_utc("2026-09-04")
    assert _date_from_json_ld(_soup(invalid)) is None


def test_date_from_time_tags_datetime_attr_and_text():
    # Arrange
    attr_html = '<time datetime="2026-09-03">Sept 3, 2026</time>'
    text_html = '<time>2026-09-03</time>'

    # Act & Assert
    assert _date_from_time_tags(_soup(attr_html)) == parse_datetime_to_utc("2026-09-03")
    assert _date_from_time_tags(_soup(text_html)) == parse_datetime_to_utc("2026-09-03")
    assert _date_from_time_tags(_soup("<div>no time here</div>")) is None


def test_extract_date_from_html_tier_fallthrough_order():
    # Arrange: meta and <time> disagree with each other; URL would disagree too
    html = '<meta property="article:published_time" content="2026-09-04"/><time datetime="2026-09-05">x</time>'

    # Act & Assert: URL tier wins; without a URL, meta beats <time>; empty HTML yields None
    assert extract_date_from_html(html, page_url="https://x.com/2026/09/03/story") == parse_datetime_to_utc("2026-09-03")
    assert extract_date_from_html(html) == parse_datetime_to_utc("2026-09-04")
    assert extract_date_from_html("") is None


@freeze_time("2026-09-04 12:00:00")
def test_infer_content_freshness():
    # Arrange
    from src.utils.date_normalizer import infer_content_freshness

    rel_content = "OpenAI today announced a new research model 3 hours ago."
    just_now_content = "Breaking news: company launched product just now."
    no_date_content = "Terms of service and privacy policy for our platform."

    # Act
    rel_dt = infer_content_freshness(rel_content)
    jn_dt = infer_content_freshness(just_now_content, fallback_now=datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc))
    none_dt = infer_content_freshness(no_date_content)

    # Assert
    assert rel_dt is not None and rel_dt.hour == 9
    assert jn_dt is not None and jn_dt.hour == 12
    assert none_dt is None


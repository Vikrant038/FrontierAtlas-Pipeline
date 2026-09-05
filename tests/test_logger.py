"""
Unit tests for the loguru redaction filter (secrets-control backstop).
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

from src.utils.logger import CREDENTIAL_INLINE_RE, SENSITIVE_KEY_RE, _mask_value, redact_record


def test_mask_value_redacts_sensitive_keys():
    # Arrange / Act / Assert
    assert _mask_value("api_key", "sk-abc123") == "[REDACTED]"
    assert _mask_value("Authorization", "Bearer xyz") == "[REDACTED]"
    assert _mask_value("GITHUB_TOKEN", "ghp_secret") == "[REDACTED]"
    assert _mask_value("password", "hunter2") == "[REDACTED]"


def test_mask_value_preserves_safe_keys():
    assert _mask_value("url", "https://example.com") == "https://example.com"
    assert _mask_value("count", 5) == 5
    assert _mask_value("host", None) is None


def test_mask_value_recurses_into_dicts_and_lists():
    # Arrange
    payload = {
        "query": "papers",
        "auth": {"token": "abc", "depth": 2},
        "keys": ["safe", {"secret": "nope"}],
    }

    # Act
    masked = _mask_value("payload", payload)

    # Assert
    assert masked["query"] == "papers"
    assert masked["auth"]["token"] == "[REDACTED]"
    assert masked["auth"]["depth"] == 2
    assert masked["keys"][0] == "safe"
    assert masked["keys"][1]["secret"] == "[REDACTED]"


def test_redact_record_masks_extra_context():
    # Arrange
    record = {"extra": {"api_key": "sk-123", "url": "https://x.com"}}

    # Act
    assert redact_record(record) is True

    # Assert
    assert record["extra"]["api_key"] == "[REDACTED]"
    assert record["extra"]["url"] == "https://x.com"


def test_redact_record_masks_inline_credentials_in_message():
    # Arrange
    record = {"message": "Calling with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI and api_key=supersecret99 done"}

    # Act
    redact_record(record)

    # Assert: both credential forms replaced, surrounding text intact
    msg = record["message"]
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI" not in msg
    assert "supersecret99" not in msg
    assert "[REDACTED_CREDENTIAL]" in msg
    assert "done" in msg


def test_redact_record_leaves_plain_messages_alone():
    record = {"message": "Papers progress: 500/1000 collected"}
    redact_record(record)
    assert record["message"] == "Papers progress: 500/1000 collected"


def test_redact_record_tolerates_missing_fields():
    assert redact_record({}) is True
    assert redact_record({"message": None}) is True


def test_patterns_catch_common_secret_shapes():
    assert SENSITIVE_KEY_RE.search("client_secret")
    assert CREDENTIAL_INLINE_RE.search("token: 'abcd1234efgh'")
    assert CREDENTIAL_INLINE_RE.search("Authorization=Bearer abcdef123456789012345")

"""
Global pytest configuration and shared fixtures for offline hermetic testing.
Enforces CODING_STANDARDS.md Pillar 7 (offline, zero-network).
"""

import pytest


@pytest.fixture(autouse=True)
def mock_dns(monkeypatch):
    """Bypass offline DNS validation during tests."""
    monkeypatch.setattr("src.crawlers.base.validate_url_safe", lambda url, *args, **kwargs: url)

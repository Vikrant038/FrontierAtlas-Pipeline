"""
Unit tests for RFC 7231 HTTP-date parsing in Retry-After handler (BUG-07).
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from freezegun import freeze_time

from src.crawlers.base import _handle_retry_after


@pytest.mark.asyncio
async def test_retry_after_integer_seconds():
    # Arrange
    headers = {"Retry-After": "5"}
    mock_sleep = AsyncMock()

    with patch("asyncio.sleep", mock_sleep):
        # Act
        await _handle_retry_after(headers, "https://example.com/api")

        # Assert
        mock_sleep.assert_awaited_once_with(5.0)


@pytest.mark.asyncio
async def test_retry_after_integer_capped_at_30s():
    # Arrange
    headers = {"Retry-After": "120"}
    mock_sleep = AsyncMock()

    with patch("asyncio.sleep", mock_sleep):
        # Act
        await _handle_retry_after(headers, "https://example.com/api")

        # Assert
        mock_sleep.assert_awaited_once_with(30.0)


@freeze_time("2026-09-04 12:00:00")
@pytest.mark.asyncio
async def test_retry_after_rfc7231_http_date():
    # Arrange: Frozen at 12:00:00 UTC; header specifies 12:00:15 GMT (15s in future)
    headers = {"Retry-After": "Fri, 04 Sep 2026 12:00:15 GMT"}
    mock_sleep = AsyncMock()

    with patch("asyncio.sleep", mock_sleep):
        # Act
        await _handle_retry_after(headers, "https://example.com/api")

        # Assert
        mock_sleep.assert_awaited_once()
        slept = mock_sleep.call_args[0][0]
        assert 14.5 <= slept <= 15.5


@freeze_time("2026-09-04 12:00:00")
@pytest.mark.asyncio
async def test_retry_after_rfc7231_past_date_clamped_to_zero():
    # Arrange: Date in the past -> clamped to 0.0s
    headers = {"Retry-After": "Fri, 04 Sep 2026 11:59:00 GMT"}
    mock_sleep = AsyncMock()

    with patch("asyncio.sleep", mock_sleep):
        # Act
        await _handle_retry_after(headers, "https://example.com/api")

        # Assert
        mock_sleep.assert_awaited_once_with(0.0)


@pytest.mark.asyncio
async def test_retry_after_invalid_header_does_not_crash():
    # Arrange
    headers = {"Retry-After": "invalid-garbage-date-format"}
    mock_sleep = AsyncMock()

    with patch("asyncio.sleep", mock_sleep):
        # Act
        await _handle_retry_after(headers, "https://example.com/api")

        # Assert: No sleep should be called on invalid data
        mock_sleep.assert_not_called()

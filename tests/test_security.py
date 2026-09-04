"""
Unit tests for SSRF prevention and URL security validation.
Follows AAA pattern (Arrange, Act, Assert) per CODING_STANDARDS.md Pillar 7.
"""

import pytest
from src.utils.security import SSRFValidationError, validate_url_safe


def test_validate_url_safe_allows_public_https_url():
    # Arrange
    target_url = "https://export.arxiv.org/api/query"

    # Act
    validated_url = validate_url_safe(target_url, resolve_dns=False)

    # Assert
    assert validated_url == target_url


def test_validate_url_safe_blocks_loopback_ipv4():
    # Arrange
    forbidden_url = "http://127.0.0.1:8000/admin"

    # Act & Assert
    with pytest.raises(SSRFValidationError, match="Access to private/internal IP"):
        validate_url_safe(forbidden_url, resolve_dns=False)


def test_validate_url_safe_blocks_localhost_hostname():
    # Arrange
    forbidden_url = "http://localhost:3000/api"

    # Act & Assert
    with pytest.raises(SSRFValidationError, match="Access to loopback hostname"):
        validate_url_safe(forbidden_url, resolve_dns=False)


def test_validate_url_safe_blocks_cloud_metadata_ip():
    # Arrange (AWS/GCP/Azure link-local cloud metadata service)
    forbidden_url = "http://169.254.169.254/latest/meta-data/"

    # Act & Assert
    with pytest.raises(SSRFValidationError, match="Access to private/internal IP"):
        validate_url_safe(forbidden_url, resolve_dns=False)


def test_validate_url_safe_blocks_rfc1918_private_ip():
    # Arrange
    forbidden_url = "http://192.168.1.1/router"

    # Act & Assert
    with pytest.raises(SSRFValidationError, match="Access to private/internal IP"):
        validate_url_safe(forbidden_url, resolve_dns=False)


def test_validate_url_safe_blocks_invalid_scheme():
    # Arrange
    forbidden_url = "ftp://ftp.example.com/file.txt"

    # Act & Assert
    with pytest.raises(SSRFValidationError, match="Invalid URL scheme 'ftp'"):
        validate_url_safe(forbidden_url, resolve_dns=False)

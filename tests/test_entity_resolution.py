"""
Unit tests for 3-Tier Entity Resolution Engine.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

from src.resolution.normalizer import entity_resolver, normalize_string_tier1
from src.schemas.entities import MatchMethodEnum


def test_tier1_normalization_strips_suffixes_and_noise():
    # Arrange
    raw_input = "  OpenAI, Inc. Technologies  "

    # Act
    normalized = normalize_string_tier1(raw_input)

    # Assert
    assert normalized == "openai"


def test_exact_alias_resolution():
    # Arrange
    raw_name = "mistralai"
    source_url = "https://example.com/company"

    # Act
    canonical, log = entity_resolver.resolve(raw_name, source_url, "STARTUP")

    # Assert
    assert canonical == "Mistral AI"
    assert log.matchMethod == MatchMethodEnum.ALIAS_MATCH
    assert log.confidenceScore == 1.00
    assert log.canonicalName == "Mistral AI"


def test_fuzzy_token_sort_resolution():
    # Arrange
    raw_name = "Open AI"
    source_url = "https://example.com/source"

    # Act
    canonical, log = entity_resolver.resolve(raw_name, source_url, "STARTUP")

    # Assert
    assert canonical == "OpenAI"
    assert log.matchMethod in (MatchMethodEnum.FUZZY_TOKEN_SORT, MatchMethodEnum.ALIAS_MATCH, MatchMethodEnum.NORMALIZATION_EXACT)
    assert log.confidenceScore >= 0.90


def test_seed_list_entity_count():
    # Assert seed list has at least 50 entities as required by Phase IV specs
    from src.resolution.seed_data import CANONICAL_AI_ENTITIES
    assert len(CANONICAL_AI_ENTITIES) >= 50

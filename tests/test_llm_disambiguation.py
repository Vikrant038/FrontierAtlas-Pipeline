"""
Unit tests for Tier 3 LLM Entity Disambiguation in EntityResolver.
Strictly follows AAA pattern with offline LLM mocking per CODING_STANDARDS.md.
"""

import pytest
from unittest.mock import AsyncMock

from src.resolution.normalizer import EntityResolver
from src.schemas.entities import MatchMethodEnum
from src.llm.prompts import EntityDisambiguationSchema


@pytest.fixture
def clean_resolver(tmp_path):
    """Fixture providing an isolated resolver instance with fresh cache."""
    cache_file = str(tmp_path / "test_canonical_registry.json")
    return EntityResolver(cache_path=cache_file, enable_llm_disambiguation=True)


@pytest.mark.asyncio
async def test_70_to_90_band_triggers_llm_disambiguation(clean_resolver, mocker):
    # Arrange - "MistralHQ" scores ~73.7 with "Mistral AI", landing in the 70-90 band
    mock_extract = mocker.patch(
        "src.resolution.normalizer.llm_engine.extract_structured",
        new_callable=AsyncMock,
        return_value=EntityDisambiguationSchema(canonical="Mistral AI", confidence=0.92),
    )

    # Act
    canonical, log = await clean_resolver.resolve_async("MistralHQ", source_url="https://mistral.ai")

    # Assert
    mock_extract.assert_awaited_once()
    assert canonical == "Mistral AI"
    assert log.matchMethod == MatchMethodEnum.LLM_DISAMBIGUATION
    assert log.confidenceScore == 0.92
    assert log.canonicalName == "Mistral AI"


@pytest.mark.asyncio
async def test_llm_none_response_falls_back_to_new_entity(clean_resolver, mocker):
    # Arrange - LLM determines the entity is distinct or ambiguous (returns None)
    mock_extract = mocker.patch(
        "src.resolution.normalizer.llm_engine.extract_structured",
        new_callable=AsyncMock,
        return_value=EntityDisambiguationSchema(canonical=None, confidence=0.0),
    )

    # Act
    canonical, log = await clean_resolver.resolve_async("RunwayML", source_url="https://example.com")

    # Assert
    mock_extract.assert_awaited_once()
    assert canonical == "RunwayML"
    assert log.matchMethod == MatchMethodEnum.NEW_ENTITY
    assert log.confidenceScore == 0.50
    assert "RunwayML" in clean_resolver.canonical_entities


@pytest.mark.asyncio
async def test_llm_crash_falls_back_to_new_entity(clean_resolver, mocker):
    # Arrange - LLM call raises unexpected exception (e.g. network failure / context overflow)
    mock_extract = mocker.patch(
        "src.resolution.normalizer.llm_engine.extract_structured",
        new_callable=AsyncMock,
        side_effect=RuntimeError("API Connection Refused"),
    )

    # Act
    canonical, log = await clean_resolver.resolve_async("ChromaDB", source_url="https://example.com")

    # Assert
    mock_extract.assert_awaited_once()
    assert canonical == "ChromaDB"
    assert log.matchMethod == MatchMethodEnum.NEW_ENTITY
    assert log.confidenceScore == 0.50
    assert "ChromaDB" in clean_resolver.canonical_entities


def test_exact_and_alias_matches_never_call_llm(clean_resolver, mocker):
    # Arrange
    mock_extract = mocker.patch(
        "src.resolution.normalizer.llm_engine.extract_structured",
        new_callable=AsyncMock,
    )

    # Act - Tier 1A known alias
    can1, log1 = clean_resolver.resolve("mistralai", source_url="https://example.com")
    # Tier 1B NFKD exact match
    can2, log2 = clean_resolver.resolve("Anthropic, Inc.", source_url="https://example.com")
    # Tier 2 high-confidence fuzzy token sort (>= 90)
    can3, log3 = clean_resolver.resolve("Open AI", source_url="https://example.com")

    # Assert
    mock_extract.assert_not_called()
    assert log1.matchMethod == MatchMethodEnum.ALIAS_MATCH
    assert log2.matchMethod == MatchMethodEnum.NORMALIZATION_EXACT
    assert log3.matchMethod in (MatchMethodEnum.FUZZY_TOKEN_SORT, MatchMethodEnum.ALIAS_MATCH, MatchMethodEnum.NORMALIZATION_EXACT)



def test_score_under_70_never_calls_llm(clean_resolver, mocker):
    # Arrange
    mock_extract = mocker.patch(
        "src.resolution.normalizer.llm_engine.extract_structured",
        new_callable=AsyncMock,
    )

    # Act - completely unrelated novel startup
    can, log = clean_resolver.resolve("Zebra Quantum Analytics", source_url="https://zebra.xyz")

    # Assert
    mock_extract.assert_not_called()
    assert can == "Zebra Quantum Analytics"
    assert log.matchMethod == MatchMethodEnum.NEW_ENTITY
    assert log.confidenceScore == 0.50


@pytest.mark.asyncio
async def test_disambiguation_memoization_cache_prevents_duplicate_calls(clean_resolver, mocker):
    # Arrange
    mock_extract = mocker.patch(
        "src.resolution.normalizer.llm_engine.extract_structured",
        new_callable=AsyncMock,
        return_value=EntityDisambiguationSchema(canonical="Midjourney", confidence=0.88),
    )

    # Act - First call invokes LLM
    can1, log1 = await clean_resolver.resolve_async("Midjourney Art", source_url="https://example.com")
    # Second call for identical raw string hits memoized cache
    can2, log2 = await clean_resolver.resolve_async("Midjourney Art", source_url="https://example.com")

    # Assert
    assert mock_extract.await_count == 1
    assert can1 == can2 == "Midjourney"
    assert log1.matchMethod == log2.matchMethod == MatchMethodEnum.LLM_DISAMBIGUATION


def test_sync_resolve_triggers_llm_seamlessly(clean_resolver, mocker):
    # Arrange - test sync resolve() caller when in 70-90 band
    mock_extract = mocker.patch(
        "src.resolution.normalizer.llm_engine.extract_structured",
        new_callable=AsyncMock,
        return_value=EntityDisambiguationSchema(canonical="ElevenLabs", confidence=0.90),
    )

    # Act
    canonical, log = clean_resolver.resolve("Eleven Labs Voice", source_url="https://elevenlabs.io")

    # Assert
    mock_extract.assert_awaited_once()
    assert canonical == "ElevenLabs"
    assert log.matchMethod == MatchMethodEnum.LLM_DISAMBIGUATION
    assert log.confidenceScore == 0.90

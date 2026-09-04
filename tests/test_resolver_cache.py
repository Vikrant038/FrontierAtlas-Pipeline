"""
Unit tests for entity resolver disk-cache load/save round trip.
Follows AAA pattern with tmp_path isolation per CODING_STANDARDS.md Pillar 7.7.
"""

import json

from src.resolution.normalizer import EntityResolver
from src.resolution.seed_data import CANONICAL_AI_ENTITIES


def test_save_and_reload_cache_round_trip(tmp_path):
    # Arrange - single-token name: domain stem must appear in normalized name for grounding
    cache_path = str(tmp_path / "registry" / "canonical_registry.json")
    resolver = EntityResolver(cache_path=cache_path)
    canonical, _ = resolver.resolve("Weirdnewstartup", source_url="https://weirdnewstartup.com")
    assert canonical == "Weirdnewstartup"

    # Act - save, then load into a fresh resolver instance
    resolver.save_cache()
    reloaded_resolver = EntityResolver(cache_path=cache_path)

    # Assert - learned entity and domain grounding survive restart
    assert "Weirdnewstartup" in reloaded_resolver.canonical_entities
    assert reloaded_resolver.domain_map.get("weirdnewstartup.com") == "Weirdnewstartup"


def test_load_cache_corrupt_file_falls_back_to_seeds(tmp_path, caplog):
    # Arrange
    cache_path = tmp_path / "corrupt_registry.json"
    cache_path.write_text("{not valid json!!", encoding="utf-8")

    # Act - must not raise
    resolver = EntityResolver(cache_path=str(cache_path))

    # Assert - seed entities intact
    assert resolver.canonical_entities == set(CANONICAL_AI_ENTITIES)
    assert resolver.domain_map == {}


def test_save_cache_missing_parent_dir_created(tmp_path):
    # Arrange
    cache_path = str(tmp_path / "deep" / "nested" / "dir" / "registry.json")
    resolver = EntityResolver(cache_path=cache_path)

    # Act
    resolver.save_cache()

    # Assert
    import os

    assert os.path.exists(cache_path)
    with open(cache_path, "r", encoding="utf-8") as cache_file:
        saved_registry = json.load(cache_file)
    assert "entities" in saved_registry
    assert "domains" in saved_registry

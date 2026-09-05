"""
Global pytest configuration and shared fixtures for offline hermetic testing.
Enforces CODING_STANDARDS.md Pillar 7 (offline, zero-network).
"""

import pytest


@pytest.fixture(autouse=True)
def mock_dns(monkeypatch):
    """Bypass offline DNS validation during tests."""
    monkeypatch.setattr("src.crawlers.base.validate_url_safe", lambda url, *args, **kwargs: url)


@pytest.fixture(autouse=True)
def mock_llm_offline(monkeypatch, request):
    """Bypass live LLM calls during unit tests by default, using fast deterministic extraction."""
    if "no_auto_mock_llm" in request.keywords:
        return
    from src.llm.fallback_chain import llm_engine
    from src.llm.chunker import chunk_to_budget

    async def _offline_extract(raw_text, schema_cls, instruction=""):
        budgeted = chunk_to_budget(raw_text)
        data = llm_engine._deterministic_extract(budgeted, schema_cls)
        return schema_cls.model_validate(data)

    monkeypatch.setattr(llm_engine, "extract_structured", _offline_extract)


@pytest.fixture(autouse=True)
def isolate_run_state(tmp_path, monkeypatch):
    """Redirect cross-run novelty state to a per-test temp file.

    Prevents test fixtures (e.g. 'Feed 2', 'Stale Feed') from polluting the
    production exports/run_state.json and surfacing phantom stale-source warnings.
    """
    monkeypatch.setattr("src.config.settings.run_state_path", str(tmp_path / "run_state.json"))

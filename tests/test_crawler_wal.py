"""
Unit tests for TargetedCrawler Append-Only Write-Ahead Log (WAL) and recovery.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

import json
import pytest

from src.crawlers.base import TargetedCrawler
from src.crawlers.papers_crawler import ResearchPapersCrawler
from src.schemas.entities import (
    ResearchPaperRecord,
    SourceMetadata,
    StartupContent,
    StartupContentData,
    StartupRecord,
)


def test_crawler_wal_append_on_add(tmp_path):
    # Arrange
    wal_file = tmp_path / "test_wal.jsonl"
    crawler = TargetedCrawler(target_count=5, wal_path=str(wal_file))

    # Act
    crawler.add("key1", {"name": "item1"})
    crawler.add("key2", {"name": "item2"})
    crawler.add("KEY1", {"name": "item1_dup"})  # Duplicate, should be ignored
    crawler.close_wal()

    # Assert
    assert wal_file.exists()
    lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])
    assert rec1["key"] == "key1"
    assert rec1["data"] == {"name": "item1"}
    assert rec2["key"] == "key2"
    assert rec2["data"] == {"name": "item2"}


def test_crawler_wal_recovery(tmp_path):
    # Arrange: write initial WAL entries
    wal_file = tmp_path / "test_recovery_wal.jsonl"
    entries = [
        {"key": "alpha", "data": {"val": 10}},
        {"key": "beta", "data": {"val": 20}},
    ]
    wal_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    # Act: Recover in a fresh crawler
    crawler = TargetedCrawler(target_count=5, wal_path=str(wal_file))
    recovered_count = crawler.recover_from_wal()

    # Assert
    assert recovered_count == 2
    assert len(crawler.collected) == 2
    assert "alpha" in crawler.seen_keys
    assert "beta" in crawler.seen_keys
    assert "gamma" not in crawler.seen_keys

    # Act: Add further items
    crawler.add("gamma", {"val": 30})
    crawler.close_wal()

    # Assert: Third item appended to WAL
    lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    rec3 = json.loads(lines[2])
    assert rec3["key"] == "gamma"
    assert rec3["data"] == {"val": 30}


def test_crawler_wal_recovery_with_pydantic_model(tmp_path):
    # Arrange: write Pydantic items via crawler
    wal_file = tmp_path / "test_pydantic_wal.jsonl"
    crawler1 = TargetedCrawler(target_count=5, wal_path=str(wal_file))
    record1 = StartupRecord(
        source=SourceMetadata(name="Seed", url="https://example.com/seed1"),
        content=StartupContent(entityName="SeedAI", data=StartupContentData(employeeCount=50)),
    )
    crawler1.add("SeedAI", record1)
    crawler1.close_wal()

    # Act: recover with model_cls
    crawler2 = TargetedCrawler(target_count=5, wal_path=str(wal_file))
    recovered = crawler2.recover_from_wal(model_cls=StartupRecord)

    # Assert
    assert recovered == 1
    assert len(crawler2.collected) == 1
    rec = crawler2.collected[0]
    assert isinstance(rec, StartupRecord)
    assert rec.content.entityName == "SeedAI"
    assert rec.content.data.employeeCount == 50


def test_crawler_wal_skips_corrupt_entries_gracefully(tmp_path):
    # Arrange: write corrupt JSON line between valid lines
    wal_file = tmp_path / "test_corrupt_wal.jsonl"
    content = (
        json.dumps({"key": "valid1", "data": {"status": "ok1"}}) + "\n"
        + "CORRUPT_NOT_JSON{{{\n"
        + json.dumps({"key": "valid2", "data": {"status": "ok2"}}) + "\n"
    )
    wal_file.write_text(content, encoding="utf-8")

    # Act
    crawler = TargetedCrawler(target_count=5, wal_path=str(wal_file))
    recovered = crawler.recover_from_wal()

    # Assert: Should recover both valid entries and skip corrupt line
    assert recovered == 2
    assert len(crawler.collected) == 2
    assert crawler.collected[0]["status"] == "ok1"
    assert crawler.collected[1]["status"] == "ok2"


@pytest.mark.asyncio
async def test_papers_crawler_wal_parity(tmp_path):
    # Arrange
    wal_file = tmp_path / "papers_wal.jsonl"
    crawler = ResearchPapersCrawler(target_count=2, wal_path=str(wal_file))

    # Mock paper batch returned by fetch_papers_batch
    raw_paper = {
        "title": "Deep Learning Survey",
        "authors": ["Author One"],
        "paper_url": "https://arxiv.org/abs/2609.00001",
        "published_date": "2026-09-03T12:00:00Z",
        "abstract_repo": None,
    }

    # Act
    rec = await crawler.enrich_paper(raw_paper)
    crawler.add(rec.content.paper_url, rec, already_seen=True)
    crawler.close_wal()

    # Assert: WAL must contain the serialized ResearchPaperRecord
    assert wal_file.exists()
    lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["key"] == "https://arxiv.org/abs/2609.00001"
    assert entry["data"]["content"]["title"] == "Deep Learning Survey"

    # Recovery check
    crawler2 = ResearchPapersCrawler(target_count=2, wal_path=str(wal_file))
    recovered = crawler2.recover_from_wal(model_cls=ResearchPaperRecord)
    assert recovered == 1
    assert len(crawler2.collected) == 1
    assert crawler2.collected[0].content.title == "Deep Learning Survey"

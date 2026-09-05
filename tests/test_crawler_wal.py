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


def test_crawler_wal_compaction_rewrites_recovered_remainder(tmp_path):
    # Arrange: WAL with a valid entry, a corrupt line, and a duplicate key
    wal_file = tmp_path / "test_compact_wal.jsonl"
    wal_file.write_text(
        json.dumps({"key": "alpha", "data": {"n": 1}}) + "\n"
        + "CORRUPT{{{\n"
        + json.dumps({"key": "alpha", "data": {"n": 99}}) + "\n"  # duplicate key, skipped
        + json.dumps({"key": "beta", "data": {"n": 2}}) + "\n",
        encoding="utf-8",
    )

    # Act
    crawler = TargetedCrawler(target_count=5, wal_path=str(wal_file))
    recovered = crawler.recover_from_wal()

    # Assert: recovered only unique valid entries; WAL compacted to the remainder
    assert recovered == 2
    lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["key"] == "alpha"
    assert json.loads(lines[1])["key"] == "beta"

    # New adds append after the compacted content
    crawler.add("gamma", {"n": 3})
    crawler.close_wal()
    lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[2])["key"] == "gamma"


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


@pytest.mark.asyncio
async def test_crawler_wal_interrupted_run_full_cycle(tmp_path):
    """
    End-to-end interrupted-run cycle: a crawler crashes mid-run, a fresh run
    recovers the partial records from the WAL, completes the target, and
    truncates the WAL so the next run starts fresh.

    Mirrors the production wiring (recover at crawl start -> add in the loop ->
    reset_wal_if_complete at the end) with a simulated hard crash in between.
    """
    # Arrange: a crawler that dies partway through collection to simulate a crash
    wal_file = tmp_path / "interrupted_run_wal.jsonl"

    class CrashyCrawler(TargetedCrawler):
        """Collects sequential records but raises mid-run once a crash point is hit."""

        def __init__(self, *args, crash_after: int = 0, **kwargs):
            super().__init__(*args, **kwargs)
            self._crash_after = crash_after
            self.recovered_count = 0

        async def crawl(self):
            # Production wiring: resume from the WAL before fetching anything new.
            self.recovered_count = self.recover_from_wal()
            for i in range(10):
                self.add(f"item-{i}", {"seq": i})
                if self.is_full:
                    break
                if self._crash_after and len(self.collected) >= self._crash_after:
                    raise RuntimeError("simulated crash: process killed mid-run")
            self.reset_wal_if_complete()
            return self.collected[: self.target_count]

    # Act (run 1): the run dies at 3/5 collected, before reset_wal_if_complete
    crashed = CrashyCrawler(target_count=5, wal_path=str(wal_file), crash_after=3)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await crashed.crawl()
    crashed.close_wal()  # test cleanup only: abandon the "killed" process's handle

    # Assert (run 1): the 3 records written before the crash survived on disk
    lines = wal_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["data"]["seq"] for line in lines] == [0, 1, 2]

    # Act (run 2): a fresh crawler on the same WAL resumes and finishes the run
    resumed = CrashyCrawler(target_count=5, wal_path=str(wal_file))
    collected = await resumed.crawl()

    # Assert (run 2): partial records recovered, target completed, no duplicates
    assert resumed.recovered_count == 3
    assert len(collected) == 5
    assert [rec["seq"] for rec in collected] == [0, 1, 2, 3, 4]
    assert resumed.is_full
    assert "item-0" in resumed.seen_keys
    assert "item-4" in resumed.seen_keys

    # Assert: a completed run truncates the WAL so the next run starts fresh
    assert wal_file.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_crawler_wal_reset_wal_truncates_and_starts_fresh(tmp_path):
    """(a) With reset_wal=True, a pre-populated WAL file is truncated and crawler collects from scratch."""
    # Arrange
    wal_file = tmp_path / "fresh_run_wal.jsonl"
    entries = [
        {"key": "item-old-0", "data": {"seq": 0}},
        {"key": "item-old-1", "data": {"seq": 1}},
        {"key": "item-old-2", "data": {"seq": 2}},
    ]
    wal_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    assert wal_file.stat().st_size > 0

    class CountingCrawler(TargetedCrawler):
        async def crawl(self):
            self.recovered_count = self.recover_from_wal()
            for i in range(5):
                self.add(f"item-new-{i}", {"seq": i})
            return self.collected[: self.target_count]

    # Act
    crawler = CountingCrawler(target_count=5, wal_path=str(wal_file), reset_wal=True)
    collected = await crawler.crawl()

    # Assert - WAL was truncated on init, recover_from_wal skipped, collected from scratch
    assert crawler.recovered_count == 0
    assert len(collected) == 5
    assert [rec["seq"] for rec in collected] == [0, 1, 2, 3, 4]
    assert "item-old-0" not in crawler.seen_keys
    assert "item-new-0" in crawler.seen_keys
    assert "item-new-4" in crawler.seen_keys


@pytest.mark.asyncio
async def test_crawler_wal_without_reset_wal_resumes_normally(tmp_path):
    """(b) Without reset_wal (default False), existing resume behavior is unchanged."""
    # Arrange
    wal_file = tmp_path / "resume_run_wal.jsonl"
    entries = [
        {"key": "item-old-0", "data": {"seq": 100}},
        {"key": "item-old-1", "data": {"seq": 101}},
    ]
    wal_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    class ResumingCrawler(TargetedCrawler):
        async def crawl(self):
            self.recovered_count = self.recover_from_wal()
            for i in range(5):
                self.add(f"item-new-{i}", {"seq": i})
                if self.is_full:
                    break
            return self.collected[: self.target_count]

    # Act - default reset_wal=False
    crawler = ResumingCrawler(target_count=5, wal_path=str(wal_file))
    collected = await crawler.crawl()

    # Assert - 2 items recovered, then 3 new items collected to reach target=5
    assert crawler.recovered_count == 2
    assert len(collected) == 5
    assert [rec["seq"] for rec in collected] == [100, 101, 0, 1, 2]
    assert "item-old-0" in crawler.seen_keys
    assert "item-new-0" in crawler.seen_keys


"""
Unit tests for TargetedCrawler base class.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

from src.crawlers.base import TargetedCrawler


def test_targeted_crawler_quota_and_deduplication():
    # Arrange
    crawler = TargetedCrawler(target_count=2)

    # Act & Assert - Key normalization & registration
    assert crawler.is_seen("OpenAI") is False
    assert crawler.is_seen("openai") is True
    assert crawler.is_seen("  OPENAI  ") is True
    assert crawler.is_seen("") is True
    assert crawler.is_seen(None) is True

    # Act & Assert - Initial quota state
    assert crawler.is_full is False
    assert crawler.remaining == 2
    assert len(crawler.collected) == 0

    # Act & Assert - Adding first unique item
    is_full = crawler.add("item1", {"name": "item1"})
    assert is_full is False
    assert crawler.remaining == 1
    assert len(crawler.collected) == 1

    # Act & Assert - Adding duplicate item (rejected, no state change)
    is_full = crawler.add("ITEM1", {"name": "item1_dup"})
    assert is_full is False
    assert crawler.remaining == 1
    assert len(crawler.collected) == 1

    # Act & Assert - Adding second unique item (reaches quota)
    is_full = crawler.add("item2", {"name": "item2"})
    assert is_full is True
    assert crawler.is_full is True
    assert crawler.remaining == 0
    assert len(crawler.collected) == 2

    # Act & Assert - Adding third item beyond quota (rejected)
    is_full = crawler.add("item3", {"name": "item3"})
    assert is_full is True
    assert len(crawler.collected) == 2


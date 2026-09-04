"""
Recorded-fixture parser tests for Himalayas, WeWorkRemotely, Arbeitnow, and News RSS feeds.
Pins per-board/feed parsing logic against real recorded response shapes.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7 with offline respx mocking and freezegun.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
import pytest
import respx
import httpx
from freezegun import freeze_time

from src.crawlers.jobs_crawler import JobsCrawler
from src.crawlers.news_crawler import NewsCrawler
from src.schemas.entities import RoleFamilyEnum

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_himalayas_parser_with_recorded_fixture():
    # Arrange - real recorded himalayas.json fixture
    fixture_path = FIXTURES_DIR / "himalayas.json"
    assert fixture_path.exists(), f"Missing fixture {fixture_path}"
    with open(fixture_path, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    respx.get("https://himalayas.app/jobs/api?q=ai").mock(
        return_value=httpx.Response(200, json=fixture_data)
    )
    crawler = JobsCrawler()

    # Act
    records = await crawler.fetch_himalayas()
    await crawler.close()

    # Assert - non-AI jobs filtered out, AI jobs parsed and normalized
    assert len(records) == 2

    job1 = records[0]
    assert job1.content.company == "Intetics"
    assert job1.content.title == "Senior AI Systems Engineer"
    assert job1.content.role_family == RoleFamilyEnum.ENGINEERING
    assert job1.content.is_remote is True
    assert "https://himalayas.app/companies/intetics/jobs/senior-ai-systems-engineer" in job1.source.url
    assert job1.source.name == "Himalayas AI"
    assert job1.content.date == datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)

    job2 = records[1]
    assert job2.content.company == "Mercor"
    assert job2.content.title == "Staff Machine Learning Scientist"
    assert job2.content.role_family == RoleFamilyEnum.RESEARCH
    assert job2.content.is_remote is True


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_himalayas_fallback_to_rss_when_api_empty():
    # Arrange - API returns 0 jobs; must fall back to RSS feed
    respx.get("https://himalayas.app/jobs/api?q=ai").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Himalayas AI Jobs</title>
        <item>
          <title>Anthropic: AI Alignment Scientist</title>
          <link>https://himalayas.app/companies/anthropic/jobs/alignment-scientist</link>
          <pubDate>Fri, 04 Sep 2026 10:00:00 GMT</pubDate>
          <author>Anthropic</author>
        </item>
      </channel>
    </rss>"""
    respx.get("https://himalayas.app/jobs/rss").mock(
        return_value=httpx.Response(200, text=rss_xml)
    )
    crawler = JobsCrawler()

    # Act
    records = await crawler.fetch_himalayas()
    await crawler.close()

    # Assert
    assert len(records) == 1
    assert records[0].content.company == "Anthropic"
    assert records[0].content.title == "Anthropic: AI Alignment Scientist"
    assert records[0].content.role_family == RoleFamilyEnum.RESEARCH


@freeze_time("2026-08-17T14:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_weworkremotely_parser_with_recorded_fixture():
    # Arrange - real recorded weworkremotely.rss fixture
    fixture_path = FIXTURES_DIR / "weworkremotely.rss"
    assert fixture_path.exists(), f"Missing fixture {fixture_path}"
    with open(fixture_path, "r", encoding="utf-8") as f:
        rss_content = f.read()

    respx.get("https://weworkremotely.com/categories/remote-programming-jobs.rss").mock(
        return_value=httpx.Response(200, text=rss_content)
    )
    respx.get("https://weworkremotely.com/remote-jobs.rss").mock(
        return_value=httpx.Response(200, text="<rss></rss>")
    )
    respx.get("https://weworkremotely.com/categories/remote-customer-support-jobs.rss").mock(
        return_value=httpx.Response(200, text="<rss></rss>")
    )
    crawler = JobsCrawler()

    # Act
    records = await crawler.fetch_weworkremotely()
    await crawler.close()

    # Assert - exactly 1 fresh (<24h from 2026-08-17T14:00:00Z)
    assert len(records) == 1
    job = records[0]
    # Colon splitting: "Collaboration.Ai: Senior Software AI Engineer" -> ("Collaboration.Ai", "Senior Software AI Engineer")
    assert job.content.company == "Collaboration.Ai"
    assert job.content.title == "Senior Software AI Engineer"
    assert job.content.role_family == RoleFamilyEnum.ENGINEERING
    assert job.content.is_remote is True
    assert "collaboration-ai-senior-software-ai-engineer" in job.source.url
    assert job.content.date == datetime(2026, 8, 17, 11, 57, 38, tzinfo=timezone.utc)


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_arbeitnow_parser_with_recorded_fixture():
    # Arrange - real recorded arbeitnow.json fixture
    fixture_path = FIXTURES_DIR / "arbeitnow.json"
    assert fixture_path.exists(), f"Missing fixture {fixture_path}"
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    respx.get("https://www.arbeitnow.com/api/job-board-api").mock(
        return_value=httpx.Response(200, json=data)
    )
    crawler = JobsCrawler()

    # Act
    records = await crawler.fetch_arbeitnow()
    await crawler.close()

    # Assert - exactly 2 jobs pass the AI title and 24h freshness filters
    assert len(records) == 2
    titles = [r.content.title for r in records]
    assert any("Machine Learning Engineer" in t for t in titles)
    assert any("AI Product Engineer" in t for t in titles)

    # Check remote derivation
    remote_map = {r.content.title: r.content.is_remote for r in records}
    ml_title = [t for t in titles if "Machine Learning" in t][0]
    lyto_title = [t for t in titles if "AI Product Engineer" in t][0]
    assert remote_map[ml_title] is False
    assert remote_map[lyto_title] is True


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_news_parse_feed_with_recorded_rss_fixture():
    # Arrange - real RSS XML fixture from tests/fixtures/news_feed.xml
    fixture_path = FIXTURES_DIR / "news_feed.xml"
    assert fixture_path.exists(), f"Missing fixture {fixture_path}"
    with open(fixture_path, "r", encoding="utf-8") as f:
        news_xml = f.read()

    feed_url = "https://techcrunch.com/category/artificial-intelligence/feed/"
    respx.get(feed_url).mock(return_value=httpx.Response(200, text=news_xml))
    crawler = NewsCrawler()

    # Act
    entries = await crawler.fetch_feed(feed_url)
    fresh_records = []
    for entry in entries:
        rec = await crawler._process_entry(entry, "TechCrunch AI")
        if rec:
            fresh_records.append(rec)
    await crawler.close()

    # Assert - exactly 1 fresh record (<24h), HTML cleaned from summary
    assert len(fresh_records) == 1
    rec = fresh_records[0]
    assert rec.content.title == "Anthropic releases new multimodal intelligence features"
    assert "<p>" not in rec.content.summary
    assert "Anthropic announced a set of new AI capabilities" in rec.content.summary
    assert rec.content.published_date == datetime(2026, 9, 4, 10, 30, 0, tzinfo=timezone.utc)

"""
Unit tests for JobsCrawler freshness filtering, role classification, and multi-source ingestion.
Follows AAA pattern with offline respx mocking and freezegun per CODING_STANDARDS.md.
"""

import pytest
import respx
import httpx
from freezegun import freeze_time

from src.crawlers.jobs_crawler import JobsCrawler
from src.schemas.entities import RoleFamilyEnum


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_jobs_crawler_freshness_filtering():
    # Arrange
    crawler = JobsCrawler()
    now_iso = "2026-09-04T10:00:00Z"  # 2h ago (< 24h)
    stale_iso = "2026-09-01T10:00:00Z"  # 3 days ago (> 24h)

    remoteok_data = [
        {"legal": "Notice"},
        {
            "id": "101",
            "company": "Scale AI, Inc.",
            "position": "Staff AI Research Scientist",
            "date": now_iso,
            "url": "https://remoteok.com/jobs/101",
            "tags": ["ai", "remote"],
            "location": "Worldwide",
        },
        {
            "id": "102",
            "company": "Stale AI Corp",
            "position": "Junior Engineer",
            "date": stale_iso,
            "url": "https://remoteok.com/jobs/102",
        },
    ]
    respx.get("https://remoteok.com/api?tag=ai").mock(return_value=httpx.Response(200, json=remoteok_data))

    # Act
    jobs = await crawler.fetch_remoteok()
    await crawler.close()

    # Assert
    assert len(jobs) == 1
    assert jobs[0].content.company == "Scale AI"
    assert jobs[0].content.title == "Staff AI Research Scientist"
    assert jobs[0].content.role_family == RoleFamilyEnum.RESEARCH
    assert jobs[0].content.is_remote is True


def test_jobs_crawler_role_classification():
    # Arrange & Act & Assert
    assert JobsCrawler._classify_role("AI Research Scientist") == RoleFamilyEnum.RESEARCH
    assert JobsCrawler._classify_role("Senior Python Backend Engineer") == RoleFamilyEnum.ENGINEERING
    assert JobsCrawler._classify_role("AI Product Manager") == RoleFamilyEnum.PRODUCT
    assert JobsCrawler._classify_role("UI/UX Designer") == RoleFamilyEnum.DESIGN
    assert JobsCrawler._classify_role("Enterprise Account Executive") == RoleFamilyEnum.SALES
    assert JobsCrawler._classify_role("Growth Marketing Lead") == RoleFamilyEnum.MARKETING
    assert JobsCrawler._classify_role("Head of Operations") == RoleFamilyEnum.OPERATIONS
    assert JobsCrawler._classify_role("General Specialist") == RoleFamilyEnum.OTHER


def test_jobs_crawler_is_remote_derivation():
    # Arrange & Act & Assert
    # Case A: Location has "remote" or "worldwide" -> is_remote True
    assert JobsCrawler._is_remote("United States (Remote)") is True
    assert JobsCrawler._is_remote("Worldwide") is True
    assert JobsCrawler._is_remote("Anywhere", ["remote"]) is True

    # Case B: Location is empty -> defaults to True
    assert JobsCrawler._is_remote("") is True
    assert JobsCrawler._is_remote("   ") is True

    # Case C: Location is an onsite office without remote keyword/tag -> False
    assert JobsCrawler._is_remote("New York, NY, USA", ["python", "django"]) is False
    assert JobsCrawler._is_remote("London, UK") is False

    # Case D: Location has no remote, but title explicitly states Remote -> True
    assert JobsCrawler._is_remote(location="Germany", title="Staff AI Engineer - 2nd Horizon | Germany | Remote") is True
    assert JobsCrawler._is_remote(location="Berlin, Germany", title="AI Specialist (Remote)") is True


def test_jobs_crawler_keyword_word_boundary_filter():
    # Arrange & Act & Assert
    from src.crawlers.jobs_crawler import AI_KEYWORD_PATTERN

    # Must match genuine AI / ML / LLM / Data Science roles
    assert AI_KEYWORD_PATTERN.search("Staff AI Engineer - 2nd Horizon | Germany | Remote")
    assert AI_KEYWORD_PATTERN.search("Machine Learning Engineer")
    assert AI_KEYWORD_PATTERN.search("Senior Data Scientist")
    assert AI_KEYWORD_PATTERN.search("LLM Alignment Specialist")
    assert AI_KEYWORD_PATTERN.search("Deep Learning Researcher")
    assert AI_KEYWORD_PATTERN.search("Software Engineer, ML Ops and Platform")

    # Must reject false positive substrings containing 'ai' inside non-AI words
    assert not AI_KEYWORD_PATTERN.search("Werkstudent (m/w/d) in Sustainable Finance")
    assert not AI_KEYWORD_PATTERN.search("Claims Validation Handler")
    assert not AI_KEYWORD_PATTERN.search("Campaigns & Content Specialist")
    assert not AI_KEYWORD_PATTERN.search("Multi Skilled Maintenance Technician")
    assert not AI_KEYWORD_PATTERN.search("Vehicle Detailer")


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_jobs_crawler_arbeitnow_ingestion():
    # Arrange
    crawler = JobsCrawler()
    now_epoch = 1788523200  # 2026-09-04 12:00:00 UTC
    stale_epoch = 1788264000  # 3 days prior

    arbeitnow_data = {
        "data": [
            {
                "slug": "ai-eng-101",
                "company_name": "OpenAI, Inc.",
                "title": "Machine Learning Engineer",
                "created_at": now_epoch,
                "url": "https://arbeitnow.com/jobs/ai-eng-101",
                "remote": True,
                "tags": ["ml", "ai"],
                "location": "Berlin, Germany",
            },
            {
                "slug": "stale-102",
                "company_name": "Old Tech",
                "title": "Stale Engineer",
                "created_at": stale_epoch,
                "url": "https://arbeitnow.com/jobs/stale-102",
                "remote": False,
                "tags": ["ai"],
            },
            {
                "slug": "non-ai-103",
                "company_name": "Insurance Co",
                "title": "Claims Validation Handler",
                "created_at": now_epoch,
                "url": "https://arbeitnow.com/jobs/non-ai-103",
                "remote": False,
            },
            {
                "slug": "grafana-remote-104",
                "company_name": "Grafana Labs",
                "title": "Staff AI Engineer - 2nd Horizon | Germany | Remote",
                "created_at": now_epoch,
                "url": "https://arbeitnow.com/jobs/grafana-remote-104",
                "remote": False,
                "location": "Germany",
                "tags": ["R&D"],
            }
        ]
    }
    respx.get("https://www.arbeitnow.com/api/job-board-api").mock(
        return_value=httpx.Response(200, json=arbeitnow_data)
    )

    # Act
    jobs = await crawler.fetch_arbeitnow()
    await crawler.close()

    # Assert
    assert len(jobs) == 2
    assert jobs[0].content.company == "OpenAI"
    assert jobs[0].content.role_family == RoleFamilyEnum.ENGINEERING
    assert jobs[0].content.is_remote is True

    assert jobs[1].content.title == "Staff AI Engineer - 2nd Horizon | Germany | Remote"
    assert jobs[1].content.is_remote is True

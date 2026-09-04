"""
Unit tests for JobsCrawler freshness filtering, role classification, and multi-source ingestion.
Follows AAA pattern with offline respx mocking and freezegun per CODING_STANDARDS.md.
"""

from datetime import datetime, timezone

import pytest
import respx
import httpx
from freezegun import freeze_time

from src.config import settings
from src.crawlers.jobs_crawler import JobsCrawler
from src.llm.rules import classify_role_family
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


@pytest.mark.parametrize(
    "first_line, expected",
    [
        # 'Company | Title | Location': location kept, title untruncated
        ("Acme | Senior AI Engineer | San Francisco", ("Acme", "Senior AI Engineer", "San Francisco")),
        # 'Company is the ...': company before the phrase, title truncated to 100
        ("Acme is the place to build agents", ("Acme", "Acme is the place to build agents", "")),
        # Generic: company None (caller falls back to the entry author), title truncated
        ("A plain posting line without structure", (None, "A plain posting line without structure", "")),
        # '|' with a blank company stays "" (no author fallback in that branch)
        ("| Title | NYC", ("", "Title", "NYC")),
    ],
)
def test_jobs_crawler_parse_hn_first_line_formats(first_line, expected):
    # Act
    parsed = JobsCrawler._parse_hn_first_line(first_line)

    # Assert
    assert parsed == expected


def test_jobs_crawler_parse_hn_first_line_truncates_long_titles():
    # Arrange: a posting line far longer than the 100-char cap
    long_line = "We need a very experienced machine learning engineer " + ("who owns the full model lifecycle " * 20)

    # Act
    company, title, loc = JobsCrawler._parse_hn_first_line(long_line)

    # Assert: generic branch truncates the title and returns no company
    assert company is None
    assert loc == ""
    assert len(title) == 100


def test_jobs_crawler_role_classification():
    # Arrange & Act & Assert — shared rules.classify_role_family (JobsCrawler delegates to it)
    assert classify_role_family("AI Research Scientist") == RoleFamilyEnum.RESEARCH
    assert classify_role_family("Senior Python Backend Engineer") == RoleFamilyEnum.ENGINEERING
    assert classify_role_family("AI Product Manager") == RoleFamilyEnum.PRODUCT
    assert classify_role_family("UI/UX Designer") == RoleFamilyEnum.DESIGN
    assert classify_role_family("Enterprise Account Executive") == RoleFamilyEnum.SALES
    assert classify_role_family("Growth Marketing Lead") == RoleFamilyEnum.MARKETING
    assert classify_role_family("Head of Operations") == RoleFamilyEnum.OPERATIONS
    assert classify_role_family("General Specialist") == RoleFamilyEnum.OTHER


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


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_jobs_crawler_stale_source_date_rejected_not_novelty_stamped():
    # Arrange - RemoteOK defect regression: item with real but stale date must be
    # rejected outright, never rescued by the cross-run novelty stamp.
    crawler = JobsCrawler()
    stale_iso = "2026-08-11T12:28:07+00:00"  # weeks old, source-stated

    stale_data = [
        {
            "company": "OldCo",
            "position": "Machine Learning Engineer",
            "date": stale_iso,
            "url": "https://remoteok.com/jobs/stale-ml",
            "tags": ["ml"],
            "location": "",
            "id": 1,
        }
    ]
    respx.get("https://remoteok.com/api?tag=ai").mock(
        return_value=httpx.Response(200, json=[{"legal": "notice"}] + stale_data)
    )

    # Act
    jobs = await crawler.fetch_remoteok()
    await crawler.close()

    # Assert - stale source date wins over novelty: zero records
    assert jobs == []


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_jobs_crawler_dateless_new_posting_novelty_stamped_once():
    # Arrange - truly dateless posting: novelty stamp on first run, rejected on second.
    crawler = JobsCrawler()
    dateless_data = [
        {
            "company": "NewCo",
            "position": "LLM Research Engineer",
            "date": "",
            "url": "https://remoteok.com/jobs/dateless-llm",
            "tags": [],
            "location": "",
            "id": 2,
        }
    ]
    respx.get("https://remoteok.com/api?tag=ai").mock(
        return_value=httpx.Response(200, json=[{"legal": "notice"}] + dateless_data)
    )

    # Act - first run: dateless + unseen = collect with collection-time stamp
    jobs_first = await crawler.fetch_remoteok()

    # Act - second run: same dateless posting now in prev-run state = reject
    crawler._prev_run_urls = {"" .join(["https://remoteok.com/jobs/dateless-llm"])}
    jobs_second = await crawler.fetch_remoteok()
    await crawler.close()

    # Assert
    assert len(jobs_first) == 1
    assert jobs_first[0].content.date == datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    assert jobs_second == []


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_jobs_crawler_anti_bot_403_escalation():
    # Arrange - Verify that JobsCrawler inherits full anti-bot TLS escalation on HTTP 403
    import json
    crawler = JobsCrawler()
    respx.get("https://remoteok.com/api?tag=ai").mock(return_value=httpx.Response(403))

    payload = [
        {"legal": "Notice"},
        {
            "id": "201",
            "company": "Anthropic",
            "position": "AI Alignment Researcher",
            "date": "2026-09-04T11:00:00Z",
            "url": "https://remoteok.com/jobs/201",
            "tags": ["ai", "remote"],
            "location": "Remote",
        },
    ]
    escalation_called = False

    async def mock_fetch_tls(url, params=None, timeout=None):
        nonlocal escalation_called
        escalation_called = True
        assert "remoteok" in url
        return json.dumps(payload)

    crawler.fetch_tls = mock_fetch_tls  # type: ignore[method-assign]

    # Act
    jobs = await crawler.fetch_remoteok()
    await crawler.close()

    # Assert
    assert escalation_called is True
    assert len(jobs) == 1
    assert jobs[0].content.company == "Anthropic"
    assert jobs[0].content.title == "AI Alignment Researcher"


# ---------------------------------------------------------------------------
# Per-board fetch/parse coverage against authentic board payload shapes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_title, author, split_colon, expected",
    [
        # Colon split ('Company: Title') only when split_colon is set (WWR feeds).
        ("Acme: Staff AI Engineer", "", True, ("Acme", "Staff AI Engineer")),
        # No colon + no 'is hiring': company falls back to the feed author.
        ("Staff AI Engineer", "acme-hr", False, ("acme-hr", "Staff AI Engineer")),
        # 'is hiring' phrase: company taken from the prefix, title kept whole.
        ("Acme is hiring a Senior AI Engineer", "", False, ("Acme", "Acme is hiring a Senior AI Engineer")),
        # Author missing entirely: 'AI Startup' placeholder (dateless-job feed convention).
        ("Senior AI Engineer", "", False, ("AI Startup", "Senior AI Engineer")),
        # Colon present but split_colon disabled -> author fallback, title whole.
        ("Acme: Staff AI Engineer", "other-hr", False, ("other-hr", "Acme: Staff AI Engineer")),
    ],
)
def test_jobs_crawler_split_company_title_branches(raw_title, author, split_colon, expected):
    assert JobsCrawler._split_company_title(raw_title, author=author, split_colon=split_colon) == expected


def _future(value):
    """Wrap a plain value in a completed coroutine (feed/fetch seam stub)."""
    import asyncio

    async def _wrap():
        return value

    return _wrap()


def _job_feed_xml(items):
    """Render a minimal RSS 2.0 document from (title, link, pub_date, description) tuples."""
    item_xml = "\n".join(
        f"<item>"
        f"<title>{title}</title>"
        f"<link>{link}</link>"
        f"<pubDate>{pub_date}</pubDate>"
        f"<description>{desc}</description>"
        f"</item>"
        for title, link, pub_date, desc in items
    )
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<rss version=\"2.0\"><channel><title>Feed</title>"
        + item_xml
        + "</channel></rss>"
    )


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_jobs_crawler_weworkremotely_feed_parsing_and_dedup(monkeypatch):
    # Arrange - two configured category feeds; the second raises (board flake) and must be
    # skipped via the per-URL except/continue, while the first is parsed and deduplicated.
    crawler = JobsCrawler()
    monkeypatch.setattr(
        settings, "jobs_weworkremotely_urls",
        "https://weworkremotely.com/feed1.rss|https://weworkremotely.com/feed2.rss",
    )
    feed_xml = _job_feed_xml([
        # AI role, colon-separated company
        ("Samsara: Staff AI Engineer", "https://weworkremotely.com/jobs/1", "Fri, 04 Sep 2026 10:00:00 GMT", "&lt;p&gt;AI infra role.&lt;/p&gt;"),
        # Same URL again: seen-dedup must drop the second copy
        ("Samsara: Staff AI Engineer (Copy)", "https://weworkremotely.com/jobs/1", "Fri, 04 Sep 2026 10:30:00 GMT", "&lt;p&gt;Duplicate.&lt;/p&gt;"),
        # Non-AI title: filtered before the freshness gate
        ("Acme: Account Executive", "https://weworkremotely.com/jobs/2", "Fri, 04 Sep 2026 10:00:00 GMT", "&lt;p&gt;Sales.&lt;/p&gt;"),
    ])

    async def fake_fetch(url, *args, **kwargs):
        if "feed2" in url:
            raise RuntimeError("board down")
        return feed_xml

    crawler.fetch = fake_fetch  # type: ignore[method-assign]

    # Act
    jobs = await crawler.fetch_weworkremotely()
    await crawler.close()

    # Assert - one record from feed1 (dup dropped, non-AI filtered); feed2 error swallowed
    assert len(jobs) == 1
    assert jobs[0].content.company == "Samsara"
    assert jobs[0].content.title == "Staff AI Engineer"
    assert jobs[0].content.is_remote is True


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_jobs_crawler_weworkremotely_is_hiring_title_branch(monkeypatch):
    # Arrange - 'Company is hiring ...' title shape with no author: company from prefix.
    crawler = JobsCrawler()
    monkeypatch.setattr(
        settings, "jobs_weworkremotely_urls", "https://weworkremotely.com/feed1.rss",
    )
    feed_xml = _job_feed_xml([
        ("Anthropic is hiring an AI Research Engineer", "https://weworkremotely.com/jobs/3", "Fri, 04 Sep 2026 10:00:00 GMT", "&lt;p&gt;LLM research.&lt;/p&gt;"),
    ])
    crawler.fetch = lambda url, *a, **k: _future(feed_xml)  # type: ignore[method-assign]

    # Act
    jobs = await crawler.fetch_weworkremotely()
    await crawler.close()

    # Assert - 'is hiring' branch: company before the phrase, title kept whole
    assert len(jobs) == 1
    assert jobs[0].content.company == "Anthropic"
    assert "is hiring" in jobs[0].content.title


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_jobs_crawler_himalayas_api_parsing():
    # Arrange - real Himalayas /jobs/api shape (jobs[].companyName/title/pubDate/applicationLink/guid).
    crawler = JobsCrawler()
    payload = {
        "totalCount": 3,
        "jobs": [
            {
                "title": "Senior AI Engineer", "companyName": "Intetics", "pubDate": 1788523200,
                "applicationLink": "https://himalayas.app/jobs/1/apply", "guid": "g-1",
            },
            # companyName missing -> 'Himalayas Tech' fallback
            {"title": "LLM Trainer", "pubDate": 1788523200, "applicationLink": "https://himalayas.app/jobs/2/apply"},
            # Non-AI title filtered out
            {"title": "Accountant", "companyName": "Firm", "pubDate": 1788523200, "guid": "g-3"},
        ],
    }
    respx.get(settings.jobs_himalayas_api_url).mock(return_value=httpx.Response(200, json=payload))

    # Act
    jobs = await crawler.fetch_himalayas()
    await crawler.close()

    # Assert
    assert len(jobs) == 2
    assert jobs[0].content.company == "Intetics"
    assert jobs[0].content.title == "Senior AI Engineer"
    assert jobs[0].content.is_remote is True  # himalayas roles are remote by convention
    assert jobs[1].content.company == "Himalayas Tech"
    assert jobs[1].source.url == "https://himalayas.app/jobs/2/apply"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_jobs_crawler_himalayas_api_empty_falls_back_to_rss():
    # Arrange - API returns zero jobs; the crawler must fall back to the RSS feed helper.
    crawler = JobsCrawler()
    rss_xml = _job_feed_xml([
        ("Rivian: AI Platform Engineer", "https://himalayas.app/jobs/9", "Fri, 04 Sep 2026 10:00:00 GMT", "&lt;p&gt;AI.&lt;/p&gt;"),
    ])

    async def fake_fetch_json(url, **kwargs):
        return {"jobs": [], "totalCount": 0}

    crawler.fetch_json = fake_fetch_json  # type: ignore[method-assign]
    crawler.fetch = lambda url, *a, **k: _future(rss_xml)  # type: ignore[method-assign]

    # Act
    jobs = await crawler.fetch_himalayas()
    await crawler.close()

    # Assert - RSS fallback produced the record; generic RSS titles keep the whole
    # title and fall back to the 'AI Startup' company (no colon split on this board)
    assert len(jobs) == 1
    assert jobs[0].content.company == "AI Startup"
    assert jobs[0].content.title == "Rivian: AI Platform Engineer"
    assert jobs[0].source.name == "Himalayas AI"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
@respx.mock
async def test_jobs_crawler_remoteok_field_fallbacks():
    # Arrange - items missing company/tags/url fields: the map must apply its defaults,
    # and non-AI titles must be skipped rather than crash the map.
    crawler = JobsCrawler()
    payload = [
        {"legal": "Notice"},
        {"position": "AI Research Scientist", "date": "2026-09-04T10:00:00Z", "id": 7},
        {"company": "Acme", "position": "Barista", "date": "2026-09-04T10:00:00Z", "url": "https://remoteok.com/jobs/8"},
    ]
    respx.get("https://remoteok.com/api?tag=ai").mock(return_value=httpx.Response(200, json=payload))

    # Act
    jobs = await crawler.fetch_remoteok()
    await crawler.close()

    # Assert - company fallback 'Unknown', url fallback derived from id, non-AI skipped
    assert len(jobs) == 1
    assert jobs[0].content.company == "Unknown"
    assert jobs[0].source.url == "https://remoteok.com/jobs/7"


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_jobs_crawler_yc_hn_end_to_end():
    # Arrange - YC HN 'Who is hiring' RSS entries: LLM-classified long description
    # (external apply URL wins over ycombinator.com), short-desc no-LLM entry,
    # non-AI title, stale date, and a malformed author-less entry.
    crawler = JobsCrawler()
    long_desc = (
        "&lt;p&gt;Acme | Senior AI Engineer | Remote&lt;/p&gt;"
        "&lt;p&gt;Acme is hiring a founding engineer to build LLM agents that plan, reason and "
        "execute complex multi-step research tasks at scale. Apply at "
        "https://jobs.acme.com/apply and comment on https://news.ycombinator.com/item?id=9.&lt;/p&gt;"
    )
    rss_xml = _job_feed_xml([
        ("Who is hiring?", "https://news.ycombinator.com/item?id=9", "Fri, 04 Sep 2026 10:00:00 GMT", long_desc),
        ("Who is hiring?", "https://news.ycombinator.com/item?id=10", "Fri, 04 Sep 2026 10:00:00 GMT", "&lt;p&gt;Acme | AI Engineer | SF&lt;/p&gt;"),
        ("Who is hiring?", "https://news.ycombinator.com/item?id=11", "Fri, 04 Sep 2026 10:00:00 GMT", "&lt;p&gt;A bakery wants a pastry chef in NYC.&lt;/p&gt;"),
        ("Who is hiring?", "https://news.ycombinator.com/item?id=12", "Mon, 25 Aug 2026 00:00:00 GMT", "&lt;p&gt;Acme | LLM Engineer | Remote&lt;/p&gt;"),
        ("Who is hiring?", "https://news.ycombinator.com/item?id=13", "Fri, 04 Sep 2026 10:00:00 GMT", "&lt;p&gt;Senior AI Engineer in Berlin&lt;/p&gt;"),
    ])
    crawler.fetch = lambda url, *a, **k: _future(rss_xml)  # type: ignore[method-assign]

    # Act
    jobs = await crawler.fetch_yc_hn_jobs()
    await crawler.close()

    # Assert
    # id=9: LLM path, external apply URL, remote inferred from pipe location
    # id=10: short description (no LLM call), still collected
    # id=11: non-AI title -> skipped
    # id=12: stale feed date -> rejected
    # id=13: generic first line -> company falls back to author/AI Startup
    assert len(jobs) == 3
    assert jobs[0].source.url == "https://jobs.acme.com/apply"
    assert jobs[0].content.company == "Acme"
    assert jobs[0].content.is_remote is True
    assert jobs[1].content.title == "AI Engineer"
    assert jobs[2].content.company in ("AI Startup", "")


@freeze_time("2026-09-04T12:00:00Z")
@pytest.mark.asyncio
async def test_jobs_crawler_crawl_dispatcher_board_failure(monkeypatch):
    # Arrange - crawl() gathers all five boards concurrently; one board raising must
    # degrade to a zero-fresh stamp instead of aborting the whole crawl.
    import asyncio

    from src.schemas.entities import JobContent, JobRecord, SourceMetadata

    crawler = JobsCrawler()

    def make_rec(title):
        return JobRecord(
            source=SourceMetadata(name="RemoteOK AI", url=f"https://remoteok.com/jobs/{title}"),
            content=JobContent(
                company="Acme", title=title, date=datetime.now(timezone.utc),
                is_remote=True, role_family=classify_role_family(title),
            ),
        )

    async def fetch_remoteok():
        return [make_rec("AI Engineer")]

    async def fetch_arbeitnow():
        raise RuntimeError("board timeout")

    async def fetch_empty():
        return []

    crawler.fetch_remoteok = fetch_remoteok  # type: ignore[method-assign]
    crawler.fetch_arbeitnow = fetch_arbeitnow  # type: ignore[method-assign]
    crawler.fetch_himalayas = fetch_empty  # type: ignore[method-assign]
    crawler.fetch_weworkremotely = fetch_empty  # type: ignore[method-assign]
    crawler.fetch_yc_hn_jobs = fetch_empty  # type: ignore[method-assign]

    # Act
    records = await crawler.crawl()
    await crawler.close()

    # Assert - failing board excluded, others merged; freshness stamp records the zero
    from src.utils.run_state import load_source_freshness

    assert len(records) == 1
    assert records[0].content.company == "Acme"
    freshness = load_source_freshness("jobs")
    assert freshness["Arbeitnow AI Jobs"]["recent_fresh_counts"][-1] == 0
    assert freshness["RemoteOK AI"]["recent_fresh_counts"][-1] == 1

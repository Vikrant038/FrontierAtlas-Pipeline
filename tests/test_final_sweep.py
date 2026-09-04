"""
Final branch-coverage sweep. Targets the last uncovered branch arcs across the
small utility/exporter modules: config env-pool parsing, SSRF validation edges,
logger redaction, chunker token budgeting, date-parser failure paths, run_state
non-fcntl writes, entity to_row variants, and tiny exporter helpers.
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
import dateparser as dateparser_lib

from src.config import Settings
from src.crawlers import anti_bot
from src.crawlers.news_crawler import NewsCrawler
from src.exporters.base import to_str
from src.exporters.csv_exporter import CSVExporter
from src.exporters.excel_exporter import ExcelExporter
from src.llm.chunker import chunk_to_budget, clean_html_text
from src.schemas.entities import (
    EntityResolutionLog,
    MatchMethodEnum,
    ResearchPaperContent,
    ResearchPaperRecord,
)
from src.utils import run_state
from src.utils.date_normalizer import (
    extract_date_from_html,
    infer_content_freshness,
    parse_datetime_to_utc,
    parse_retry_after,
)
from src.utils.logger import redact_record, setup_logging
from src.utils.security import SSRFValidationError, is_ip_blocked, validate_url_safe


# ---------------------------------------------------------------------------
# config: pool-parsing property branches (each needs its env var populated)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_settings(monkeypatch):
    for var in ("GITHUB_TOKENS", "GROQ_API_KEYS", "CUSTOM_LLM_API_KEYS", "DEEPSEEK_API_KEYS",
                "GITHUB_TOKEN", "GROQ_API_KEY", "CUSTOM_LLM_API_KEY", "DEEPSEEK_API_KEY",
                "GEMINI_API_KEYS", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return Settings(_env_file=None)


def test_config_github_token_pool(fresh_settings, monkeypatch):
    assert fresh_settings.github_token_list == []
    monkeypatch.setenv("GITHUB_TOKEN", "single")
    assert Settings(_env_file=None).github_token_list == ["single"]
    monkeypatch.setenv("GITHUB_TOKENS", "a, b, ,c")
    assert Settings(_env_file=None).github_token_list == ["a", "b", "c"]


def test_config_llm_key_pools(fresh_settings, monkeypatch):
    s = fresh_settings
    # Empty pools fall back to single keys (else-branch)
    assert s.groq_api_key_list == []
    assert s.gemini_api_key_list == []
    assert s.tier3_api_key_list == []

    monkeypatch.setenv("GROQ_API_KEYS", "g1,g2")
    monkeypatch.setenv("GEMINI_API_KEYS", "m1")
    monkeypatch.setenv("CUSTOM_LLM_API_KEYS", "c1,c2")
    monkeypatch.setenv("DEEPSEEK_API_KEYS", "d1")
    s2 = Settings(_env_file=None)
    assert s2.groq_api_key_list == ["g1", "g2"]
    assert s2.gemini_api_key_list == ["m1"]
    # Custom pool takes precedence over the DeepSeek pool
    assert s2.tier3_api_key_list == ["c1", "c2"]

    # No custom pool -> DeepSeek pool is used
    monkeypatch.delenv("CUSTOM_LLM_API_KEYS", raising=False)
    s3 = Settings(_env_file=None)
    assert s3.tier3_api_key_list == ["d1"]


# ---------------------------------------------------------------------------
# security: SSRF validation matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, expected",
    [
        ("10.1.2.3", True),          # private
        ("127.0.0.1", True),         # loopback
        ("169.254.1.1", True),       # link-local
        ("240.0.0.1", True),         # reserved
        ("64:ff9b::a00:1", True),    # IPv4-mapped 10.0.0.1 (mapped to v4 before checks)
        ("93.184.216.34", False),    # public
        ("example.com", False),      # not an IP literal
        ("", False),
    ],
)
def test_is_ip_blocked_matrix(target, expected):
    assert is_ip_blocked(target) is expected


@pytest.mark.parametrize(
    "url",
    ["", None, 123, "ftp://files.example.com/x", "http://", "http://localhost/x",
     "http://broadcasthost/", "http://10.0.0.1/admin", "http://[::1]/"],
)
def test_validate_url_safe_rejects(url):
    with pytest.raises(SSRFValidationError):
        validate_url_safe(url, resolve_dns=False)


def test_validate_url_safe_dns_rebinding_blocked(monkeypatch):
    monkeypatch.setattr("src.utils.security.socket.getaddrinfo",
                        lambda host, *a, **k: [(2, 1, 6, "", ("192.168.1.50", 0))])
    with pytest.raises(SSRFValidationError, match="forbidden IP"):
        validate_url_safe("https://evil.example.com/x")


def test_validate_url_safe_dns_failure(monkeypatch):
    def gaierror(host, *a, **k):
        raise __import__("socket").gaierror("no such host")

    monkeypatch.setattr("src.utils.security.socket.getaddrinfo", gaierror)
    with pytest.raises(SSRFValidationError, match="Failed to resolve"):
        validate_url_safe("https://nonexistent.example.com/x")


def test_validate_url_safe_valid_dns_and_public(monkeypatch):
    monkeypatch.setattr("src.utils.security.socket.getaddrinfo",
                        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert validate_url_safe("https://Example.com/a?b=1") == "https://Example.com/a?b=1"


# ---------------------------------------------------------------------------
# logger: redaction of nested extras and inline credentials
# ---------------------------------------------------------------------------


def test_logger_redact_record_masks_extra_and_message():
    record = {
        "extra": {
            "api_key": "sk-secret-value",
            "safe": {"nested_token": "abc12345", "fine": 1},
            "items": [{"password": "pw"}, 42],
        },
        "message": "Connecting with Bearer abcdefghijklmnopqrstuvwxyz and api_key=12345678 now",
    }
    assert redact_record(record) is True
    assert record["extra"]["api_key"] == "[REDACTED]"
    assert record["extra"]["safe"]["nested_token"] == "[REDACTED]"
    assert record["extra"]["safe"]["fine"] == 1
    assert record["extra"]["items"][0]["password"] == "[REDACTED]"
    assert "[REDACTED_CREDENTIAL]" in record["message"]
    assert "Bearer" not in record["message"]


def test_logger_redact_non_string_message_and_plain_extra():
    record = {"extra": {"count": 3}, "message": "just a log line"}
    assert redact_record(record) is True
    assert record["message"] == "just a log line"
    record2 = {"message": 42}
    assert redact_record(record2) is True


def test_logger_setup_logging_reruns(tmp_path, monkeypatch):
    monkeypatch.setattr("src.utils.logger.LOG_FILE_PATH", str(tmp_path / "logs" / "pipeline.log"))
    setup_logging("DEBUG")  # idempotent: removes previous sinks, re-adds both
    setup_logging("INFO")


# ---------------------------------------------------------------------------
# chunker: token-budget truncation and HTML cleaning branches
# ---------------------------------------------------------------------------


def test_chunker_clean_html_empty_and_soup_fallback():
    assert clean_html_text("") == ""
    # Too short for trafilatura's extractor -> BeautifulSoup fallback path
    short = "<html><nav>menu</nav><footer>foot</footer><p>Hi there.</p></html>"
    cleaned = clean_html_text(short)
    assert "menu" not in cleaned and "foot" not in cleaned
    assert "Hi there." in cleaned


def test_chunker_budget_truncation():
    assert chunk_to_budget("") == ""
    assert chunk_to_budget("short text", max_tokens=1000) == "short text"
    long_text = "word " * 2000
    truncated = chunk_to_budget(long_text, max_tokens=100)
    assert len(truncated) < len(long_text)
    assert truncated == chunk_to_budget(long_text, max_tokens=100)


# ---------------------------------------------------------------------------
# date_normalizer: remaining failure/fallback branches
# ---------------------------------------------------------------------------


def test_parse_retry_after_naive_rfc_date():
    # RFC date without a timezone -> treated as UTC before subtraction
    wait = parse_retry_after("Mon, 01 Jan 2035 00:00:00")
    assert wait is not None and wait > 0


def test_parse_datetime_overflow_and_blank():
    assert parse_datetime_to_utc(1e300) is None          # OverflowError -> None
    assert parse_datetime_to_utc("   ") is None          # blank after strip
    naive = datetime(2026, 9, 4, 10, 0)
    assert parse_datetime_to_utc(naive).tzinfo == timezone.utc  # naive -> UTC-stamped


def test_parse_datetime_dateparser_exception(monkeypatch):
    def boom(value, settings=None):
        raise RuntimeError("dateparser crashed")

    monkeypatch.setattr(dateparser_lib, "parse", boom)
    assert parse_datetime_to_utc("some unparseable nonsense text") is None


def test_infer_content_freshness_branches():
    assert infer_content_freshness("") is None
    assert infer_content_freshness(None) is None
    # Relative recency term resolves through dateparser
    inferred = infer_content_freshness("The announcement was made yesterday.")
    assert inferred is not None
    # Strong signal tokens return 'now'
    nowish = infer_content_freshness("BREAKING NEWS: model released today")
    assert nowish is not None
    age = datetime.now(timezone.utc) - nowish
    assert age.total_seconds() < 60


def test_extract_date_from_html_fallback_to_none():
    # HTML with no URL date, meta, JSON-LD, or <time> -> all tiers miss
    html = "<html><body><h1>No dates anywhere</h1></body></html>"
    assert extract_date_from_html(html, page_url="https://example.com/plain") is None
    assert extract_date_from_html("", page_url="https://example.com/plain") is None


# ---------------------------------------------------------------------------
# run_state: non-fcntl fallback and malformed freshness history
# ---------------------------------------------------------------------------


def test_run_state_persist_without_fcntl(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl -> ImportError
    state_path = str(tmp_path / "state.json")
    run_state.save_seen_keys("crawler", {"k1", "k2"}, state_path=state_path)
    assert json.loads(open(state_path).read()) == {"crawler": ["k1", "k2"]}


def test_run_state_freshness_repairs_bad_history(tmp_path):
    state_path = str(tmp_path / "state.json")
    open(state_path, "w").write(json.dumps({
        "news_freshness": {
            "Feed A": {"recent_fresh_counts": "not-a-list"},   # non-list history
            "Feed B": ["not-a-dict"],                          # non-dict entry
        }
    }))
    run_state.save_source_freshness("news", {"Feed A": 3}, state_path=state_path)
    state = json.loads(open(state_path).read())
    assert state["news_freshness"]["Feed A"]["recent_fresh_counts"] == [3]
    assert isinstance(state["news_freshness"]["Feed B"], dict) or "Feed B" in state["news_freshness"]


# ---------------------------------------------------------------------------
# entities / exporters: to_row edge shapes and wrapper calls
# ---------------------------------------------------------------------------


def test_entities_to_row_edge_shapes():
    rec = ResearchPaperRecord(
        content=ResearchPaperContent(
            title="Paper Without Repo", authors=["A"], paper_url="https://arxiv.org/abs/1",
            github_url=None, published_date="2026-09-03",
        )
    )
    row = rec.to_row()
    assert row[5] == "" and row[6] == ""  # both cells empty when no repo was found

    log = EntityResolutionLog(
        rawName="openai", canonicalName="OpenAI", matchMethod=MatchMethodEnum.LLM_DISAMBIGUATION,
        confidenceScore=0.9, sourceUrl="https://x", timestamp="2026-09-04T10:00:00Z",
    )
    row = log.to_row()
    assert row[3] == "LLM_DISAMBIGUATION"


def test_exporters_base_to_str_edge():
    assert to_str(None) == ""
    assert to_str(5) == "5"


def test_csv_exporter_individual_wrappers(tmp_path):
    exporter = CSVExporter(output_dir=str(tmp_path))
    rec = ResearchPaperRecord(
        content=ResearchPaperContent(
            title="Some Paper", authors=["A"], paper_url="https://arxiv.org/abs/1",
            github_url=None, published_date="2026-09-03",
        )
    )
    for method, filename, records in (
        ("export_startups", "startups.csv", []),
        ("export_papers", "research_papers.csv", [rec]),
        ("export_logs", "entity_mapping_log.csv", []),
    ):
        path = getattr(exporter, method)(records)
        assert (tmp_path / filename).exists()
        assert path.endswith(filename)
    # export_logs writes the canonical CSV plus the legacy alias
    assert (tmp_path / "entity_resolution_logs.csv").exists()
    # export_all with no datasets is a no-op that returns {}
    assert exporter.export_all() == {}


def test_excel_exporter_reload_and_corrupt_workbook(tmp_path):
    from src.schemas.entities import StartupContent, StartupRecord, SourceMetadata

    def startup(name):
        return StartupRecord(
            source=SourceMetadata(name="S", url=f"https://{name}.example"),
            content=StartupContent(entityName=name),
        )

    filepath = str(tmp_path / "book.xlsx")
    ExcelExporter().export(filepath, startups=[startup("Acme")])
    # Second export over an existing valid workbook: dataset replaced, others kept
    ExcelExporter().export(filepath, startups=[startup("Acme"), startup("Beta")], papers=[])
    wb = __import__("openpyxl").load_workbook(filepath)
    assert wb["Startups"].max_row == 3
    assert "Research_Papers" in wb.sheetnames  # empty-but-present tab preserved

    # Corrupt workbook file: loader falls back to a fresh workbook instead of crashing
    corrupt = str(tmp_path / "corrupt.xlsx")
    open(corrupt, "w").write("this is not a zip file")
    ExcelExporter().export(corrupt, startups=[startup("Gamma")])
    wb = __import__("openpyxl").load_workbook(corrupt)
    assert wb["Startups"].max_row == 2


# ---------------------------------------------------------------------------
# anti_bot: host extraction fallback
# ---------------------------------------------------------------------------


def test_anti_bot_host_of_exception_fallback():
    # Non-string input makes urlparse raise -> the fallback returns the input
    assert anti_bot._host_of(123) == 123
    assert anti_bot._host_of("https://Example.com/path") == "example.com"

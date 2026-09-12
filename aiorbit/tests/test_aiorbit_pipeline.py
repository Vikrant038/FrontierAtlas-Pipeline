"""
Unit tests for AIOrbit MCP Curation Pipeline (Stage 2).
Tests schema validation, scoring boundaries, detail page extraction, and dedup canonicalization.
All tests are 100% hermetic and offline (no network).
"""

from datetime import datetime, timezone, timedelta
import pytest
from pydantic import ValidationError

from aiorbit.schema import MCPEntry
from aiorbit.scoring import (
    score_activity,
    score_adoption,
    score_documentation,
    score_reliability,
    compute_deterministic_score,
)
from aiorbit.http_common import extract_detail_page_data


class TestMCPEntrySchema:
    """Validate strict Pydantic v2 schema constraints."""

    def test_valid_minimal_mcp_entry(self):
        entry = MCPEntry(
            name="Postgres MCP",
            mcp_type="SERVER",
            listing_url="https://creati.ai/mcp/postgres-mcp/"
        )
        assert entry.name == "Postgres MCP"
        assert entry.mcp_type == "SERVER"
        assert entry.verification_status == "PENDING"
        assert entry.github_owner_repo is None
        assert entry.pricing is None

    def test_invalid_mcp_type_fails(self):
        with pytest.raises(ValidationError):
            MCPEntry(
                name="Invalid",
                mcp_type="MIDDLEWARE",  # must be SERVER or CLIENT
                listing_url="https://creati.ai/mcp/invalid/"
            )

    def test_clean_owner_repo_validator(self):
        entry = MCPEntry(
            name="Test",
            mcp_type="CLIENT",
            listing_url="https://creati.ai/mcp/test/",
            github_owner_repo="https://github.com/ModelContextProtocol/Servers/"
        )
        assert entry.github_owner_repo == "modelcontextprotocol/servers"


class TestDeterministicScoring:
    """Validate 100-point rubric deterministic boundaries (max 40 pts)."""

    @pytest.fixture
    def fixed_now(self):
        return datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)

    def test_activity_score_tiers(self, fixed_now):
        # 1. Archived -> 0.0
        assert score_activity("2026-09-01T00:00:00Z", archived=True, now=fixed_now) == 0.0

        # 2. <= 30 days -> 15.0
        t_10d = (fixed_now - timedelta(days=10)).isoformat()
        assert score_activity(t_10d, archived=False, now=fixed_now) == 15.0

        # 3. <= 90 days -> 10.0
        t_60d = (fixed_now - timedelta(days=60)).isoformat()
        assert score_activity(t_60d, archived=False, now=fixed_now) == 10.0

        # 4. <= 180 days -> 5.0
        t_120d = (fixed_now - timedelta(days=120)).isoformat()
        assert score_activity(t_120d, archived=False, now=fixed_now) == 5.0

        # 5. > 180 days -> 0.0
        t_250d = (fixed_now - timedelta(days=250)).isoformat()
        assert score_activity(t_250d, archived=False, now=fixed_now) == 0.0

        # 6. Unknown -> 0.0
        assert score_activity(None, archived=False, now=fixed_now) == 0.0

    def test_adoption_score_tiers(self):
        assert score_adoption(0) == 0.0
        assert score_adoption(None) == 0.0
        assert score_adoption(5) == 2.0
        assert score_adoption(42) == 4.0
        assert score_adoption(250) == 6.0
        assert score_adoption(1500) == 8.0
        assert score_adoption(5000) == 10.0

    def test_documentation_score(self):
        assert score_documentation("https://docs.mcp.run") == 5.0
        assert score_documentation(None, has_readme=True) == 3.0
        assert score_documentation("", has_readme=False) == 0.0

    def test_reliability_score(self):
        assert score_reliability(site_alive=True, is_fork=False) == 5.0
        assert score_reliability(site_alive=True, is_fork=True) == 2.5
        assert score_reliability(site_alive=False, is_fork=False) == 1.0

    def test_full_deterministic_score_aggregation(self, fixed_now):
        entry = MCPEntry(
            name="Top MCP",
            mcp_type="SERVER",
            listing_url="https://creati.ai/mcp/top-mcp/",
            github_stars=3000,
            github_pushed_at=(fixed_now - timedelta(days=5)).isoformat(),
            github_archived=False,
            docs_url="https://top.mcp/docs",
        )
        act, total = compute_deterministic_score(entry, has_readme=True, is_fork=False, site_alive=True, now=fixed_now)
        assert act == 15.0
        # 15 (act) + 10 (stars) + 5 (docs) + 5 (recency) + 5 (reliability) = 40.0 / 45.0
        assert total == 40.0


class TestDetailExtraction:
    """Validate extraction of metadata and GitHub URLs from Creati.ai HTML."""

    def test_extract_detail_html(self):
        sample_html = """
        <html>
        <head><title>MySQL MCP Server | Creati.ai</title></head>
        <body>
            <h1>MySQL MCP Server</h1>
            <meta name="description" content="High performance database MCP connector">
            <div class="category-tags">
                <a href="/mcp/categories/databases/">Databases</a>
            </div>
            <div class="pricing">Pricing: Free open source</div>
            <div><span>★ 45 Stars</span></div>
            <div>
                <a href="https://github.com/mcp-community/mysql-server">Visit MCP</a>
                <a href="https://github.com/mcp-community/.github/profile">README</a>
                <a href="https://mysql-mcp.org">Official Site</a>
            </div>
        </body>
        </html>
        """
        data = extract_detail_page_data(sample_html)
        assert data["name"] == "MySQL MCP Server"
        assert "High performance" in data["description"]
        assert data["category"] == "Databases"
        assert data["pricing"] == "Free"
        assert data["github_owner_repo"] == "mcp-community/mysql-server"
        assert data["github_url"] == "https://github.com/mcp-community/mysql-server"
        assert data["official_url"] == "https://mysql-mcp.org"


class TestDeduplicationAndCapping:
    """Validate Guideline §7 canonical merging and §8 cluster saturation capping."""

    def test_canonical_key_generation(self):
        from aiorbit.dedup import clean_canonical_key

        e1 = MCPEntry(name="Postgres", mcp_type="SERVER", listing_url="https://c.ai/1", github_owner_repo="org/repo")
        assert clean_canonical_key(e1) == "gh:org/repo"

        e2 = MCPEntry(name="Web App", mcp_type="CLIENT", listing_url="https://c.ai/2", official_url="https://myapp.ai/")
        assert clean_canonical_key(e2) == "url:myapp.ai"

        e3 = MCPEntry(name="Custom Tool!", mcp_type="SERVER", listing_url="https://c.ai/3")
        assert clean_canonical_key(e3) == "name:customtool"

    def test_merge_two_entries(self):
        from aiorbit.dedup import merge_two_entries

        primary = MCPEntry(
            name="Alpha MCP",
            mcp_type="SERVER",
            listing_url="https://c.ai/a",
            discovery_source="Creati.ai",
            github_stars=100,
            overall_score=80.0,
            description="Alpha description",
        )
        secondary = MCPEntry(
            name="Alpha MCP Copy",
            mcp_type="SERVER",
            listing_url="https://c.ai/b",
            discovery_source="GitHub",
            github_stars=50,
            overall_score=75.0,
            official_url="https://alpha.io",
            license="MIT",
        )
        merged = merge_two_entries(primary, secondary)
        assert merged.name == "Alpha MCP"
        assert merged.github_stars == 100
        assert merged.description == "Alpha description"
        assert merged.official_url == "https://alpha.io"
        assert merged.license == "MIT"
        assert "Creati.ai" in merged.discovery_source and "GitHub" in merged.discovery_source

    def test_apply_saturation_capping(self):
        from aiorbit.dedup import apply_saturation_capping

        # 3 postgres servers, 1 client
        p1 = MCPEntry(name="Postgres Pro", mcp_type="SERVER", listing_url="https://c/1", overall_score=90.0)
        p2 = MCPEntry(name="Postgres Fast", mcp_type="SERVER", listing_url="https://c/2", overall_score=85.0)
        p3 = MCPEntry(name="Postgres Simple", mcp_type="SERVER", listing_url="https://c/3", overall_score=60.0)
        c1 = MCPEntry(name="Postgres Client App", mcp_type="CLIENT", listing_url="https://c/4", overall_score=50.0)

        # Cap servers to max 2
        survivors = apply_saturation_capping([p1, p2, p3, c1], max_per_cluster=2)
        survivor_names = [s.name for s in survivors]
        assert "Postgres Pro" in survivor_names
        assert "Postgres Fast" in survivor_names
        assert "Postgres Simple" not in survivor_names  # Dropped (saturated clone)
        assert "Postgres Client App" in survivor_names  # Client preserved


class TestFullRubricAndExport:
    """Validate 100-point score aggregation and export row flattening."""

    def test_full_100_point_aggregation(self):
        entry = MCPEntry(
            name="Super MCP",
            mcp_type="SERVER",
            listing_url="https://creati.ai/mcp/super-mcp/",
            activity_score=15.0,
            adoption_score=10.0,
            docs_score=5.0,
            reliability_score=5.0,
            usefulness_score=30.0,
            quality_score=25.0,
            differentiation_score=5.0,
        )
        total = (
            entry.activity_score
            + entry.adoption_score
            + entry.docs_score
            + entry.reliability_score
            + entry.usefulness_score
            + entry.quality_score
            + entry.differentiation_score
        )
        entry.overall_score = round(min(total, 100.0), 1)
        assert entry.overall_score == 95.0

    def test_export_entry_to_row(self):
        from aiorbit.run_export import entry_to_row, SERVER_COLUMNS

        entry = MCPEntry(
            name="Test MCP",
            mcp_type="SERVER",
            listing_url="https://creati.ai/mcp/test-mcp/",
            category="Databases",
            github_stars=120,
            overall_score=82.5,
            usefulness_score=24.0,
            quality_score=21.0,
            key_capabilities=["Query tables", "List databases"],
            supported_ai_clients=["Claude Desktop", "Cursor"],
            transport="stdio",
        )
        row = entry_to_row(entry, rank=1)
        assert row["rank"] == 1
        assert row["name"] == "Test MCP"
        assert row["overall_score"] == 82.5
        assert "Query tables" in row["key_capabilities_str"]
        assert "Claude Desktop" in row["supported_ai_clients_str"]
        assert row["transport"] == "stdio"
        for _, col_key in SERVER_COLUMNS:
            assert col_key in row

"""
Schema definitions for AIOrbit MCP Curation Pipeline (Stage 2).
Strict Pydantic v2 types, None-when-unknown, zero fabricated defaults.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


class MCPEntry(BaseModel):
    """Normalized MCP record matching AIOrbit Curation Guideline §9."""

    name: str = Field(..., description="Canonical product or repository name")
    mcp_type: Literal["SERVER", "CLIENT"] = Field(..., description="Classification as MCP Server or Client")
    listing_url: str = Field(..., description="Directory listing URL where item was discovered")
    discovery_source: str = Field(default="Creati.ai", description="Source directory or registry")

    # Links & Identifiers
    official_url: Optional[str] = Field(default=None, description="Official project or company homepage")
    github_url: Optional[str] = Field(default=None, description="Direct link to GitHub repository")
    github_owner_repo: Optional[str] = Field(default=None, description="Normalized 'owner/repo' identifier")

    # Product Metadata
    description: Optional[str] = Field(default=None, description="Project summary (official README overrides directory)")
    category: Optional[str] = Field(default=None, description="Primary vertical (e.g. Developer Tools, Databases)")
    subcategory: Optional[str] = Field(default=None, description="Granular subcategory")
    company_creator: Optional[str] = Field(default=None, description="Author, organization, or GitHub username")
    pricing: Optional[str] = Field(default=None, description="Pricing tier: Free, Freemium, Paid")
    license: Optional[str] = Field(default=None, description="SPDX license ID or description (e.g. MIT, Apache-2.0)")

    # Technical Telemetry
    github_stars: Optional[int] = Field(default=None, ge=0, description="GitHub stargazers count")
    github_pushed_at: Optional[str] = Field(default=None, description="ISO-8601 timestamp of last repository commit")
    github_archived: Optional[bool] = Field(default=None, description="Whether the GitHub repository is archived")
    docs_url: Optional[str] = Field(default=None, description="Documentation or setup guide URL")

    # Verification & Audit Status
    verification_status: Literal["PENDING", "VERIFIED", "REJECTED"] = Field(
        default="PENDING", description="Verification lifecycle status"
    )
    rejection_reason: Optional[str] = Field(default=None, description="Reason code if rejected (e.g. REPO_GONE, ARCHIVED, SITE_DEAD)")

    # Scoring Breakdown (100-Point Rubric)
    usefulness_score: Optional[float] = Field(default=None, ge=0.0, le=30.0, description="Usefulness / real-world value (max 30)")
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=25.0, description="Quality & functionality score (max 25)")
    activity_score: Optional[float] = Field(default=None, ge=0.0, le=15.0, description="Activity / maintenance score (max 15)")
    adoption_score: Optional[float] = Field(default=None, ge=0.0, le=10.0, description="Adoption / traction score (max 10)")
    docs_score: Optional[float] = Field(default=None, ge=0.0, le=5.0, description="Documentation / ease of use score (max 5)")
    recency_score: Optional[float] = Field(default=None, ge=0.0, le=5.0, description="Recency / momentum score (max 5)")
    reliability_score: Optional[float] = Field(default=None, ge=0.0, le=5.0, description="Reliability / trust score (max 5)")
    differentiation_score: Optional[float] = Field(default=None, ge=0.0, le=5.0, description="Differentiation score (max 5)")
    overall_score: Optional[float] = Field(default=None, ge=0.0, le=100.0, description="Cumulative score (0-100)")

    # Capabilities & Transports (Guideline §9)
    key_capabilities: Optional[list[str]] = Field(default=None, description="Key features or exposed tools/resources")
    supported_ai_clients: Optional[list[str]] = Field(default=None, description="Supported AI clients (e.g. Claude Desktop, Cursor)")
    transport: Optional[str] = Field(default=None, description="Transport protocol (stdio, sse, http, websocket)")
    pricing_type: Optional[str] = Field(default=None, description="Pricing classification (Open Source, Freemium, Paid)")

    # Metadata & Notes
    last_verified_date: Optional[str] = Field(default=None, description="ISO-8601 timestamp of verification pass")
    notes: Optional[str] = Field(default=None, description="Audit trail notes")
    curation_notes: Optional[str] = Field(default=None, description="Curation justification for inclusion/scoring")

    @field_validator("github_owner_repo")
    @classmethod
    def clean_owner_repo(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        clean = v.strip().rstrip("/")
        parts = clean.split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}".lower()
        return clean.lower()

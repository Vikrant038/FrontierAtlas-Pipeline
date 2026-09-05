"""
Data Schemas for FrontierAtlas / GraphOne AI Intelligence Pipeline.
Strictly conforms to PROJECT_CONTEXT.md Section 4 and REQUIREMENTS.md.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PricingModelEnum(str, Enum):
    FREE = "FREE"
    FREEMIUM = "FREEMIUM"
    PAID = "PAID"
    ENTERPRISE = "ENTERPRISE"


class RoleFamilyEnum(str, Enum):
    ENGINEERING = "Engineering"
    RESEARCH = "Research"
    PRODUCT = "Product"
    DESIGN = "Design"
    SALES = "Sales"
    OPERATIONS = "Operations"
    MARKETING = "Marketing"
    LEGAL = "Legal"
    FINANCE = "Finance"
    OTHER = "Other"


class MatchMethodEnum(str, Enum):
    # Values mirror PROJECT_CONTEXT.md Section 4 "Match Method Values" plus
    # MANUAL_OVERRIDE (empty-name sentinel). FUZZY_PARTIAL is specified there but
    # intentionally unimplemented: no partial-ratio matching tier exists in the resolver.
    NORMALIZATION_EXACT = "NORMALIZATION_EXACT"
    FUZZY_TOKEN_SORT = "FUZZY_TOKEN_SORT"
    ALIAS_MATCH = "ALIAS_MATCH"
    LLM_DISAMBIGUATION = "LLM_DISAMBIGUATION"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    NEW_ENTITY = "NEW_ENTITY"


class BaseEntityModel(BaseModel):
    """Base model enforcing UTC timezone and strict configuration."""
    model_config = ConfigDict(
        use_enum_values=True,
        validate_assignment=True,
        populate_by_name=True,
    )

    @field_validator("*", mode="before")
    @classmethod
    def strip_whitespace_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and not isinstance(value, Enum):
            return value.strip()
        return value


class SourceMetadata(BaseEntityModel):
    name: str = Field(..., min_length=1, description="Human-readable name of the source site")
    url: str = Field(..., description="Exact live source URL")


class AuditedEntityRecord(BaseEntityModel):
    """Base record with standard provenance metadata (source, version, collectedAt)."""
    schemaVersion: str = Field(default="1.0", frozen=True)
    source: SourceMetadata
    collectedAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================================================
# 1. STARTUP ENTITY (Schema 1)
# ============================================================================
class StartupContentData(BaseEntityModel):
    employeeCount: Optional[int] = Field(None, ge=1, description="Estimated employee count if available")


class StartupContent(BaseEntityModel):
    entityName: str = Field(..., min_length=1, description="Canonical entity name after resolution")
    data: StartupContentData = Field(default_factory=StartupContentData)


class StartupRecord(AuditedEntityRecord):
    recordType: str = Field(default="STARTUP", frozen=True)
    content: StartupContent

    def to_row(self) -> List[Any]:
        return [
            self.schemaVersion,
            self.recordType,
            self.source.name,
            self.source.url,
            self.content.entityName,
            self.content.data.employeeCount if self.content.data.employeeCount is not None else "N/A",
            self.collectedAt.isoformat(),
        ]


# ============================================================================
# 2. PRODUCT ENTITY (Schema 2)
# ============================================================================
class ProductContent(BaseEntityModel):
    startupName: str = Field(..., min_length=1, description="Canonical startup name")
    productName: str = Field(..., min_length=1, description="Name of the AI product/tool")
    productUrl: Optional[str] = Field(None, description="Direct URL of the product")
    pricingModel: PricingModelEnum = Field(..., description="Standard pricing model enum")


class ProductRecord(AuditedEntityRecord):
    recordType: str = Field(default="PRODUCT", frozen=True)
    content: ProductContent

    def to_row(self) -> List[Any]:
        pm = self.content.pricingModel
        return [
            self.schemaVersion,
            self.recordType,
            self.source.name,
            self.source.url,
            self.content.productName,
            self.content.startupName,
            str(pm.value if hasattr(pm, "value") else pm),
            self.content.productUrl or "N/A",
            self.collectedAt.isoformat(),
        ]


# ============================================================================
# 3. RESEARCH PAPER ENTITY (Schema 3)
# ============================================================================
class ResearchPaperContent(BaseEntityModel):
    title: str = Field(..., min_length=3, description="Full paper title")
    authors: List[str] = Field(..., min_length=1, description="List of author full names")
    paper_url: str = Field(..., description="Arxiv abstract page or direct paper URL")
    github_url: Optional[str] = Field(None, description="Associated GitHub repo URL or None")
    github_stars: Optional[int] = Field(None, ge=0, description="Live fetched GitHub star count")
    published_date: datetime = Field(..., description="Original submission/publish date in UTC")


class ResearchPaperRecord(BaseEntityModel):
    schemaVersion: str = Field(default="1.0", frozen=True)
    recordType: str = Field(default="RESEARCH_PAPER", frozen=True)
    content: ResearchPaperContent

    def to_row(self) -> List[Any]:
        # Distinguish: no repo in abstract (both None → ""), vs repo found but lookup failed (url set, stars None → "N/A")
        if self.content.github_url is None:
            github_url_cell = ""
            github_stars_cell = ""
        else:
            github_url_cell = self.content.github_url
            github_stars_cell = self.content.github_stars if self.content.github_stars is not None else "N/A"
        return [
            self.schemaVersion,
            self.recordType,
            self.content.title,
            ", ".join(self.content.authors),
            self.content.paper_url,
            github_url_cell,
            github_stars_cell,
            self.content.published_date.isoformat(),
        ]


# ============================================================================
# 4. JOB ENTITY (Schema 4 - 24h Freshness Guarantee)
# ============================================================================
class JobContent(BaseEntityModel):
    company: str = Field(..., min_length=1, description="Canonical company name")
    title: str = Field(..., min_length=1, description="Job title")
    date: datetime = Field(..., description="Job posting date in UTC (must be <= 24h old)")
    is_remote: bool = Field(..., description="Whether role is remote")
    role_family: RoleFamilyEnum = Field(default=RoleFamilyEnum.OTHER)


class JobRecord(AuditedEntityRecord):
    recordType: str = Field(default="JOB", frozen=True)
    content: JobContent

    def to_row(self) -> List[Any]:
        rf = self.content.role_family
        return [
            self.schemaVersion,
            self.recordType,
            self.source.name,
            self.source.url,
            self.content.company,
            self.content.title,
            self.content.date.isoformat(),
            self.content.is_remote,
            str(rf.value if hasattr(rf, "value") else rf),
            self.collectedAt.isoformat(),
        ]


# ============================================================================
# 5. NEWS ENTITY (Schema 5 - 24h Freshness Guarantee)
# ============================================================================
class NewsContent(BaseEntityModel):
    title: str = Field(..., min_length=5, description="Article headline")
    published_date: datetime = Field(..., description="Publication date in UTC (must be <= 24h old)")
    summary: Optional[str] = Field(None, description="Concise summary or lead paragraph")
    full_text: str = Field(..., min_length=20, description="Full crawled article body text")


class NewsRecord(AuditedEntityRecord):
    recordType: str = Field(default="NEWS", frozen=True)
    content: NewsContent

    def to_row(self) -> List[Any]:
        return [
            self.schemaVersion,
            self.recordType,
            self.source.name,
            self.source.url,
            self.content.title,
            self.content.published_date.isoformat(),
            self.content.summary or (self.content.full_text[:200] + "..."),
            self.collectedAt.isoformat(),
        ]


# ============================================================================
# 6. ENTITY RESOLUTION AUDIT LOG (Schema 6 - Tab 6)
# ============================================================================
class EntityResolutionLog(BaseEntityModel):
    rawName: str = Field(..., min_length=1, description="Raw unnormalized company name")
    canonicalName: str = Field(..., min_length=1, description="Resolved canonical company name")
    entityType: str = Field(default="STARTUP", description="Entity classification")
    matchMethod: MatchMethodEnum = Field(..., description="Resolution tier method used")
    confidenceScore: float = Field(..., ge=0.0, le=1.0, description="Confidence score 0.0 - 1.0")
    sourceUrl: str = Field(..., description="Source URL where raw name was observed")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_row(self) -> List[Any]:
        mm = self.matchMethod
        return [
            self.rawName,
            self.canonicalName,
            self.entityType,
            str(mm.value if hasattr(mm, "value") else mm),
            self.confidenceScore,
            self.sourceUrl,
            self.timestamp.isoformat(),
        ]

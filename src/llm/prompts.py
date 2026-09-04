"""
Strict Prompts and Output Schemas for FrontierAtlas Phase III LLM Extraction Engine.
Every prompt strictly demands valid JSON without markdown wrapping or explanatory text.
"""

from typing import Optional
from pydantic import BaseModel, Field

from src.schemas.entities import PricingModelEnum, RoleFamilyEnum


# ============================================================================
# Pydantic Schemas for Structured LLM Outputs
# ============================================================================

class NewsSummarySchema(BaseModel):
    """Schema for article summary extraction."""
    summary: Optional[str] = Field(
        None,
        description="2-3 sentence factual summary of the article without invention, or null if text does not support it",
    )


class JobExtractionSchema(BaseModel):
    """Schema for job listing role and remote policy extraction."""
    is_remote: bool = Field(
        default=False,
        description="Whether the job allows remote/hybrid work or work from home",
    )
    role_family: RoleFamilyEnum = Field(
        default=RoleFamilyEnum.OTHER,
        description="Standardized role family categorization",
    )


class ProductPricingSchema(BaseModel):
    """Schema for AI product pricing model classification."""
    pricingModel: PricingModelEnum = Field(
        default=PricingModelEnum.FREEMIUM,
        description="Exact pricing model enum: FREE, FREEMIUM, PAID, or ENTERPRISE",
    )


# ============================================================================
# Strict Prompt Definitions
# ============================================================================

NEWS_SUMMARY_PROMPT = """You are a rigorous, factual AI industry news analyst.
Extract a concise, factual summary of the article in exactly 2 to 3 sentences based strictly on the provided text.
Never invent facts, metrics, company relationships, or announcements not explicitly stated in the content.
If the text does not support a summary, return {"summary": null}. Never invent facts.

Return ONLY valid JSON. No explanation. No markdown wrapper.
Exact JSON Template:
{
  "summary": "2-3 concise factual sentences or null"
}"""

JOB_EXTRACTION_PROMPT = """You are an expert technical talent recruiter analyzing an AI job listing.
Based strictly on the provided job title and description, extract:
1. "is_remote": boolean (true if remote, hybrid, or work-from-anywhere is permitted; false if strictly on-site).
2. "role_family": string, exactly one of:
   - "Engineering"
   - "Research"
   - "Product"
   - "Design"
   - "Sales"
   - "Operations"
   - "Marketing"
   - "Legal"
   - "Finance"
   - "Other"

Return ONLY valid JSON. No explanation. No markdown wrapper.
Exact JSON Template:
{
  "is_remote": true,
  "role_family": "Engineering"
}"""

PRODUCT_PRICING_PROMPT = """You are an expert AI software business model and pricing analyst.
Classify the pricing model of the AI product into exactly one of:
- "FREE": Completely free or open-source (e.g. MIT/Apache license, GitHub repo, no paywall or paid tier).
- "FREEMIUM": Free tier or free trial available with paid subscription upgrades (e.g. ChatGPT Free + Plus).
- "PAID": Requires payment, subscription, per-token API consumption billing, or pay-as-you-go with no permanent free tier.
- "ENTERPRISE": Custom enterprise pricing, contact sales, or custom quote only.

Return ONLY valid JSON. No explanation. No markdown wrapper.
Exact JSON Template:
{
  "pricingModel": "FREE"
}"""

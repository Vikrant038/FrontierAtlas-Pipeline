"""
Unit tests for strict prompts and extraction schemas (Phase III: 3A).
Follows AAA pattern per CODING_STANDARDS.md.
"""

import pytest
from src.llm.prompts import (
    JOB_EXTRACTION_PROMPT,
    NEWS_SUMMARY_PROMPT,
    PRODUCT_PRICING_PROMPT,
    JobExtractionSchema,
    NewsSummarySchema,
    ProductPricingSchema,
)
from src.schemas.entities import PricingModelEnum, RoleFamilyEnum


def test_news_summary_prompt_structure():
    # Arrange & Act & Assert
    assert "Return ONLY valid JSON. No explanation. No markdown wrapper." in NEWS_SUMMARY_PROMPT
    assert '"summary": "2-3 concise factual sentences or null"' in NEWS_SUMMARY_PROMPT
    assert "Never invent facts" in NEWS_SUMMARY_PROMPT


def test_job_extraction_prompt_structure():
    # Arrange & Act & Assert
    assert "Return ONLY valid JSON. No explanation. No markdown wrapper." in JOB_EXTRACTION_PROMPT
    assert '"is_remote": true' in JOB_EXTRACTION_PROMPT
    assert '"role_family": "Engineering"' in JOB_EXTRACTION_PROMPT


def test_product_pricing_prompt_structure():
    # Arrange & Act & Assert
    assert "Return ONLY valid JSON. No explanation. No markdown wrapper." in PRODUCT_PRICING_PROMPT
    assert '"pricingModel": "FREE"' in PRODUCT_PRICING_PROMPT
    assert "FREE" in PRODUCT_PRICING_PROMPT
    assert "FREEMIUM" in PRODUCT_PRICING_PROMPT
    assert "PAID" in PRODUCT_PRICING_PROMPT
    assert "ENTERPRISE" in PRODUCT_PRICING_PROMPT


def test_news_summary_schema_validation():
    # Arrange
    valid_payload = {"summary": "Company launched a new AI cluster. The cluster delivers 10x throughput."}

    # Act
    obj = NewsSummarySchema.model_validate(valid_payload)

    # Assert
    assert obj.summary == "Company launched a new AI cluster. The cluster delivers 10x throughput."

    # Arrange null summary
    null_payload = {"summary": None}

    # Act
    null_obj = NewsSummarySchema.model_validate(null_payload)

    # Assert
    assert null_obj.summary is None


def test_job_extraction_schema_validation():
    # Arrange
    payload = {"is_remote": True, "role_family": "Research"}

    # Act
    obj = JobExtractionSchema.model_validate(payload)

    # Assert
    assert obj.is_remote is True
    assert obj.role_family == RoleFamilyEnum.RESEARCH


def test_product_pricing_schema_validation():
    # Arrange
    payload = {"pricingModel": "PAID"}

    # Act
    obj = ProductPricingSchema.model_validate(payload)

    # Assert
    assert obj.pricingModel == PricingModelEnum.PAID

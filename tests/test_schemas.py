"""
Unit tests for strict Pydantic v2 schemas and validation boundaries.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from src.schemas.entities import (
    PricingModelEnum,
    ProductContent,
    ProductRecord,
    ResearchPaperContent,
    ResearchPaperRecord,
    SourceMetadata,
    StartupContent,
    StartupContentData,
    StartupRecord,
)


def test_startup_record_valid():
    # Arrange & Act
    record = StartupRecord(
        source=SourceMetadata(name="YC AI", url="https://ycombinator.com/companies/openai"),
        content=StartupContent(
            entityName="OpenAI",
            data=StartupContentData(employeeCount=1500)
        )
    )

    # Assert
    assert record.schemaVersion == "1.0"
    assert record.recordType == "STARTUP"
    assert record.content.entityName == "OpenAI"
    assert record.content.data.employeeCount == 1500


def test_product_record_pricing_enum_validation():
    # Arrange & Act
    record = ProductRecord(
        source=SourceMetadata(name="Futurepedia", url="https://futurepedia.io/tool/chatgpt"),
        content=ProductContent(
            startupName="OpenAI",
            productName="ChatGPT",
            productUrl="https://chat.openai.com",
            pricingModel=PricingModelEnum.FREEMIUM,
        )
    )

    # Assert
    assert record.content.pricingModel == PricingModelEnum.FREEMIUM


def test_product_record_invalid_pricing_fails():
    # Arrange, Act & Assert
    with pytest.raises(ValidationError):
        ProductContent(
            startupName="OpenAI",
            productName="ChatGPT",
            pricingModel="INVALID_PRICING",  # Not in ENUM
        )


def test_research_paper_record_valid():
    # Arrange & Act
    now = datetime.now(timezone.utc)
    record = ResearchPaperRecord(
        content=ResearchPaperContent(
            title="Attention Is All You Need",
            authors=["Ashish Vaswani", "Noam Shazeer"],
            paper_url="https://arxiv.org/abs/1706.03762",
            github_url="https://github.com/tensorflow/tensor2tensor",
            github_stars=15847,
            published_date=now,
        )
    )

    # Assert
    assert record.recordType == "RESEARCH_PAPER"
    assert record.content.github_stars == 15847
    assert len(record.content.authors) == 2

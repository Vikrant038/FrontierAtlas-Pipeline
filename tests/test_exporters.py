"""
Unit tests for Excel and Knowledge Graph exporters.
Follows AAA pattern per CODING_STANDARDS.md Pillar 7.
"""

import os
import tempfile
import openpyxl

from src.exporters.excel_exporter import ExcelExporter
from src.exporters.graph_builder import KnowledgeGraphBuilder
from src.schemas.entities import (
    PricingModelEnum,
    ProductContent,
    ProductRecord,
    SourceMetadata,
    StartupContent,
    StartupRecord,
)


def test_excel_exporter_creates_6_tabs():
    # Arrange
    exporter = ExcelExporter()
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_file = os.path.join(tmp_dir, "test_output.xlsx")
        
        startups = [
            StartupRecord(
                source=SourceMetadata(name="YC", url="https://yc.com"),
                content=StartupContent(entityName="Anthropic")
            )
        ]
        products = [
            ProductRecord(
                source=SourceMetadata(name="FP", url="https://fp.io"),
                content=ProductContent(
                    startupName="Anthropic",
                    productName="Claude",
                    pricingModel=PricingModelEnum.FREEMIUM
                )
            )
        ]

        # Act
        exporter.export(
            filepath=output_file,
            startups=startups,
            products=products,
        )

        # Assert
        assert os.path.exists(output_file)
        wb = openpyxl.load_workbook(output_file)
        expected_sheets = [
            "Startups", "Products", "Research_Papers", "Jobs_24h", "News_24h", "Entity Mapping Log"
        ]
        assert wb.sheetnames == expected_sheets


def test_knowledge_graph_builder_links_startup_to_product():
    # Arrange
    builder = KnowledgeGraphBuilder()
    startups = [
        StartupRecord(
            source=SourceMetadata(name="YC", url="https://yc.com"),
            content=StartupContent(entityName="Anthropic")
        )
    ]
    products = [
        ProductRecord(
            source=SourceMetadata(name="FP", url="https://fp.io"),
            content=ProductContent(
                startupName="Anthropic",
                productName="Claude",
                pricingModel=PricingModelEnum.FREEMIUM
            )
        )
    ]

    # Act
    graph = builder.build_graph(startups=startups, products=products)
    metrics = builder.get_summary_metrics()

    # Assert
    assert graph.has_node("Startup:Anthropic")
    assert graph.has_node("Product:Claude")
    assert graph.has_edge("Startup:Anthropic", "Product:Claude")
    assert metrics["total_nodes"] >= 2
    assert metrics["total_edges"] >= 1


def test_csv_exporter_exports_files():
    # Arrange
    from src.exporters.csv_exporter import CSVExporter
    with tempfile.TemporaryDirectory() as tmp_dir:
        exporter = CSVExporter(output_dir=tmp_dir)
        startups = [
            StartupRecord(
                source=SourceMetadata(name="YC", url="https://yc.com"),
                content=StartupContent(entityName="Anthropic")
            )
        ]

        # Act
        exported = exporter.export_all(startups=startups)

        # Assert
        assert "startups" in exported
        assert os.path.exists(exported["startups"])
        with open(exported["startups"], "r", encoding="utf-8") as f:
            content = f.read()
            assert "Anthropic" in content
            assert "schemaVersion" in content

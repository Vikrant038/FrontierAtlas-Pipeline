"""
Unit tests for GoogleSheetsExporter.
Strictly offline and hermetic using pytest-mock per CODING_STANDARDS.md Pillar 7.
"""

from unittest.mock import MagicMock
import pytest

from src.exporters.base import ENTITY_SPECS
from src.exporters.sheets_exporter import GoogleSheetsExporter, BATCH_SIZE
from src.schemas.entities import (
    PricingModelEnum,
    ProductContent,
    ProductRecord,
    SourceMetadata,
    StartupContent,
    StartupRecord,
)


def test_unconfigured_credentials_returns_none_and_warns(tmp_path):
    # Arrange
    missing_key = str(tmp_path / "nonexistent.json")
    exporter = GoogleSheetsExporter(service_account_path=missing_key)

    # Act
    result = exporter.export()

    # Assert
    assert result is None
    assert not exporter.is_configured()


def test_sheets_exporter_creates_6_tabs_and_batches(mocker, tmp_path):
    # Arrange
    fake_key = tmp_path / "fake_service_account.json"
    fake_key.write_text('{"type": "service_account"}')

    mock_client = MagicMock()
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.id = "mock-sheet-12345"
    mock_spreadsheet.url = "https://docs.google.com/spreadsheets/d/mock-sheet-12345"
    mock_spreadsheet.worksheets.return_value = []
    
    mock_created_ws = MagicMock()
    mock_spreadsheet.add_worksheet.return_value = mock_created_ws
    mock_client.create.return_value = mock_spreadsheet

    mocker.patch("gspread.service_account", return_value=mock_client)

    startups = [
        StartupRecord(
            source=SourceMetadata(name="YC", url="https://yc.com"),
            content=StartupContent(entityName="Anthropic"),
        )
    ]
    products = [
        ProductRecord(
            source=SourceMetadata(name="FP", url="https://fp.io"),
            content=ProductContent(
                startupName="Anthropic",
                productName="Claude",
                pricingModel=PricingModelEnum.FREEMIUM,
            ),
        )
    ]

    exporter = GoogleSheetsExporter(
        service_account_path=str(fake_key),
        spreadsheet_id="",
        evaluator_email="test@example.com",
    )

    # Act
    sheet_url = exporter.export(startups=startups, products=products)

    # Assert
    assert sheet_url == "https://docs.google.com/spreadsheets/d/mock-sheet-12345"
    assert mock_spreadsheet.add_worksheet.call_count == 6
    
    created_tab_names = [call.kwargs["title"] for call in mock_spreadsheet.add_worksheet.call_args_list]
    expected_tab_names = [spec[0] for spec in ENTITY_SPECS.values()]
    assert created_tab_names == expected_tab_names

    # Verify values_update was called for each tab
    assert mock_spreadsheet.values_update.call_count >= 6
    mock_spreadsheet.share.assert_called_once_with(
        "test@example.com",
        perm_type="user",
        role="reader",
        notify=False,
    )


def test_500_row_batch_chunking(tmp_path):
    # Arrange
    exporter = GoogleSheetsExporter(service_account_path=str(tmp_path / "fake.json"))
    mock_spreadsheet = MagicMock()

    # Create 1,050 sample rows
    all_rows = [["col1", "col2"]] + [[f"val_{i}", f"data_{i}"] for i in range(1049)]
    assert len(all_rows) == 1050

    # Act
    exporter._batch_update_values(
        spreadsheet=mock_spreadsheet,
        title="Startups",
        all_rows=all_rows,
    )

    # Assert
    # 1050 rows / 500 batch size = 3 chunks (500, 500, 50)
    assert mock_spreadsheet.values_update.call_count == 3
    calls = mock_spreadsheet.values_update.call_args_list

    # First chunk starts at row 1, length 500
    assert calls[0].args[0] == "'Startups'!A1"
    assert len(calls[0].kwargs["body"]["values"]) == 500

    # Second chunk starts at row 501, length 500
    assert calls[1].args[0] == "'Startups'!A501"
    assert len(calls[1].kwargs["body"]["values"]) == 500

    # Third chunk starts at row 1001, length 50
    assert calls[2].args[0] == "'Startups'!A1001"
    assert len(calls[2].kwargs["body"]["values"]) == 50


def test_idempotent_overwrite_clears_existing_sheet(tmp_path):
    # Arrange
    exporter = GoogleSheetsExporter(service_account_path=str(tmp_path / "fake.json"))
    mock_spreadsheet = MagicMock()
    mock_existing_ws = MagicMock()
    mock_existing_ws.title = "Startups"
    mock_spreadsheet.worksheets.return_value = [mock_existing_ws]

    # Act
    ws = exporter._prepare_worksheet(
        spreadsheet=mock_spreadsheet,
        title="Startups",
        num_rows=250,
        num_cols=7,
    )

    # Assert
    assert ws == mock_existing_ws
    mock_existing_ws.clear.assert_called_once()
    mock_existing_ws.resize.assert_called_once_with(rows=250, cols=10)
    mock_spreadsheet.add_worksheet.assert_not_called()


def test_evaluator_sharing_reminder_when_email_unset(tmp_path):
    # Arrange
    exporter = GoogleSheetsExporter(
        service_account_path=str(tmp_path / "fake.json"),
        evaluator_email="",
    )
    mock_spreadsheet = MagicMock()
    mock_spreadsheet.url = "https://docs.google.com/spreadsheets/d/123"

    # Act
    exporter._share_with_evaluator(mock_spreadsheet)

    # Assert
    mock_spreadsheet.share.assert_not_called()


def test_open_existing_spreadsheet_by_id(mocker, tmp_path):
    # Arrange
    fake_key = tmp_path / "fake.json"
    fake_key.write_text('{"type": "service_account"}')

    mock_client = MagicMock()
    mock_spreadsheet = MagicMock()
    mock_client.open_by_key.return_value = mock_spreadsheet

    exporter = GoogleSheetsExporter(
        service_account_path=str(fake_key),
        spreadsheet_id="custom-existing-id",
    )

    # Act
    spreadsheet = exporter.get_or_create_spreadsheet(mock_client)

    # Assert
    mock_client.open_by_key.assert_called_once_with("custom-existing-id")
    mock_client.create.assert_not_called()
    assert spreadsheet == mock_spreadsheet


def test_explicit_spreadsheet_id_failure_raises_no_stray_creation(mocker, tmp_path):
    # Arrange - strict mode: a configured ID that fails to open must raise, never
    # silently create a stray spreadsheet that masks the misconfiguration.
    fake_key = tmp_path / "fake.json"
    fake_key.write_text('{"type": "service_account"}')

    mock_client = MagicMock()
    mock_client.open_by_key.side_effect = Exception("Not found")
    exporter = GoogleSheetsExporter(
        service_account_path=str(fake_key),
        spreadsheet_id="bad-or-deleted-id",
    )

    # Act & Assert
    with pytest.raises(Exception, match="Not found"):
        exporter.get_or_create_spreadsheet(mock_client, title="Fallback Title")
    mock_client.create.assert_not_called()

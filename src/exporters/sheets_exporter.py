"""
Google Sheets exporter for FrontierAtlas AI Intelligence Pipeline.
Deliverable 1 implementation using gspread and Google Sheets API v4.
Provides service account authentication, 6-tab parity, 500-row batched updates,
idempotent tab management, and evaluator sharing.
"""

import os
from typing import Any, Dict, List, Optional
import gspread

from src.config import settings
from src.exporters.base import ENTITY_SPECS
from src.schemas.entities import (
    EntityResolutionLog,
    JobRecord,
    NewsRecord,
    ProductRecord,
    ResearchPaperRecord,
    StartupRecord,
)
from src.utils.logger import logger


BATCH_SIZE = 500
DEFAULT_SPREADSHEET_TITLE = "FrontierAtlas AI Intelligence"


class GoogleSheetsExporter:
    """Exports pipeline entities to a 6-tab Google Spreadsheet via gspread."""

    def __init__(
        self,
        service_account_path: Optional[str] = None,
        spreadsheet_id: Optional[str] = None,
        evaluator_email: Optional[str] = None,
    ):
        self.service_account_path = service_account_path or settings.effective_service_account_path
        self.spreadsheet_id = spreadsheet_id or settings.google_sheets_spreadsheet_id
        self.evaluator_email = evaluator_email or settings.evaluator_email
        self._client: Optional[gspread.Client] = None

    def is_configured(self) -> bool:
        """Check if service account file exists and credentials path is configured."""
        if not self.service_account_path:
            return False
        return os.path.exists(self.service_account_path)

    def authenticate(self) -> Optional[gspread.Client]:
        """Authenticate with Google Sheets API via service account credentials."""
        if not self.is_configured():
            logger.warning(
                "Google Sheets service account not configured. "
                f"Path: {self.service_account_path}. "
                "Set GOOGLE_SERVICE_ACCOUNT_PATH in .env to enable Google Sheets export."
            )
            return None
        try:
            self._client = gspread.service_account(filename=self.service_account_path)
            logger.info("Successfully authenticated with Google Sheets API via service account.")
            return self._client
        except Exception as exc:
            logger.error(f"Failed to authenticate with Google Sheets service account: {exc}")
            return None

    def get_or_create_spreadsheet(
        self,
        client: gspread.Client,
        title: str = DEFAULT_SPREADSHEET_TITLE,
    ) -> gspread.Spreadsheet:
        """Open existing spreadsheet by ID if configured, or create a new one."""
        if self.spreadsheet_id:
            try:
                spreadsheet = client.open_by_key(self.spreadsheet_id)
                logger.info(f"Opened existing Google Spreadsheet by ID: {self.spreadsheet_id}")
                return spreadsheet
            except Exception as exc:
                logger.warning(
                    f"Could not open spreadsheet ID {self.spreadsheet_id} ({exc}). "
                    f"Falling back to creating a new spreadsheet named '{title}'."
                )
        spreadsheet = client.create(title)
        logger.info(f"Created new Google Spreadsheet: '{title}' (ID: {spreadsheet.id})")
        return spreadsheet

    def _prepare_worksheet(
        self,
        spreadsheet: gspread.Spreadsheet,
        title: str,
        num_rows: int,
        num_cols: int,
    ) -> gspread.Worksheet:
        """Clear existing worksheet or create new worksheet with collision handling."""
        existing = {ws.title: ws for ws in spreadsheet.worksheets()}
        if title in existing:
            ws = existing[title]
            ws.clear()
            ws.resize(rows=max(num_rows, 100), cols=max(num_cols, 10))
            logger.debug(f"Cleared and resized existing worksheet '{title}'")
            return ws

        try:
            ws = spreadsheet.add_worksheet(
                title=title,
                rows=max(num_rows, 100),
                cols=max(num_cols, 10),
            )
            logger.debug(f"Created new worksheet '{title}'")
            return ws
        except Exception:
            # Handle potential concurrent creation collision
            ws = spreadsheet.worksheet(title)
            ws.clear()
            ws.resize(rows=max(num_rows, 100), cols=max(num_cols, 10))
            return ws

    def _batch_update_values(
        self,
        spreadsheet: gspread.Spreadsheet,
        title: str,
        all_rows: List[List[Any]],
    ) -> None:
        """Write rows to worksheet in chunks of BATCH_SIZE using values_update."""
        for i in range(0, len(all_rows), BATCH_SIZE):
            chunk = all_rows[i : i + BATCH_SIZE]
            start_row = i + 1
            cell_range = f"'{title}'!A{start_row}"
            spreadsheet.values_update(
                cell_range,
                params={"valueInputOption": "USER_ENTERED"},
                body={"values": chunk},
            )
        logger.info(f"Uploaded {len(all_rows) - 1} records to tab '{title}' in {len(all_rows) // BATCH_SIZE + 1} batch(es).")

    def _cleanup_default_sheet(self, spreadsheet: gspread.Spreadsheet) -> None:
        """Remove default 'Sheet1' if additional worksheets exist."""
        try:
            sheets = spreadsheet.worksheets()
            if len(sheets) > 1:
                default_sheet = spreadsheet.worksheet("Sheet1")
                spreadsheet.del_worksheet(default_sheet)
                logger.debug("Removed default 'Sheet1'.")
        except Exception:
            pass

    def _share_with_evaluator(self, spreadsheet: gspread.Spreadsheet) -> None:
        """Programmatically share spreadsheet with evaluator email or print reminder."""
        if self.evaluator_email:
            try:
                spreadsheet.share(
                    self.evaluator_email,
                    perm_type="user",
                    role="viewer",
                    notify=False,
                )
                logger.info(f"Shared spreadsheet with evaluator '{self.evaluator_email}' as viewer.")
            except Exception as exc:
                logger.warning(f"Could not automatically share spreadsheet with {self.evaluator_email}: {exc}")
        else:
            logger.info(
                f"EVALUATOR_EMAIL not set. Manual sharing reminder: "
                f"Share viewer access to {spreadsheet.url} with evaluators."
            )

    def export(
        self,
        startups: Optional[List[StartupRecord]] = None,
        products: Optional[List[ProductRecord]] = None,
        papers: Optional[List[ResearchPaperRecord]] = None,
        jobs: Optional[List[JobRecord]] = None,
        news: Optional[List[NewsRecord]] = None,
        logs: Optional[List[EntityResolutionLog]] = None,
        title: str = DEFAULT_SPREADSHEET_TITLE,
    ) -> Optional[str]:
        """
        Export all 6 entity datasets into the Google Spreadsheet.
        Returns spreadsheet URL if successful, or None if skipped/unconfigured.
        """
        client = self.authenticate()
        if not client:
            return None

        try:
            spreadsheet = self.get_or_create_spreadsheet(client, title=title)
            datasets: Dict[str, Optional[List]] = {
                "startups": startups,
                "products": products,
                "papers": papers,
                "jobs": jobs,
                "news": news,
                "logs": logs,
            }

            for key, (tab_title, _, headers) in ENTITY_SPECS.items():
                records = datasets.get(key) or []
                record_rows = [r.to_row() if hasattr(r, "to_row") else r for r in records]
                all_rows = [headers] + record_rows

                self._prepare_worksheet(
                    spreadsheet=spreadsheet,
                    title=tab_title,
                    num_rows=len(all_rows),
                    num_cols=len(headers),
                )
                self._batch_update_values(
                    spreadsheet=spreadsheet,
                    title=tab_title,
                    all_rows=all_rows,
                )

            self._cleanup_default_sheet(spreadsheet)
            self._share_with_evaluator(spreadsheet)

            logger.info(f"Successfully exported 6 tabs to Google Sheets: {spreadsheet.url}")
            return spreadsheet.url
        except Exception as exc:
            logger.error(f"Google Sheets export failed: {exc}")
            return None

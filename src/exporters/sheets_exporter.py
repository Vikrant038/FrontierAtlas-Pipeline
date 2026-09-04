"""
Google Sheets exporter for FrontierAtlas AI Intelligence Pipeline.
Deliverable 1 implementation using gspread and Google Sheets API v4.
Provides service account authentication, 6-tab parity, 500-row batched updates,
idempotent tab management, and evaluator sharing.
"""

import os
import time
from typing import Any, Dict, List, Optional, Tuple
import gspread
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.config import settings
from src.exporters.base import ENTITY_SPECS
from src.utils.date_normalizer import parse_retry_after
from src.schemas.entities import (
    EntityResolutionLog,
    JobRecord,
    NewsRecord,
    ProductRecord,
    ResearchPaperRecord,
    StartupRecord,
)
from src.utils.logger import logger


BATCH_SIZE = settings.sheets_batch_size  # configurable for quota tuning at scale
DEFAULT_SPREADSHEET_TITLE = "FrontierAtlas AI Intelligence"


def _dims(num_rows: int, num_cols: int) -> Tuple[int, int]:
    """Minimum worksheet dimensions (100 rows x 10 cols) for readable auto-resize."""
    return max(num_rows, 100), max(num_cols, 10)


def _sheets_before_sleep(retry_state) -> None:
    """Honor the Retry-After header (seconds or RFC 7231 HTTP-date) before a Sheets retry."""
    exc = retry_state.outcome.exception()
    if exc is None:
        return
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) if resp is not None else None
    retry_after = headers.get("Retry-After") if headers else None
    wait_time = parse_retry_after(retry_after)
    if wait_time is not None and wait_time > 0:
        wait = min(wait_time, 300.0)
        logger.warning(f"Sleeping {wait:.0f}s per Sheets Retry-After header before retry.")
        time.sleep(wait)


def _is_transient_sheets_error(exc: BaseException) -> bool:
    """Check if an exception represents a transient Google API rate limit (429) or server error (500/503)."""
    msg = str(exc).lower()
    if "storage quota" in msg:
        return False
    if isinstance(exc, gspread.exceptions.APIError):
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        if status in (429, 500, 502, 503, 504):
            return True
        if any(term in msg for term in ["429", "rate limit", "quota exceeded", "500", "502", "503", "unavailable"]):
            return True
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    return False


_SHEETS_RETRY = retry(
    wait=wait_exponential_jitter(initial=1.0, max=20.0, exp_base=2, jitter=2.0),
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_is_transient_sheets_error),
    before_sleep=_sheets_before_sleep,
    reraise=True,
)


class GoogleSheetsExporter:
    """Exports pipeline entities to a 6-tab Google Spreadsheet via gspread."""

    def __init__(
        self,
        service_account_path: Optional[str] = None,
        spreadsheet_id: Optional[str] = None,
        evaluator_email: Optional[str] = None,
    ):
        self.service_account_path = (
            service_account_path if service_account_path is not None else settings.effective_service_account_path
        )
        self.spreadsheet_id = (
            spreadsheet_id if spreadsheet_id is not None else settings.google_sheets_spreadsheet_id
        )
        self.evaluator_email = (
            evaluator_email if evaluator_email is not None else settings.evaluator_email
        )
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

    @property
    def service_account_email(self) -> str:
        """Extract client_email from the service account JSON if available."""
        if self.service_account_path and os.path.exists(self.service_account_path):
            try:
                import json
                with open(self.service_account_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("client_email", "your-service-account@...iam.gserviceaccount.com")
            except Exception as exc:
                logger.debug(f"Could not read service account email from {self.service_account_path}: {exc}")
        return "your-service-account@...iam.gserviceaccount.com"

    @_SHEETS_RETRY
    def get_or_create_spreadsheet(
        self,
        client: gspread.Client,
        title: str = DEFAULT_SPREADSHEET_TITLE,
    ) -> gspread.Spreadsheet:
        """
        Open the spreadsheet when an explicit GOOGLE_SHEETS_SPREADSHEET_ID is configured,
        or create a fresh one when unset. Strict mode with explicit ID: any open failure
        raises (no silent stray-spreadsheet creation masking a misconfigured ID).
        """
        if self.spreadsheet_id:
            spreadsheet = client.open_by_key(self.spreadsheet_id)
            logger.info(f"Opened existing Google Spreadsheet by ID: {self.spreadsheet_id}")
            return spreadsheet
        try:
            spreadsheet = client.create(title)
            logger.info(f"Created new Google Spreadsheet: '{title}' (ID: {spreadsheet.id})")
            return spreadsheet
        except Exception as exc:
            err_msg = str(exc)
            if "storage quota" in err_msg.lower() or "403" in err_msg:
                sa_email = self.service_account_email
                logger.error(
                    "Google Drive storage quota error: Google Cloud service accounts have 0 MB personal Drive storage by default. "
                    f"Resolution: Create a blank sheet in your Google Drive, share it with '{sa_email}' as Editor, "
                    "and set GOOGLE_SHEETS_SPREADSHEET_ID in your .env file."
                )
            raise

    @_SHEETS_RETRY
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
            rows, cols = _dims(num_rows, num_cols)
            ws.resize(rows=rows, cols=cols)
            logger.debug(f"Cleared and resized existing worksheet '{title}'")
            return ws

        rows, cols = _dims(num_rows, num_cols)
        try:
            ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
            logger.debug(f"Created new worksheet '{title}'")
            return ws
        except Exception:
            # Handle potential concurrent creation collision
            ws = spreadsheet.worksheet(title)
            ws.clear()
            ws.resize(rows=rows, cols=cols)
            return ws

    @_SHEETS_RETRY
    def _execute_values_update(
        self,
        spreadsheet: gspread.Spreadsheet,
        cell_range: str,
        values: List[List[Any]],
    ) -> None:
        """Execute values_update with exponential backoff on transient errors."""
        spreadsheet.values_update(
            cell_range,
            params={"valueInputOption": "USER_ENTERED"},
            body={"values": values},
        )

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
            self._execute_values_update(
                spreadsheet=spreadsheet,
                cell_range=cell_range,
                values=chunk,
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
        except Exception as exc:
            # Best-effort cleanup: 'Sheet1' may not exist (pre-created spreadsheet).
            logger.debug(f"Default sheet cleanup skipped: {exc}")

    def _share_with_evaluator(self, spreadsheet: gspread.Spreadsheet) -> None:
        """Programmatically share spreadsheet with evaluator email or print reminder."""
        if self.evaluator_email:
            try:
                spreadsheet.share(
                    self.evaluator_email,
                    perm_type="user",
                    role="reader",
                    notify=False,
                )
                logger.info(f"Shared spreadsheet with evaluator '{self.evaluator_email}' as reader.")
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

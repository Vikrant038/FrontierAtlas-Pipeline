import csv
import os
from typing import Dict, List, Optional

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


class CSVExporter:
    """Exports all 6 entity types into standalone CSV files using ENTITY_SPECS."""

    def __init__(self, output_dir: str = "exports"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def _write_csv(self, filename: str, headers: List[str], records: Optional[List]) -> str:
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in (records or []):
                writer.writerow(r.to_row() if hasattr(r, "to_row") else r)
        logger.info(f"Exported {len(records or [])} records to {filepath}")
        return filepath

    def export_startups(self, records: List[StartupRecord], filename: str = "startups.csv") -> str:
        return self._write_csv(filename, ENTITY_SPECS["startups"][2], records)

    def export_products(self, records: List[ProductRecord], filename: str = "products.csv") -> str:
        return self._write_csv(filename, ENTITY_SPECS["products"][2], records)

    def export_papers(self, records: List[ResearchPaperRecord], filename: str = "research_papers.csv") -> str:
        return self._write_csv(filename, ENTITY_SPECS["papers"][2], records)

    def export_jobs(self, records: List[JobRecord], filename: str = "jobs.csv") -> str:
        return self._write_csv(filename, ENTITY_SPECS["jobs"][2], records)

    def export_news(self, records: List[NewsRecord], filename: str = "news.csv") -> str:
        return self._write_csv(filename, ENTITY_SPECS["news"][2], records)

    def export_logs(self, records: List[EntityResolutionLog], filename: str = "entity_mapping_log.csv") -> str:
        filepath = self._write_csv(filename, ENTITY_SPECS["logs"][2], records)
        if filename == "entity_mapping_log.csv":
            self._write_csv("entity_resolution_logs.csv", ENTITY_SPECS["logs"][2], records)
        return filepath

    def export_all(self, **datasets) -> Dict[str, str]:
        res = {
            key: self._write_csv(filename, headers, datasets.get(key))
            for key, (_, filename, headers) in ENTITY_SPECS.items()
        }
        if "logs" in datasets and datasets["logs"]:
            self._write_csv("entity_resolution_logs.csv", ENTITY_SPECS["logs"][2], datasets["logs"])
        return res

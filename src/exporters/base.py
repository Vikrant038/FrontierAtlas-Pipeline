"""Shared export specifications and mapping definitions."""

from typing import Any, Dict, List, Tuple


def to_str(val: Any) -> str:
    """Safely extract string value from enum or raw value."""
    if val is None:
        return ""
    return str(val.value) if hasattr(val, "value") else str(val)


# Tuple format: (Tab Title, CSV Filename, Column Headers)
ENTITY_SPECS: Dict[str, Tuple[str, str, List[str]]] = {
    "startups": ("Startups", "startups.csv", ["Canonical Entity Name", "Employee Count", "Source Name", "Source URL", "Collected At"]),
    "products": ("Products", "products.csv", ["Product Name", "Startup Name", "Pricing Model", "Product URL", "Collected At"]),
    "papers": ("Research_Papers", "research_papers.csv", ["Title", "Authors", "Paper URL", "GitHub Repo", "GitHub Stars", "Published Date"]),
    "jobs": ("Jobs_24h", "jobs.csv", ["Job Title", "Company", "Role Family", "Remote", "Source Name", "Source URL", "Posting Date"]),
    "news": ("News_24h", "news.csv", ["Headline", "Source", "Source URL", "Published Date", "Summary"]),
    "logs": ("Entity_Resolution_Log", "entity_resolution_logs.csv", ["Raw Name", "Canonical Name", "Entity Type", "Match Method", "Confidence Score", "Source URL", "Timestamp"]),
}
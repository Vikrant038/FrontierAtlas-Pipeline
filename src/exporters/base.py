"""Shared export specifications and mapping definitions."""

from typing import Any, Dict, List, Tuple


def to_str(val: Any) -> str:
    """Safely extract string value from enum or raw value."""
    if val is None:
        return ""
    return str(val.value) if hasattr(val, "value") else str(val)


# Tuple format: (Tab Title, CSV Filename, Column Headers)
ENTITY_SPECS: Dict[str, Tuple[str, str, List[str]]] = {
    "startups": (
        "Startups",
        "startups.csv",
        ["schemaVersion", "recordType", "source.name", "source.url", "content.entityName", "content.data.employeeCount", "collectedAt"],
    ),
    "products": (
        "Products",
        "products.csv",
        ["schemaVersion", "recordType", "source.name", "source.url", "content.productName", "content.startupName", "content.pricingModel", "content.productUrl", "collectedAt"],
    ),
    "papers": (
        "Research_Papers",
        "research_papers.csv",
        ["schemaVersion", "recordType", "content.title", "content.authors", "content.paper_url", "content.github_url", "content.github_stars", "content.published_date"],
    ),
    "jobs": (
        "Jobs_24h",
        "jobs.csv",
        ["schemaVersion", "recordType", "source.name", "source.url", "content.company", "content.title", "content.date", "content.is_remote", "content.role_family", "collectedAt"],
    ),
    "news": (
        "News_24h",
        "news.csv",
        ["schemaVersion", "recordType", "source.name", "source.url", "content.title", "content.published_date", "content.summary", "collectedAt"],
    ),
    "logs": (
        "Entity Mapping Log",
        "entity_mapping_log.csv",
        ["rawName", "canonicalName", "entityType", "matchMethod", "confidenceScore", "sourceUrl", "resolvedAt"],
    ),
}
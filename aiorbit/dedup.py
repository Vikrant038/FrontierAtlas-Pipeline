"""
Canonical deduplication and subcategory saturation capping for AIOrbit.
Enforces Guideline §7 (merge multi-source duplicates) and §8 (cap identical clones).
Pure functions, strictly typed with Pydantic v2, independently unit-testable.
"""

import re
from typing import Dict, List, Optional
from aiorbit.schema import MCPEntry

# Functional cluster keywords for Guideline §8 saturation capping
CLUSTER_PATTERNS = {
    "postgresql": [r"\bpostgres\b", r"\bpostgresql\b", r"\bpg_"],
    "sqlite": [r"\bsqlite\b", r"\bsqlite3\b"],
    "mysql": [r"\bmysql\b", r"\bmariadb\b"],
    "mongodb": [r"\bmongo\b", r"\bmongodb\b"],
    "redis": [r"\bredis\b"],
    "docker": [r"\bdocker\b", r"\bcontainer\b"],
    "kubernetes": [r"\bkubernetes\b", r"\bk8s\b"],
    "github": [r"\bgithub\b"],
    "gitlab": [r"\bgitlab\b"],
    "slack": [r"\bslack\b"],
    "discord": [r"\bdiscord\b"],
    "notion": [r"\bnotion\b"],
    "jira": [r"\bjira\b"],
    "linear": [r"\blinear\b"],
    "filesystem": [r"\bfilesystem\b", r"\bfile-system\b", r"\blocalfolder\b"],
    "weather": [r"\bweather\b", r"\bmeteorology\b"],
    "brave_search": [r"\bbrave\b.*search", r"\bbrave-search\b"],
    "google_drive": [r"\bgoogle\b.*drive", r"\bgdrive\b"],
    "browser_playwright": [r"\bplaywright\b"],
    "browser_puppeteer": [r"\bpuppeteer\b"],
}


def clean_canonical_key(entry: MCPEntry) -> str:
    """Derives unique canonical deduplication key (Guideline §7)."""
    if entry.github_owner_repo:
        return f"gh:{entry.github_owner_repo.lower().strip()}"
    if entry.official_url:
        clean_url = entry.official_url.strip().lower().rstrip("/")
        # Remove http(s)://
        clean_url = re.sub(r"^https?://(www\.)?", "", clean_url)
        return f"url:{clean_url}"
    # Fallback to normalized product name
    clean_name = re.sub(r"[^a-z0-9]", "", entry.name.lower())
    return f"name:{clean_name}"


def merge_two_entries(primary: MCPEntry, secondary: MCPEntry) -> MCPEntry:
    """
    Merges duplicate records pointing to the same canonical project.
    Prefers the higher-scoring / higher-star entry as the base,
    filling missing attributes from the other.
    """
    p_stars = primary.github_stars or 0
    s_stars = secondary.github_stars or 0
    p_score = primary.overall_score or 0.0
    s_score = secondary.overall_score or 0.0

    if (s_score > p_score) or (s_score == p_score and s_stars > p_stars):
        base, other = secondary.model_copy(deep=True), primary
    else:
        base, other = primary.model_copy(deep=True), secondary

    # Fill in missing fields
    if not base.description and other.description:
        base.description = other.description
    if not base.official_url and other.official_url:
        base.official_url = other.official_url
    if not base.docs_url and other.docs_url:
        base.docs_url = other.docs_url
    if not base.company_creator and other.company_creator:
        base.company_creator = other.company_creator
    if not base.category and other.category:
        base.category = other.category
    if not base.license and other.license:
        base.license = other.license
    if not base.pricing and other.pricing:
        base.pricing = other.pricing

    # Merge discovery source
    sources = set(filter(None, [base.discovery_source, other.discovery_source]))
    base.discovery_source = ", ".join(sorted(sources))

    return base


def deduplicate_canonical_entries(entries: List[MCPEntry]) -> List[MCPEntry]:
    """
    Groups entries by canonical repository or URL and collapses duplicates into one.
    """
    grouped: Dict[str, MCPEntry] = {}
    for entry in entries:
        key = clean_canonical_key(entry)
        if key in grouped:
            grouped[key] = merge_two_entries(grouped[key], entry)
        else:
            grouped[key] = entry.model_copy(deep=True)
    return list(grouped.values())


def identify_functional_cluster(entry: MCPEntry) -> Optional[str]:
    """
    Detects if an entry belongs to a specific saturated utility niche.
    """
    text = f"{entry.name} {entry.description or ''} {entry.subcategory or ''}".lower()
    for cluster_id, patterns in CLUSTER_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text):
                return cluster_id
    return None


def apply_saturation_capping(
    entries: List[MCPEntry],
    max_per_cluster: int = 4
) -> List[MCPEntry]:
    """
    Applies Guideline §8: Prevents over-representation of identical clones
    (e.g., max 4 Postgres servers, max 4 SQLite servers).
    Keeps the highest scoring entries in each cluster.
    """
    # Sort by overall score descending, tie break by stars
    sorted_entries = sorted(
        entries,
        key=lambda e: (e.overall_score or 0.0, e.github_stars or 0),
        reverse=True
    )

    cluster_counts: Dict[str, int] = {}
    survivors: List[MCPEntry] = []

    for entry in sorted_entries:
        # Never cap MCP Clients (they are rare and high priority)
        if entry.mcp_type == "CLIENT":
            survivors.append(entry)
            continue

        cluster = identify_functional_cluster(entry)
        if not cluster:
            survivors.append(entry)
            continue

        count = cluster_counts.get(cluster, 0)
        if count < max_per_cluster:
            cluster_counts[cluster] = count + 1
            survivors.append(entry)
        else:
            # Saturated clone dropped
            pass

    return survivors

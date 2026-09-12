"""
AIOrbit Multi-Source Discovery Engine.
Harvests high-quality-density MCP inventory across 4 sources:
  1. Official MCP Registry (registry.modelcontextprotocol.io)
  2. GitHub Topic Search (topic:mcp-server & topic:modelcontextprotocol sorted by stars)
  3. Glama.ai Directory (glama.ai/mcp/servers)
  4. Smithery.ai Directory (smithery.ai)
Merges canonical duplicates with existing Creati.ai candidates.
Emits aiorbit/exports_aiorbit/multisource_candidates.jsonl.
"""

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Set
import httpx
from curl_cffi import requests
from bs4 import BeautifulSoup
from rich.console import Console

from aiorbit.config import settings
from aiorbit.schema import MCPEntry
from aiorbit.dedup import clean_canonical_key, merge_two_entries

EXPORTS_DIR = Path("aiorbit/exports_aiorbit")
MULTISOURCE_PATH = EXPORTS_DIR / "multisource_candidates.jsonl"
EXISTING_CANDIDATES_PATH = EXPORTS_DIR / "candidates.jsonl"


def fetch_official_registry(limit_pages: int = 15) -> List[MCPEntry]:
    """Harvests curated servers from the Official MCP Registry API."""
    console = Console()
    console.print("[cyan]🔍 Fetching Official MCP Registry (registry.modelcontextprotocol.io)...[/cyan]")
    entries: List[MCPEntry] = []
    cursor: Optional[str] = None

    client = httpx.Client(timeout=30.0)
    for page in range(limit_pages):
        url = "https://registry.modelcontextprotocol.io/v0/servers?limit=50"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            resp = client.get(url)
            if resp.status_code != 200:
                console.print(f"[yellow]Registry HTTP {resp.status_code} on page {page+1}[/yellow]")
                break
            data = resp.json()
            servers = data.get("servers", [])
            for item in servers:
                s = item.get("server", {})
                name = s.get("title") or s.get("name") or "Unnamed MCP"
                desc = s.get("description") or ""
                repo_info = s.get("repository") or {}
                repo_url = repo_info.get("url") if isinstance(repo_info, dict) else None
                website_url = s.get("websiteUrl")
                
                # Extract owner/repo if repo_url is GitHub
                owner_repo = None
                if repo_url and "github.com" in repo_url:
                    match = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git|/)?$", repo_url)
                    if match:
                        owner_repo = match.group(1).lower()

                # Remotes/transports
                remotes = s.get("remotes") or []
                transport = "http" if remotes else "stdio"

                entry = MCPEntry(
                    name=name,
                    mcp_type="SERVER",
                    listing_url=website_url or repo_url or f"https://registry.modelcontextprotocol.io/server/{s.get('name')}",
                    official_url=website_url,
                    github_url=repo_url if (repo_url and "github.com" in repo_url) else None,
                    github_owner_repo=owner_repo,
                    description=desc,
                    transport=transport,
                    discovery_source="Official Registry",
                )
                entries.append(entry)

            cursor = data.get("metadata", {}).get("nextCursor")
            console.print(f"  Official Registry Page {page+1}: +{len(servers)} servers (cursor: {cursor})")
            if not cursor or not servers:
                break
            time.sleep(1.0)
        except Exception as e:
            console.print(f"[red]Registry fetch error on page {page+1}: {e}[/red]")
            break

    client.close()
    console.print(f"[green]✅ Official Registry harvested: {len(entries)} servers[/green]")
    return entries


def fetch_github_topics(max_pages_per_topic: int = 5) -> List[MCPEntry]:
    """Harvests top-starred MCP repositories directly from GitHub Topic Search."""
    console = Console()
    console.print("[cyan]🔍 Fetching GitHub Topic Search (mcp-server, modelcontextprotocol)...[/cyan]")
    entries: List[MCPEntry] = []
    token = settings.github_token
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"

    client = httpx.Client(timeout=15.0, headers=headers)
    topics = ["topic:mcp-server", "topic:modelcontextprotocol"]

    for query in topics:
        for page in range(1, max_pages_per_topic + 1):
            url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=100&page={page}"
            try:
                resp = client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("items", [])
                    for repo in items:
                        name = repo.get("name") or "Unnamed"
                        full_name = repo.get("full_name") or ""
                        stars = repo.get("stargazers_count", 0)
                        pushed_at = repo.get("pushed_at")
                        archived = repo.get("archived", False)
                        desc = repo.get("description") or ""
                        html_url = repo.get("html_url")
                        homepage = repo.get("homepage")
                        lic_info = repo.get("license") or {}
                        lic_name = lic_info.get("spdx_id") or lic_info.get("name")

                        mcp_type = "CLIENT" if "client" in name.lower() or "client" in desc.lower() else "SERVER"

                        entry = MCPEntry(
                            name=name,
                            mcp_type=mcp_type,
                            listing_url=html_url,
                            official_url=homepage or html_url,
                            github_url=html_url,
                            github_owner_repo=full_name.lower(),
                            github_stars=stars,
                            github_pushed_at=pushed_at,
                            github_archived=archived,
                            license=lic_name,
                            description=desc,
                            discovery_source="GitHub Topic Search",
                        )
                        entries.append(entry)
                    console.print(f"  GitHub Search '{query}' Page {page}: +{len(items)} repos")
                elif resp.status_code == 403:
                    console.print(f"[yellow]GitHub rate limited: {resp.text[:100]}[/yellow]")
                    break
                else:
                    console.print(f"[yellow]GitHub status {resp.status_code} on {query} page {page}[/yellow]")
                    break
                time.sleep(1.0)
            except Exception as e:
                console.print(f"[red]GitHub search error: {e}[/red]")
                break

    client.close()
    console.print(f"[green]✅ GitHub Topic Search harvested: {len(entries)} repos[/green]")
    return entries


def fetch_glama_and_smithery() -> List[MCPEntry]:
    """Harvests server listings from Glama.ai and Smithery.ai directories."""
    console = Console()
    console.print("[cyan]🔍 Fetching Glama.ai and Smithery.ai directory listings...[/cyan]")
    entries: List[MCPEntry] = []

    # 1. Glama.ai
    try:
        r = requests.get("https://glama.ai/mcp/servers", impersonate="chrome124", timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            seen_glama: Set[str] = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/mcp/servers/" in href:
                    slug = href.split("/mcp/servers/")[-1].strip("/")
                    parts = slug.split("/")
                    if len(parts) >= 2 and slug not in seen_glama:
                        seen_glama.add(slug)
                        owner_repo = f"{parts[0]}/{parts[1]}".lower()
                        name = a.get_text(strip=True) or parts[1]
                        entries.append(
                            MCPEntry(
                                name=name,
                                mcp_type="SERVER",
                                listing_url=f"https://glama.ai{href}",
                                github_url=f"https://github.com/{owner_repo}",
                                github_owner_repo=owner_repo,
                                discovery_source="Glama.ai",
                            )
                        )
            console.print(f"  Glama.ai: harvested {len(seen_glama)} server links")
    except Exception as e:
        console.print(f"[yellow]Glama fetch notice: {e}[/yellow]")

    time.sleep(1.0)

    # 2. Smithery.ai
    try:
        r2 = requests.get("https://smithery.ai/", impersonate="chrome124", timeout=15)
        if r2.status_code == 200:
            soup2 = BeautifulSoup(r2.text, "html.parser")
            seen_smithery: Set[str] = set()
            for a in soup2.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/servers/") or href.startswith("/@"):
                    slug = href.replace("/servers/", "").replace("/@", "").strip("/")
                    parts = slug.split("/")
                    if len(parts) >= 2 and slug not in seen_smithery:
                        seen_smithery.add(slug)
                        owner_repo = f"{parts[0]}/{parts[1]}".lower()
                        name = a.get_text(strip=True) or parts[1]
                        entries.append(
                            MCPEntry(
                                name=name,
                                mcp_type="SERVER",
                                listing_url=f"https://smithery.ai{href}",
                                github_url=f"https://github.com/{owner_repo}",
                                github_owner_repo=owner_repo,
                                discovery_source="Smithery.ai",
                            )
                        )
            console.print(f"  Smithery.ai: harvested {len(seen_smithery)} server links")
    except Exception as e:
        console.print(f"[yellow]Smithery fetch notice: {e}[/yellow]")

    console.print(f"[green]✅ Glama & Smithery harvested: {len(entries)} directory entries[/green]")
    return entries


def run_multisource_discovery() -> None:
    console = Console()
    console.print("\n[bold cyan]================================================================[/bold cyan]")
    console.print("[bold green]🌟 AIOrbit Stage-2: Multi-Source Parallel Expansion Discovery[/bold green]")
    console.print("[bold cyan]================================================================[/bold cyan]\n")

    # 1. Load existing Creati.ai candidates if present
    existing_entries: List[MCPEntry] = []
    if EXISTING_CANDIDATES_PATH.exists():
        with open(EXISTING_CANDIDATES_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        existing_entries.append(MCPEntry.model_validate(json.loads(line)))
                    except Exception:
                        pass
        console.print(f"Loaded [cyan]{len(existing_entries)}[/cyan] existing Creati.ai candidates.")

    # 2. Harvest new sources
    official_servers = fetch_official_registry(limit_pages=15)
    github_repos = fetch_github_topics(max_pages_per_topic=5)
    directory_entries = fetch_glama_and_smithery()

    all_harvested = existing_entries + official_servers + github_repos + directory_entries
    console.print(f"Total raw candidates across all sources: [bold]{len(all_harvested)}[/bold]")

    # 3. Canonical Deduplication & Merging
    grouped: Dict[str, MCPEntry] = {}
    for entry in all_harvested:
        key = clean_canonical_key(entry)
        if key in grouped:
            grouped[key] = merge_two_entries(grouped[key], entry)
        else:
            grouped[key] = entry.model_copy(deep=True)

    unique_candidates = list(grouped.values())
    console.print(f"Merged into [bold green]{len(unique_candidates)}[/bold green] unique canonical projects.")

    # 4. Save to multisource_candidates.jsonl
    with open(MULTISOURCE_PATH, "w", encoding="utf-8") as f:
        for entry in unique_candidates:
            f.write(entry.model_dump_json() + "\n")

    console.print(f"[bold green]🚀 Successfully wrote {len(unique_candidates)} multisource candidates to {MULTISOURCE_PATH}![/bold green]\n")


if __name__ == "__main__":
    run_multisource_discovery()

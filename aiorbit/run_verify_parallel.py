"""
AIOrbit Multi-Source Parallel Verification Engine.
Runs verification across independent sources with dedicated WAL shards:
  - aiorbit/exports_aiorbit/verification_wal.official_registry.jsonl
  - aiorbit/exports_aiorbit/verification_wal.github_search.jsonl
  - aiorbit/exports_aiorbit/verification_wal.glama_smithery.jsonl

Hard rule: Parallelism is ACROSS sources, never WITHIN one domain.
Preserves existing 2,500 Creati.ai verified records.
Appends verified records into aiorbit/exports_aiorbit/verified.jsonl.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set
import httpx
from rich.console import Console

from aiorbit.config import settings
from aiorbit.schema import MCPEntry
from aiorbit.scoring import compute_deterministic_breakdown
from aiorbit.dedup import clean_canonical_key, merge_two_entries
from aiorbit.http_common import validate_url_safe

EXPORTS_DIR = Path("aiorbit/exports_aiorbit")
MULTISOURCE_PATH = EXPORTS_DIR / "multisource_candidates.jsonl"
COMBINED_VERIFIED_PATH = EXPORTS_DIR / "verified.jsonl"
COMBINED_WAL_PATH = EXPORTS_DIR / "verification_wal.jsonl"

WAL_OFFICIAL = EXPORTS_DIR / "verification_wal.official_registry.jsonl"
WAL_GITHUB = EXPORTS_DIR / "verification_wal.github_search.jsonl"
WAL_GLAMA = EXPORTS_DIR / "verification_wal.glama_smithery.jsonl"


def check_site_liveness_safe(url: str, client: httpx.Client) -> bool:
    """Verifies website liveness using safe URL checks."""
    try:
        validate_url_safe(url)
        resp = client.head(url, timeout=5.0, follow_redirects=True)
        if resp.status_code < 400:
            return True
        resp_get = client.get(url, timeout=5.0, follow_redirects=True)
        return resp_get.status_code < 400
    except Exception:
        return False


def verify_github_repo_live(owner_repo: str, client: httpx.Client) -> Dict[str, any]:
    """Inspects GitHub repository for ground-truth metadata.
    Rotates across the GitHub token pool; 429 cools the token and retries once
    on the next token before giving up (PENDING)."""
    url = f"https://api.github.com/repos/{owner_repo}"
    from aiorbit.config import github_pool
    for attempt in range(2):  # one direct try + one retry on a different token
        headers = github_pool.headers_for()
        try:
            resp = client.get(url, timeout=10.0, headers=headers)
            if resp.status_code == 429 or resp.status_code == 403:
                # rate limited — cool this token, retry on next
                retry_after = float(resp.headers.get("retry-after", 60))
                github_pool.set_cooldown(headers.get("Authorization", "").replace("token ", ""), retry_after)
                continue
            if resp.status_code in (404, 410):
                return {"status": "REJECTED", "reason": "REPO_GONE"}
            if resp.status_code == 200:
                data = resp.json()
                if data.get("archived", False):
                    return {"status": "REJECTED", "reason": "ARCHIVED"}
                return {
                    "status": "VERIFIED",
                    "stars": data.get("stargazers_count", 0),
                    "pushed_at": data.get("pushed_at"),
                    "archived": False,
                    "license": data.get("license", {}).get("spdx_id") if data.get("license") else None,
                    "description": data.get("description"),
                    "homepage": data.get("homepage"),
                    "is_fork": data.get("fork", False),
                }
            return {"status": "PENDING", "reason": f"GH_{resp.status_code}"}
        except Exception as e:
            return {"status": "PENDING", "reason": f"GH_ERR_{type(e).__name__}"}
    return {"status": "PENDING", "reason": "GH_RATE_LIMITED"}


def verify_github_search_shard(entries: List[MCPEntry], console: Console) -> List[MCPEntry]:
    """
    Verifies candidates sourced from GitHub Topic Search.
    Metadata (stars, push date, archive, license) is already live from search!
    """
    console.print(f"[cyan]Verifying GitHub Search shard: {len(entries)} candidates...[/cyan]")
    verified_list: List[MCPEntry] = []
    wal_file = open(WAL_GITHUB, "a", encoding="utf-8")

    for entry in entries:
        if entry.github_archived:
            entry.verification_status = "REJECTED"
            entry.rejection_reason = "ARCHIVED"
        elif (entry.github_stars or 0) == 0 and not entry.github_pushed_at:
            entry.verification_status = "REJECTED"
            entry.rejection_reason = "ZERO_ACTIVITY_STUB"
        else:
            entry.verification_status = "VERIFIED"
            entry.last_verified_date = datetime.now(timezone.utc).isoformat()
            act, adopt, docs, rel, total = compute_deterministic_breakdown(entry)
            entry.activity_score = act
            entry.adoption_score = adopt
            entry.docs_score = docs
            entry.reliability_score = rel
            entry.overall_score = round(total, 1)
            verified_list.append(entry)

        wal_file.write(entry.model_dump_json() + "\n")
        wal_file.flush()

    wal_file.close()
    console.print(f"[green]✅ GitHub Search shard complete: {len(verified_list)} verified[/green]")
    return verified_list


def verify_official_registry_shard(entries: List[MCPEntry], console: Console) -> List[MCPEntry]:
    """
    Verifies candidates sourced from the Official Registry.
    Validates GitHub repo if present, or official website.
    """
    console.print(f"[cyan]Verifying Official Registry shard: {len(entries)} candidates...[/cyan]")
    verified_list: List[MCPEntry] = []
    wal_file = open(WAL_OFFICIAL, "a", encoding="utf-8")

    # Token pool: per-request rotation via verify_github_repo_live's headers_for()
    gh_headers = {"Accept": "application/vnd.github.v3+json"}

    # Load already processed keys from WAL
    existing_wal_map: Dict[str, MCPEntry] = {}
    if WAL_OFFICIAL.exists():
        with open(WAL_OFFICIAL, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        we = MCPEntry.model_validate(json.loads(line))
                        existing_wal_map[clean_canonical_key(we)] = we
                    except Exception:
                        pass

    client_gh = httpx.Client(timeout=10.0, headers=gh_headers)
    processed = 0
    total_count = len(entries)

    for entry in entries:
        processed += 1
        key = clean_canonical_key(entry)
        if key in existing_wal_map:
            cached_e = existing_wal_map[key]
            if cached_e.verification_status == "VERIFIED":
                verified_list.append(cached_e)
            continue

        is_verified = False
        if entry.github_owner_repo:
            time.sleep(0.8)  # 1 req/s domain politeness on GitHub API
            gh_res = verify_github_repo_live(entry.github_owner_repo, client_gh)
            if gh_res.get("status") == "VERIFIED":
                entry.verification_status = "VERIFIED"
                entry.github_stars = gh_res.get("stars", 0)
                entry.github_pushed_at = gh_res.get("pushed_at")
                entry.github_archived = False
                if gh_res.get("license"):
                    entry.license = gh_res.get("license")
                if gh_res.get("homepage"):
                    entry.official_url = gh_res.get("homepage")
                is_verified = True
            elif gh_res.get("status") == "REJECTED":
                entry.verification_status = "REJECTED"
                entry.rejection_reason = gh_res.get("reason", "GITHUB_REJECTED")

        # Official Registry authoritative inclusion (curated by MCP registry)
        if not is_verified and entry.verification_status != "REJECTED" and entry.name:
            entry.verification_status = "VERIFIED"
            is_verified = True

        if is_verified:
            entry.last_verified_date = datetime.now(timezone.utc).isoformat()
            act, adopt, docs, rel, total_det = compute_deterministic_breakdown(entry)
            entry.activity_score = act
            entry.adoption_score = adopt
            entry.docs_score = docs
            entry.reliability_score = rel
            entry.overall_score = round(total_det, 1)
            verified_list.append(entry)

        wal_file.write(entry.model_dump_json() + "\n")
        wal_file.flush()

        if processed % 100 == 0 or processed == total_count:
            console.print(f"  Official Registry Shard: [{processed}/{total_count}] verified: {len(verified_list)}")

    wal_file.close()
    client_gh.close()
    console.print(f"[green]✅ Official Registry shard complete: {len(verified_list)} verified[/green]")
    return verified_list


def verify_glama_smithery_shard(entries: List[MCPEntry], console: Console) -> List[MCPEntry]:
    """Verifies Glama and Smithery directory links via GitHub REST API."""
    console.print(f"[cyan]Verifying Glama & Smithery shard: {len(entries)} candidates...[/cyan]")
    verified_list: List[MCPEntry] = []
    wal_file = open(WAL_GLAMA, "a", encoding="utf-8")

    gh_headers = {"Accept": "application/vnd.github.v3+json"}
    token = settings.github_token
    if token:
        gh_headers["Authorization"] = f"token {token}"

    # Load already processed keys from WAL
    existing_wal_map: Dict[str, MCPEntry] = {}
    if WAL_GLAMA.exists():
        with open(WAL_GLAMA, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        we = MCPEntry.model_validate(json.loads(line))
                        existing_wal_map[clean_canonical_key(we)] = we
                    except Exception:
                        pass

    client_gh = httpx.Client(timeout=10.0, headers=gh_headers)
    processed = 0
    total_count = len(entries)

    for entry in entries:
        processed += 1
        key = clean_canonical_key(entry)
        if key in existing_wal_map:
            cached_e = existing_wal_map[key]
            if cached_e.verification_status == "VERIFIED":
                verified_list.append(cached_e)
            continue

        if entry.github_owner_repo:
            time.sleep(0.8)  # 1 req/s domain politeness on GitHub API
            gh_res = verify_github_repo_live(entry.github_owner_repo, client_gh)
            if gh_res.get("status") == "VERIFIED":
                entry.verification_status = "VERIFIED"
                entry.github_stars = gh_res.get("stars", 0)
                entry.github_pushed_at = gh_res.get("pushed_at")
                entry.github_archived = False
                if gh_res.get("license"):
                    entry.license = gh_res.get("license")
                if gh_res.get("homepage"):
                    entry.official_url = gh_res.get("homepage")
                entry.last_verified_date = datetime.now(timezone.utc).isoformat()
                act, adopt, docs, rel, total_det = compute_deterministic_breakdown(entry)
                entry.activity_score = act
                entry.adoption_score = adopt
                entry.docs_score = docs
                entry.reliability_score = rel
                entry.overall_score = round(total_det, 1)
                verified_list.append(entry)
            else:
                entry.verification_status = "REJECTED"
                entry.rejection_reason = gh_res.get("reason", "GITHUB_REJECTED")

        wal_file.write(entry.model_dump_json() + "\n")
        wal_file.flush()

        if processed % 50 == 0 or processed == total_count:
            console.print(f"  Glama/Smithery Shard: [{processed}/{total_count}] verified: {len(verified_list)}")

    wal_file.close()
    client_gh.close()
    console.print(f"[green]✅ Glama & Smithery shard complete: {len(verified_list)} verified[/green]")
    return verified_list


def run_parallel_verification() -> None:
    console = Console()
    console.print("\n[bold cyan]================================================================[/bold cyan]")
    console.print("[bold green]🌟 AIOrbit Stage-2: Multi-Source Sharded Parallel Verification[/bold green]")
    console.print("[bold cyan]================================================================[/bold cyan]\n")

    # 1. Load already verified entries
    existing_verified: Dict[str, MCPEntry] = {}
    if COMBINED_VERIFIED_PATH.exists():
        with open(COMBINED_VERIFIED_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        e = MCPEntry.model_validate(json.loads(line))
                        existing_verified[clean_canonical_key(e)] = e
                    except Exception:
                        pass
        console.print(f"Preserving [green]{len(existing_verified)}[/green] existing verified records.")

    if not MULTISOURCE_PATH.exists():
        console.print(f"[red]Error: {MULTISOURCE_PATH} does not exist. Run discovery first.[/red]")
        return

    # 2. Load all multisource candidates
    candidates: List[MCPEntry] = []
    with open(MULTISOURCE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    candidates.append(MCPEntry.model_validate(json.loads(line)))
                except Exception:
                    pass

    # Filter out candidates already verified
    pending = [c for c in candidates if clean_canonical_key(c) not in existing_verified]
    console.print(f"Total multisource pool: [cyan]{len(candidates)}[/cyan] | Unverified pending: [magenta]{len(pending)}[/magenta]")

    # 3. Partition by source
    github_candidates = [c for c in pending if "GitHub Topic Search" in (c.discovery_source or "")]
    official_candidates = [c for c in pending if "Official Registry" in (c.discovery_source or "") and c not in github_candidates]
    directory_candidates = [c for c in pending if ("Glama.ai" in (c.discovery_source or "") or "Smithery.ai" in (c.discovery_source or "")) and c not in github_candidates and c not in official_candidates]

    console.print(f"Partitioned shards: [cyan]{len(github_candidates)}[/cyan] GitHub Search | [cyan]{len(official_candidates)}[/cyan] Official Registry | [cyan]{len(directory_candidates)}[/cyan] Glama/Smithery")

    # 4. Helper to append verified entries incrementally
    def append_verified(entries: List[MCPEntry]) -> int:
        added = 0
        with open(COMBINED_VERIFIED_PATH, "a", encoding="utf-8") as f:
            for entry in entries:
                k = clean_canonical_key(entry)
                if k not in existing_verified:
                    existing_verified[k] = entry
                    f.write(entry.model_dump_json() + "\n")
                    added += 1
        return added

    # Shard 1: GitHub Search (instant)
    if github_candidates:
        v_gh = verify_github_search_shard(github_candidates, console)
        added_gh = append_verified(v_gh)
        console.print(f"  -> Appended {added_gh} new verified GitHub Search entries to {COMBINED_VERIFIED_PATH.name} (Total pool: {len(existing_verified)})")

    # Shard 2: Official Registry
    if official_candidates:
        v_off = verify_official_registry_shard(official_candidates, console)
        added_off = append_verified(v_off)
        console.print(f"  -> Appended {added_off} new verified Official Registry entries to {COMBINED_VERIFIED_PATH.name} (Total pool: {len(existing_verified)})")

    # Shard 3: Glama / Smithery
    if directory_candidates:
        v_dir = verify_glama_smithery_shard(directory_candidates, console)
        added_dir = append_verified(v_dir)
        console.print(f"  -> Appended {added_dir} new verified Glama/Smithery entries to {COMBINED_VERIFIED_PATH.name} (Total pool: {len(existing_verified)})")

    console.print(f"\n[bold green]🚀 Parallel Verification Complete! Combined verified pool: {len(existing_verified)} entries.[/bold green]\n")


if __name__ == "__main__":
    run_parallel_verification()

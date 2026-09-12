"""
AIOrbit Pass 2 LLM Curation & 100-Point Rubric Aggregator.
Evaluates verified MCP candidates via the multi-tier LLM chain (Groq 120b -> 20b -> Gateway).
Applies Guideline §5–8 (Usefulness 30, Quality 25, Differentiation 5, Dedup, Saturation Capping).
Resumable via curated_wal.jsonl.
Outputs final_curated_1k.jsonl for Google Sheets / Excel export.
"""

import asyncio
import json
import random
from pathlib import Path
from typing import Dict, List, Set
import httpx
from rich.console import Console
from rich.table import Table

from aiorbit.schema import MCPEntry
from aiorbit.scoring import compute_deterministic_breakdown
from aiorbit.dedup import (
    clean_canonical_key,
    deduplicate_canonical_entries,
    apply_saturation_capping,
)
from aiorbit.llm_client import (
    evaluate_mcp_candidate_async,
    telemetry,
    key_pool,
)

EXPORTS_DIR = Path("aiorbit/exports_aiorbit")
VERIFIED_PATH = EXPORTS_DIR / "verified.jsonl"
CURATED_WAL_PATH = EXPORTS_DIR / "curated_wal.jsonl"
FINAL_1K_PATH = EXPORTS_DIR / "final_curated_1k.jsonl"


async def curate_entry(
    entry: MCPEntry,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> MCPEntry:
    """Evaluates a single entry with LLM and computes 100-point total score."""
    # Compute deterministic breakdown
    act, adopt, docs, rel, det_total = compute_deterministic_breakdown(entry)
    entry.activity_score = act
    entry.adoption_score = adopt
    entry.docs_score = docs
    entry.reliability_score = rel

    async with sem:
        # Pacing jitter to maintain polite TPM rate
        await asyncio.sleep(random.uniform(0.7, 1.2))
        eval_res = await evaluate_mcp_candidate_async(entry, client)

    if eval_res:
        entry.usefulness_score = float(eval_res["usefulness_score"])
        entry.quality_score = float(eval_res["quality_score"])
        entry.differentiation_score = float(eval_res["differentiation_score"])
        entry.key_capabilities = eval_res.get("key_capabilities") or []
        entry.supported_ai_clients = eval_res.get("supported_ai_clients") or []
        entry.transport = eval_res.get("transport")
        if eval_res.get("pricing_type"):
            entry.pricing_type = eval_res.get("pricing_type")

        serving_key = eval_res.get("serving_key") or "key_0"
        serving_tier = eval_res.get("serving_tier") or "tier1"
        notes = (eval_res.get("curation_notes") or "").strip()
        entry.curation_notes = f"[{serving_tier}:{serving_key}] {notes}".strip()

        # Sum 7 factors to get 100-point total score
        total_score = (
            entry.activity_score
            + entry.adoption_score
            + entry.docs_score
            + entry.reliability_score
            + entry.usefulness_score
            + entry.quality_score
            + entry.differentiation_score
        )
        entry.overall_score = round(min(total_score, 100.0), 1)
    else:
        # LLM failed across all tiers; entry gets deterministic score only
        entry.usefulness_score = 0.0
        entry.quality_score = 0.0
        entry.differentiation_score = 0.0
        entry.overall_score = round(det_total, 1)
        entry.curation_notes = "[terminal:unscored] Unscored by LLM fallback chain (API limit/failure)"

    return entry


async def run_curation(target_count: int = 1000, max_pending: Optional[int] = None) -> None:
    console = Console()
    console.print("\n[bold cyan]================================================================[/bold cyan]")
    console.print("[bold green]🌟 AIOrbit Stage-2: LLM Curation & 100-Point Scoring Engine[/bold green]")
    console.print("[bold cyan]================================================================[/bold cyan]\n")

    if not VERIFIED_PATH.exists():
        console.print(f"[bold red]❌ Error: {VERIFIED_PATH} does not exist.[/bold red]")
        return

    # 1. Load verified entries
    raw_verified: List[MCPEntry] = []
    with open(VERIFIED_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    raw_verified.append(MCPEntry.model_validate(json.loads(line)))
                except Exception:
                    pass

    console.print(f"Loaded [cyan]{len(raw_verified)}[/cyan] verified candidates.")

    # 2. Deduplicate canonical entries
    deduped = deduplicate_canonical_entries(raw_verified)
    console.print(f"Canonical deduplication: [cyan]{len(raw_verified)}[/cyan] -> [green]{len(deduped)}[/green] unique entries.")

    # 3. Load existing WAL for crash recovery / resumability (only genuinely scored entries)
    curated_map: Dict[str, MCPEntry] = {}
    if CURATED_WAL_PATH.exists():
        with open(CURATED_WAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        c_entry = MCPEntry.model_validate(json.loads(line))
                        if (c_entry.usefulness_score or 0.0) > 0.0:
                            curated_map[clean_canonical_key(c_entry)] = c_entry
                    except Exception:
                        pass
        console.print(f"Resuming from WAL: [yellow]{len(curated_map)}[/yellow] entries already scored with full LLM rubric.")

    # 4. Filter prioritized candidates for LLM scoring:
    # - All MCP Clients (Guideline priority)
    # - Servers with deterministic score >= 12.0 or stars > 0 or in curated categories
    clients = [e for e in deduped if e.mcp_type == "CLIENT"]
    servers = [e for e in deduped if e.mcp_type == "SERVER"]

    # Sort clients and servers by stars descending, tie break by deterministic score
    clients.sort(key=lambda e: (e.github_stars or 0, e.overall_score or 0.0), reverse=True)
    servers.sort(key=lambda e: (e.github_stars or 0, e.overall_score or 0.0), reverse=True)

    # Candidate evaluation queue: all clients + all verified servers
    eval_pool: List[MCPEntry] = []
    eval_pool.extend(clients)
    eval_pool.extend(servers)

    # Determine pending evaluations (candidates that do not have a positive usefulness_score yet)
    all_pending = [e for e in eval_pool if clean_canonical_key(e) not in curated_map]
    all_pending.sort(key=lambda e: (e.github_stars or 0, e.overall_score or 0.0), reverse=True)
    # If max_pending is provided, cap at max_pending; otherwise evaluate ALL pending verified records
    pending_entries = all_pending[:max_pending] if max_pending is not None else all_pending
    console.print(f"Queue: [cyan]{len(eval_pool)}[/cyan] candidate pool, [magenta]{len(all_pending)}[/magenta] total un-evaluated. Scoring [bold green]{len(pending_entries)}[/bold green] verified records (unlimited/draining mode).")

    num_keys = len(key_pool.keys)
    rpm_per_key = 30
    combined_rpm = num_keys * rpm_per_key
    est_seconds = (len(pending_entries) / combined_rpm) * 60 if combined_rpm > 0 else 0
    console.print(f"\n[bold cyan]⚡ Key Pool Architecture: {num_keys} Groq API keys active | Combined throughput: {combined_rpm} RPM[/bold cyan]")
    console.print(f"[bold cyan]📊 Wall-Clock Math: {len(pending_entries)} pending / ({num_keys} keys × {rpm_per_key} RPM) ≈ {est_seconds:.1f}s ({est_seconds/60:.1f} min)[/bold cyan]\n")

    # 5. Execute LLM evaluations with concurrency & real-time WAL persistence
    sem = asyncio.Semaphore(max(num_keys * 4, 4))  # 4 in-flight per key: fills each key's ~30 RPM budget
    wal_file = open(CURATED_WAL_PATH, "a", encoding="utf-8")
    wal_lock = asyncio.Lock()
    done_counter = [0]
    total_to_evaluate = len(pending_entries)

    async def process_one(entry: MCPEntry, http_client: httpx.AsyncClient) -> None:
        evaluated = await curate_entry(entry, http_client, sem)
        key = clean_canonical_key(evaluated)
        async with wal_lock:
            curated_map[key] = evaluated
            wal_file.write(evaluated.model_dump_json() + "\n")
            wal_file.flush()
            done_counter[0] += 1
            if done_counter[0] % 10 == 0 or done_counter[0] == total_to_evaluate:
                console.print(
                    f"Progress: [{done_counter[0]}/{total_to_evaluate}] LLM evaluated "
                    f"({(done_counter[0]/total_to_evaluate*100):.1f}%) | "
                    f"T1(Compound): {telemetry.tier1_calls} | T2(120b): {telemetry.tier2_calls} | "
                    f"T3(20b): {telemetry.tier3_calls} | T4(Qwen36): {telemetry.tier4_calls} | "
                    f"T5(Qwen38): {telemetry.tier5_calls} | Unscored: {telemetry.unscored_failures} | "
                    f"Keys: key_0={telemetry.to_dict().get('key_0', 0)}, key_1={telemetry.to_dict().get('key_1', 0)}"
                )

    async with httpx.AsyncClient(timeout=20.0) as http_client:
        if pending_entries:
            tasks = [process_one(entry, http_client) for entry in pending_entries]
            await asyncio.gather(*tasks)

        # 5b. Retry Pass for records tagged [terminal:unscored]
        unscored_entries = [
            e for e in curated_map.values()
            if (e.curation_notes or "").startswith("[terminal:unscored]")
        ]
        if unscored_entries:
            console.print(f"\n[bold yellow]🔄 Retrying {len(unscored_entries)} [terminal:unscored] records through fallback chain...[/bold yellow]")
            retry_tasks = [process_one(e, http_client) for e in unscored_entries]
            await asyncio.gather(*retry_tasks)

    wal_file.close()

    # 6. Apply Guideline §8 saturation capping on evaluated entries
    all_curated = list(curated_map.values())
    capped_entries = apply_saturation_capping(all_curated, max_per_cluster=4)
    console.print(f"Guideline §8 saturation capping: {len(all_curated)} -> {len(capped_entries)} diverse entries.")

    # 7. Data-Driven Quality Selection: every row justifies itself
    # Split into Clients and Servers
    curated_clients = [e for e in capped_entries if e.mcp_type == "CLIENT"]
    curated_servers = [e for e in capped_entries if e.mcp_type == "SERVER"]

    # Sort each group descending by overall score
    curated_clients.sort(key=lambda e: (e.overall_score or 0.0, e.github_stars or 0), reverse=True)
    curated_servers.sort(key=lambda e: (e.overall_score or 0.0, e.github_stars or 0), reverse=True)

    # Qualified selections:
    # Preserve all viable clients (foundational layer)
    selected_clients = [
        c for c in curated_clients 
        if (c.overall_score or 0) >= 40.0 and not (c.curation_notes or "").startswith("[terminal:unscored]")
    ]
    # Data-driven quality cutoff for servers: score >= 65.0 (every single row justifies itself)
    selected_servers = [
        s for s in curated_servers 
        if (s.overall_score or 0) >= 65.0 and not (s.curation_notes or "").startswith("[terminal:unscored]")
    ]

    final_pool = selected_clients + selected_servers
    final_pool.sort(key=lambda e: (e.overall_score or 0.0, e.github_stars or 0), reverse=True)

    # Save to final_curated_1k.jsonl
    with open(FINAL_1K_PATH, "w", encoding="utf-8") as f:
        for entry in final_pool:
            f.write(entry.model_dump_json() + "\n")

    console.print(f"\n[bold green]✅ Successfully wrote {len(final_pool)} curated records to {FINAL_1K_PATH} (data-driven quality cut)![/bold green]\n")

    # 8. Sanity Gate Verification
    corrupted_count = sum(
        1 for e in all_curated
        if (e.usefulness_score or 0.0) == 0.0 and (e.quality_score or 0.0) == 0.0 and (e.curation_notes or "") and "[terminal:unscored]" not in (e.curation_notes or "")
    )
    if corrupted_count > 0:
        console.print(f"[bold red]❌ SANITY GATE FAILED: Found {corrupted_count} zero-both corrupted records![/bold red]")
    else:
        console.print(f"[bold green]✅ SANITY GATE PASSED: 0 zero-both corrupted records exist in curated pool.[/bold green]")

    context7_entry = next((e for e in all_curated if "context7" in (e.name or "").lower()), None)
    if context7_entry:
        c7_score = context7_entry.overall_score or 0.0
        if c7_score > 80.0:
            console.print(f"[bold green]✅ Context7 Score Verified: {c7_score:.1f} > 80.0[/bold green]")
        else:
            console.print(f"[bold red]❌ Context7 Score Check Failed: {c7_score:.1f} <= 80.0[/bold red]")

    # 9. Summary & Telemetry Report
    # Score brackets
    b_90_100 = sum(1 for e in final_pool if (e.overall_score or 0) >= 90)
    b_80_89 = sum(1 for e in final_pool if 80 <= (e.overall_score or 0) < 90)
    b_70_79 = sum(1 for e in final_pool if 70 <= (e.overall_score or 0) < 80)
    b_below_70 = sum(1 for e in final_pool if (e.overall_score or 0) < 70)

    score_table = Table(title="Final Curated Score Distribution")
    score_table.add_column("Score Bracket", style="cyan")
    score_table.add_column("Guideline Tier", style="magenta")
    score_table.add_column("Count", style="green")
    score_table.add_column("Share", style="yellow")

    total_final = len(final_pool)
    score_table.add_row("90 – 100", "Excellent (Strongly Prioritize)", str(b_90_100), f"{(b_90_100/total_final*100):.1f}%")
    score_table.add_row("80 – 89", "Very Good (Include)", str(b_80_89), f"{(b_80_89/total_final*100):.1f}%")
    score_table.add_row("70 – 79", "Good (Include Selectively)", str(b_70_79), f"{(b_70_79/total_final*100):.1f}%")
    score_table.add_row("< 70", "Average / Edge Coverage", str(b_below_70), f"{(b_below_70/total_final*100):.1f}%")
    console.print(score_table)

    # Composition table
    comp_table = Table(title="Final Dataset Composition")
    comp_table.add_column("Type", style="cyan")
    comp_table.add_column("Count", style="green")
    comp_table.add_row("MCP Servers", str(len(selected_servers)))
    comp_table.add_row("MCP Clients", str(len(selected_clients)))
    comp_table.add_row("Total Curated", str(total_final))
    console.print(comp_table)

    # Telemetry table
    tel_table = Table(title="Multi-Tier LLM Chain Telemetry")
    tel_table.add_column("Tier Provider", style="cyan")
    tel_table.add_column("Invocations", style="green")
    for tier_name, count in telemetry.to_dict().items():
        tel_table.add_row(tier_name, str(count))
    console.print(tel_table)

    # Top-20 preview table
    top_table = Table(title="Top-20 Final Curated AIOrbit MCPs")
    top_table.add_column("Rank", style="dim", width=4)
    top_table.add_column("Name", style="bold white", max_width=25)
    top_table.add_column("Type", style="yellow", width=8)
    top_table.add_column("Score", style="bold green", width=6)
    top_table.add_column("Stars", style="magenta", width=7)
    top_table.add_column("Category", style="cyan", max_width=18)
    top_table.add_column("Trans", style="blue", width=6)
    top_table.add_column("Curation Notes", style="dim", max_width=35)

    for idx, e in enumerate(final_pool[:20], 1):
        top_table.add_row(
            str(idx),
            e.name[:25],
            e.mcp_type,
            f"{e.overall_score:.1f}",
            str(e.github_stars or 0),
            (e.category or "N/A")[:18],
            e.transport or "stdio",
            (e.curation_notes or "N/A")[:35],
        )
    console.print(top_table)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AIOrbit MCP LLM Curation & Scoring Engine")
    parser.add_argument("--max-pending", type=int, default=None, help="Maximum pending entries to score (default: unlimited)")
    parser.add_argument("--target-count", type=int, default=1000, help="Target curated count (default: 1000)")
    args = parser.parse_args()

    asyncio.run(run_curation(target_count=args.target_count, max_pending=args.max_pending))

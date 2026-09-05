"""
CLI entrypoint for running the FrontierAtlas AI Intelligence Pipeline.
Supports executing individual phases or running the end-to-end extraction and export.
"""

import asyncio
import json
import os
import signal
import time
from contextlib import suppress
from typing import Any, Callable, Dict, List, Optional, Tuple
import click
from rich.console import Console
from rich.table import Table

from src.config import settings
from src.crawlers.base import anti_bot_snapshot
from src.crawlers.papers_crawler import ResearchPapersCrawler
from src.crawlers.startups_crawler import StartupsCrawler
from src.crawlers.products_crawler import ProductsCrawler
from src.crawlers.news_crawler import NewsCrawler
from src.crawlers.jobs_crawler import JobsCrawler
from src.exporters.csv_exporter import CSVExporter
from src.exporters.excel_exporter import ExcelExporter
from src.exporters.graph_builder import KnowledgeGraphBuilder
from src.exporters.sheets_exporter import GoogleSheetsExporter
from src.llm.fallback_chain import llm_engine
from src.resolution.normalizer import entity_resolver
from src.utils.logger import logger, setup_logging
from src.utils.run_state import load_source_freshness, reset_source_freshness

# WAL checkpointing kicks in for long runs so an interrupted crawl resumes from exports/wal/.
WAL_AUTO_ENABLE_TARGET = 1000


def _warn_config(run_phase1: bool, upload_sheets: bool, target_count: int, fresh: bool = False) -> None:
    """Surface configuration gaps up front instead of letting them degrade silently."""
    if run_phase1:
        if not settings.github_token:
            console.print(
                "[yellow]⚠️  GITHUB_TOKEN not set: GitHub star enrichment runs in anonymous mode "
                "(~50 lookups, then pauses for an hour). Set GITHUB_TOKEN in .env for full enrichment.[/yellow]"
            )
        if not (settings.gemini_api_key or settings.groq_api_key or settings.effective_tier3_api_key):
            console.print(
                "[yellow]⚠️  No LLM API keys set: extraction will be deterministic-only. "
                "Set at least one of GEMINI_API_KEY / GROQ_API_KEY / CUSTOM_LLM_API_KEY in .env.[/yellow]"
            )
        if fresh:
            console.print("[yellow]--fresh flag set: WAL checkpoints ignored/truncated, per-source freshness history reset.[/yellow]")
        elif target_count >= WAL_AUTO_ENABLE_TARGET:
            console.print("[dim]WAL checkpointing enabled: an interrupted run resumes from exports/wal/.[/dim]")
    if upload_sheets and not settings.effective_service_account_path:
        console.print(
            "[yellow]⚠️  --sheets requested but GOOGLE_SERVICE_ACCOUNT_PATH is not set; upload will be skipped.[/yellow]"
        )


def _warn_stale_sources() -> None:
    """Surface news/job sources that produced 0 fresh items for >=2 consecutive runs
    (a dead feed looks like a healthy '0 fresh' run unless we track the history)."""
    for crawler_name in ("news", "jobs"):
        for source, entry in load_source_freshness(crawler_name).items():
            history = entry.get("recent_fresh_counts") if isinstance(entry, dict) else None
            if not isinstance(history, list):
                continue
            trailing_zeros = 0
            for count in reversed(history):
                if count == 0:
                    trailing_zeros += 1
                else:
                    break
            if trailing_zeros >= 2:
                console.print(
                    f"[yellow]⚠️  Stale source: '{source}' ({crawler_name}) produced 0 fresh items "
                    f"for {trailing_zeros} consecutive runs — the feed may be down or its URL may have changed.[/yellow]"
                )


def _stale_sources_snapshot() -> List[Dict[str, Any]]:
    """Stale-source entries ({crawler, source, consecutive_zero_runs}) for sources with
    >=2 consecutive zero-fresh runs — the run-report form of _warn_stale_sources."""
    stale: List[Dict[str, Any]] = []
    for crawler_name in ("news", "jobs"):
        for source, entry in load_source_freshness(crawler_name).items():
            history = entry.get("recent_fresh_counts") if isinstance(entry, dict) else None
            if not isinstance(history, list):
                continue
            trailing_zeros = 0
            for count in reversed(history):
                if count == 0:
                    trailing_zeros += 1
                else:
                    break
            if trailing_zeros >= 2:
                stale.append({"crawler": crawler_name, "source": source, "consecutive_zero_runs": trailing_zeros})
    return stale


def _collect_source_freshness() -> Dict[str, Dict[str, int]]:
    """Per-crawler {source: latest fresh count} for the run report."""
    out: Dict[str, Dict[str, int]] = {}
    for crawler_name in ("news", "jobs"):
        counts: Dict[str, int] = {}
        for source, entry in load_source_freshness(crawler_name).items():
            history = entry.get("recent_fresh_counts") if isinstance(entry, dict) else None
            if isinstance(history, list) and history:
                counts[source] = history[-1]
        if counts:
            out[crawler_name] = counts
    return out


def _write_run_report(
    run_id: str,
    duration_s: float,
    status: str,
    counts: Dict[str, int],
    target_count: int,
    resolution_log_rows: int,
    phase1: bool,
    phase2: bool,
    sheets_upload: str = "not_requested",
    output_dir: str = "exports",
) -> str:
    """Persist a machine-readable run report (CI/cron alerting primitive)."""
    report = {
        "run_id": run_id,
        "status": status,
        "phase1": phase1,
        "phase2": phase2,
        "duration_seconds": round(duration_s, 1),
        "target_count": target_count,
        "collected": counts,
        "resolution_log_rows": resolution_log_rows,
        "llm_tier_usage": llm_engine.get_tier_usage(),
        "sheets_upload": sheets_upload,
        "source_freshness": _collect_source_freshness(),
        "stale_sources": _stale_sources_snapshot(),
        "anti_bot": anti_bot_snapshot(active_crawlers=list(_ACTIVE_CRAWLERS.values())),
    }
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "run_report.json")
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    os.replace(tmp_path, path)
    logger.info(f"Run report persisted to {path}: status={status}, counts={counts}")
    return path


async def _periodic_cache_save(interval: float = 600.0) -> None:
    """Periodically persist the entity registry so a hard kill loses at most `interval` of learning."""
    while True:
        await asyncio.sleep(interval)
        entity_resolver.save_cache()

console = Console()


TARGET_VERTICALS = {"papers", "startups", "products"}
WINDOW_VERTICALS = {"news", "jobs"}

# Live and completed crawler registries: _progress_monitor polls these instances
# to report collected-vs-target counts without resetting counts when a vertical completes.
_ACTIVE_CRAWLERS: Dict[str, Any] = {}
_COMPLETED_CRAWLERS: Dict[str, Dict[str, Any]] = {}


def _mark_crawler_done(name: str, count: int, target: Optional[int] = None) -> None:
    """Retain final counts and mark a crawler completed so progress reporting persists."""
    _COMPLETED_CRAWLERS[name] = {"count": count, "target": target, "done": True}
    _ACTIVE_CRAWLERS.pop(name, None)


def _is_crawler_done(name: str) -> bool:
    """True when the vertical has completed execution."""
    return name in _COMPLETED_CRAWLERS


def _reset_crawler_state() -> None:
    """Clear active and completed crawler registries (for fresh runs and tests)."""
    _ACTIVE_CRAWLERS.clear()
    _COMPLETED_CRAWLERS.clear()


async def _crawl(name: str, crawler_cls, **kwargs):
    """Execute a crawler safely within an async context manager, exposing it for live progress."""
    async with crawler_cls(**kwargs) as crawler:
        _ACTIVE_CRAWLERS[name] = crawler
        try:
            results = await crawler.crawl()
            count = len(results) if isinstance(results, list) else len(getattr(crawler, "collected", []))
            target = getattr(crawler, "target_count", kwargs.get("target_count"))
            _mark_crawler_done(name, count, target)
            return results
        finally:
            if name not in _COMPLETED_CRAWLERS:
                count, target = _crawler_progress(name)
                _mark_crawler_done(name, count, target or kwargs.get("target_count"))
            _ACTIVE_CRAWLERS.pop(name, None)


def _crawler_progress(name: str) -> Tuple[int, Optional[int]]:
    """Return (collected, target) for a live crawler; retained final counts when complete; (0, None) otherwise."""
    crawler = _ACTIVE_CRAWLERS.get(name)
    if crawler is not None:
        collected = getattr(crawler, "collected", None)
        if collected is None:
            stats = getattr(crawler, "stats", None)  # news: per-source processed totals
            if stats:
                return sum(s.get("total", 0) for s in stats.values()), None
            return getattr(crawler, "_live_count", 0), None  # jobs: fresh records built
        return len(collected), getattr(crawler, "target_count", None)
    completed = _COMPLETED_CRAWLERS.get(name)
    if completed is not None:
        return completed.get("count", 0), completed.get("target")
    return 0, None


def _papers_enrichment_details(crawler: Any) -> Optional[Tuple[str, str, str]]:
    """Compute (enrichment_label, remaining_repos, eta_str) for in-flight star lookups."""
    if crawler is None or not getattr(crawler, "is_enriching", False):
        return None
    total = getattr(crawler, "enrich_total", 0)
    done = getattr(crawler, "enriched_count", 0)
    if total <= 0:
        return None
    remaining = max(0, total - done)
    pct = int(round(100.0 * done / total)) if total > 0 else 0
    available_tokens = [
        t for t in getattr(crawler, "github_tokens", [])
        if t not in getattr(crawler, "_exhausted_github_tokens", set())
    ]
    tokens = max(1, len(available_tokens))
    interval = float(getattr(settings, "github_interval_seconds", 2.1))
    eta_min = max(1, round((remaining * interval) / (tokens * 60.0))) if remaining > 0 else 0
    eta_str = f"ETA ~{eta_min} min" if eta_min > 0 else "~0 min"
    label = f"enriching stars {done}/{total} ({pct}%)"
    return label, str(remaining), eta_str


async def _progress_monitor(interval: float, run_phase1: bool, run_phase2: bool) -> None:
    """Periodically print per-vertical collected/target/remaining/ETA while the pipeline runs."""
    names = []
    if run_phase1:
        names += ["papers", "startups", "products"]
    if run_phase2:
        names += ["news", "jobs"]
    if not names:
        return
    start = time.monotonic()
    console.print(f"[dim]Live progress reporting every {interval:.0f}s — counts refresh automatically.[/dim]")
    while True:
        await asyncio.sleep(interval)
        elapsed_min = (time.monotonic() - start) / 60.0
        table = Table(title=f"Live Pipeline Progress — elapsed {elapsed_min:.1f} min")
        table.add_column("Vertical", style="cyan", no_wrap=True)
        table.add_column("Collected", style="magenta")
        table.add_column("Target", style="magenta")
        table.add_column("Remaining", style="yellow")
        table.add_column("ETA", style="green")
        for n in names:
            collected, target = _crawler_progress(n)
            crawler = _ACTIVE_CRAWLERS.get(n)
            is_done = _is_crawler_done(n)
            enrichment_info = _papers_enrichment_details(crawler) if n == "papers" else None
            if target:
                remaining = max(0, target - collected)
                pct = f" ({100.0 * collected / target:.0f}%)"
                if is_done:
                    eta = "✅ Done" if remaining == 0 else f"⚠️ Shortfall ({collected}/{target})"
                else:
                    eta = ""
                    if collected > 0 and elapsed_min > 0:
                        rate = collected / elapsed_min
                        eta = f"~{remaining / rate:.0f} min" if rate > 0 else ""

                if enrichment_info:
                    enrich_label, enrich_rem, enrich_eta = enrichment_info
                    if remaining == 0:
                        table.add_row(
                            n,
                            f"{collected}/{target} collected",
                            str(target),
                            enrich_label,
                            enrich_eta,
                        )
                    else:
                        table.add_row(n, f"{collected}{pct}", str(target), str(remaining), eta)
                    table.add_row(
                        " ↳ papers: stars",
                        enrich_label,
                        str(getattr(crawler, "enrich_total", 0)),
                        enrich_rem,
                        enrich_eta,
                    )
                else:
                    table.add_row(n, f"{collected}{pct}", str(target), str(remaining), eta)
            else:
                fallback_target = "— (target)" if n in TARGET_VERTICALS else "— (24h window)"
                eta = "✅ Done" if is_done else ""
                table.add_row(n, str(collected), fallback_target, "—", eta)
        console.print(table)


async def _safe_gather(tasks) -> List[list]:
    """Run crawler tasks concurrently; a failing crawler logs and yields [] instead of aborting the pipeline.
    Cancellation is re-raised so the graceful-shutdown path (cache save -> re-raise -> clean exit) still runs."""
    results = await asyncio.gather(*tasks, return_exceptions=True)
    cleaned = []
    for r in results:
        if isinstance(r, asyncio.CancelledError):
            raise r
        if isinstance(r, Exception):
            logger.error(f"Crawler failed: {r!r}")
            cleaned.append([])
        else:
            cleaned.append(r)
    return cleaned


def _display_llm_telemetry() -> dict:
    """Display LLM multi-tier extraction usage counts and persist to disk."""
    tier_usage = llm_engine.get_tier_usage()
    llm_engine.save_tier_telemetry()

    llm_table = Table(title="LLM Multi-Tier Extraction Telemetry")
    llm_table.add_column("Tier Provider", style="cyan", no_wrap=True)
    llm_table.add_column("Extractions Served", style="magenta")
    llm_table.add_column("Architecture Role", style="green")

    llm_table.add_row("Tier 1: Google Gemini Flash", str(tier_usage.get("gemini", 0)), "Primary Extraction (gemini-3.5-flash-lite)")
    llm_table.add_row("Tier 2: Groq Llama 3 / GPT-OSS", str(tier_usage.get("groq", 0)), "Secondary Fallback (high throughput)")
    llm_table.add_row("Tier 3: DeepSeek / Custom Gateway", str(tier_usage.get("deepseek", 0)), "Tertiary Fallback (OpenAI-compatible)")
    llm_table.add_row("Tier 4: Deterministic Zero-API", str(tier_usage.get("deterministic", 0)), "Hard Zero-Cost Heuristic Fallback")

    console.print(llm_table)
    return tier_usage


def _handle_sheets_upload(
    startups: list,
    products: list,
    papers: list,
    jobs: list,
    news: list,
    logs: list,
) -> None:
    """Execute Google Sheets upload or display clear instructions if unconfigured."""
    console.print("[yellow]Executing Deliverable 1: Uploading to Google Sheets...[/yellow]")
    sheets_exporter = GoogleSheetsExporter()
    if not sheets_exporter.is_configured():
        console.print(
            "[yellow]⚠️  Google Sheets export skipped: Service account credentials not found.\n"
            "To enable Google Sheets upload:\n"
            "  1. Place your Google Cloud service account JSON key in the project (e.g., credentials/service-account.json).\n"
            "  2. Set GOOGLE_SERVICE_ACCOUNT_PATH=credentials/service-account.json in .env\n"
            "  3. (Optional) Set EVALUATOR_EMAIL in .env to share viewer permissions automatically.\n"
            "Pipeline continued successfully; Excel (.xlsx) and CSV exports are preserved.[/yellow]"
        )
        return

    sheet_url = sheets_exporter.export(
        startups=startups,
        products=products,
        papers=papers,
        jobs=jobs,
        news=news,
        logs=logs,
    )
    if sheet_url:
        console.print(f"[bold green]📊 Live Google Sheets URL: {sheet_url}[/bold green]")
    else:
        console.print(
            "[yellow]⚠️  Google Sheets upload could not complete.\n"
            "Tip: On personal Google accounts, service accounts have 0 MB Drive storage.\n"
            "To resolve:\n"
            "  1. Create a blank sheet in your Google Drive (https://sheets.new).\n"
            f"  2. Click 'Share' and share it with: {sheets_exporter.service_account_email} (as Editor).\n"
            "  3. Copy the Spreadsheet ID from the URL and set GOOGLE_SHEETS_SPREADSHEET_ID=<id> in .env.\n"
            "Excel (.xlsx) and CSV deliverables remain fully intact.[/yellow]"
        )


def setup_signal_handlers(loop: asyncio.AbstractEventLoop, on_shutdown: Optional[Callable] = None):
    """Register POSIX signal handlers for graceful cancellation and cache flushing."""
    def _shutdown_handler(sig_name: str):
        logger.warning(f"POSIX signal {sig_name} received: initiating graceful shutdown...")
        console.print(f"\n[bold red]⚠️  Signal {sig_name} received. Flushing cache and shutting down...[/bold red]")
        entity_resolver.save_cache()
        if on_shutdown:
            try:
                on_shutdown()
            except Exception as exc:
                logger.debug(f"Shutdown hook error: {exc}")
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task(loop):
                task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _shutdown_handler(s.name))
        except (NotImplementedError, RuntimeError, ValueError) as exc:
            # Fallback for environments where add_signal_handler is not permitted (e.g. non-main thread)
            logger.debug(f"Could not register signal handler for {sig.name}: {exc}")


def _start_background_tasks(progress_interval: float, run_phase1: bool, run_phase2: bool) -> Tuple[Optional[asyncio.Task], asyncio.Task]:
    """Start the live progress monitor (when enabled) and the periodic registry save."""
    monitor_task = (
        asyncio.create_task(_progress_monitor(progress_interval, run_phase1, run_phase2))
        if progress_interval > 0
        else None
    )
    cache_save_task = asyncio.create_task(_periodic_cache_save())
    return monitor_task, cache_save_task


async def _cancel_background_tasks(monitor_task: Optional[asyncio.Task], cache_save_task: asyncio.Task) -> None:
    """Cancel the live progress monitor and periodic registry-save tasks (always on exit)."""
    if monitor_task is not None:
        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task
    cache_save_task.cancel()
    with suppress(asyncio.CancelledError):
        await cache_save_task


async def _run_crawlers(run_phase1: bool, run_phase2: bool, target_count: int, reset_wal: bool = False) -> Dict[str, list]:
    """Build and execute the Phase I/II crawler set concurrently.
    Returns per-vertical record lists (empty for skipped phases); propagates
    CancelledError so the caller can persist state and report interruption."""
    papers, startups, products, news, jobs = [], [], [], [], []
    phase1_tasks = []
    if run_phase1:
        console.print(f"[yellow]Launching Phase I: Bulk Acquisition (Target: {target_count})...[/yellow]")
        phase1_tasks = [
            _crawl("papers", ResearchPapersCrawler, target_count=target_count,
                   wal_enabled=target_count >= WAL_AUTO_ENABLE_TARGET,
                   reset_wal=reset_wal),
            _crawl("startups", StartupsCrawler, target_count=target_count,
                   wal_enabled=target_count >= WAL_AUTO_ENABLE_TARGET,
                   reset_wal=reset_wal),
            _crawl("products", ProductsCrawler, target_count=target_count,
                   wal_enabled=target_count >= WAL_AUTO_ENABLE_TARGET,
                   reset_wal=reset_wal),
        ]
    phase2_tasks = []
    if run_phase2:
        console.print("[yellow]Launching Phase II: 24h Signal Monitoring (5 News + 5 Job Boards)...[/yellow]")
        phase2_tasks = [
            _crawl("news", NewsCrawler),
            _crawl("jobs", JobsCrawler),
        ]
    if phase1_tasks and phase2_tasks:
        results = await _safe_gather([*phase1_tasks, *phase2_tasks])
        papers, startups, products = results[0], results[1], results[2]
        news, jobs = results[3], results[4]
    elif phase1_tasks:
        papers, startups, products = await _safe_gather(phase1_tasks)
    elif phase2_tasks:
        news, jobs = await _safe_gather(phase2_tasks)
    return {"papers": papers, "startups": startups, "products": products, "news": news, "jobs": jobs}


async def _run_exports(
    startups: List[Any], products: List[Any], papers: List[Any],
    jobs: List[Any], news: List[Any], logs: List[Any], output_xlsx: str,
) -> Dict[str, Any]:
    """Execute the three independent deliverable exports concurrently; returns graph metrics."""
    console.print("[yellow]Executing Phase VI: Exporting 6-Tab Excel, CSVs & Graph Construction...[/yellow]")
    out_dir = os.path.dirname(output_xlsx) or "exports"
    exporter = ExcelExporter()
    csv_exporter = CSVExporter(output_dir=out_dir)
    graph_builder = KnowledgeGraphBuilder()
    await asyncio.gather(
        asyncio.to_thread(exporter.export, filepath=output_xlsx, startups=startups, products=products,
                          papers=papers, jobs=jobs, news=news, logs=logs),
        asyncio.to_thread(csv_exporter.export_all, startups=startups, products=products,
                          papers=papers, jobs=jobs, news=news, logs=logs),
        asyncio.to_thread(graph_builder.build_graph, startups=startups, products=products,
                          papers=papers, jobs=jobs, news=news),
    )
    return graph_builder.get_summary_metrics()


def _report_interrupted(
    run_id: str, run_started: float, target_count: int, run_phase1: bool, run_phase2: bool, output_dir: str = "exports",
) -> None:
    """Persist an interrupted-run report from the CancelledError handler."""
    console.print("[yellow]Pipeline execution interrupted. Saving entity cache and exiting...[/yellow]")
    entity_resolver.save_cache()
    _write_run_report(
        run_id=run_id,
        duration_s=time.monotonic() - run_started,
        status="interrupted",
        counts={name: _crawler_progress(name)[0] for name in ("papers", "startups", "products", "news", "jobs")},
        target_count=target_count,
        resolution_log_rows=len(entity_resolver.audit_log),
        phase1=run_phase1,
        phase2=run_phase2,
        output_dir=output_dir,
    )


async def _finalize_run(
    run_id: str, run_started: float, run_phase1: bool, run_phase2: bool, target_count: int,
    upload_sheets: bool, startups: List[Any], products: List[Any], papers: List[Any],
    news: List[Any], jobs: List[Any], logs: List[Any], output_dir: str = "exports",
) -> bool:
    """Optional Sheets upload, stale-source warnings, shortfall gating, and the run report."""
    sheets_upload = "not_requested"
    if upload_sheets:
        sheets_upload = await asyncio.to_thread(
            _handle_sheets_upload,
            startups=startups,
            products=products,
            papers=papers,
            jobs=jobs,
            news=news,
            logs=logs,
        )
    _warn_stale_sources()
    shortfall = run_phase1 and any(len(x) < target_count for x in (startups, products, papers))
    if shortfall:
        console.print("[bold red]⚠️  Phase I target shortfall detected; exit code will be 1.[/bold red]")
    _write_run_report(
        run_id=run_id,
        duration_s=time.monotonic() - run_started,
        status="shortfall" if shortfall else "completed",
        counts={
            "startups": len(startups),
            "products": len(products),
            "papers": len(papers),
            "news": len(news),
            "jobs": len(jobs),
        },
        target_count=target_count,
        resolution_log_rows=len(logs),
        phase1=run_phase1,
        phase2=run_phase2,
        sheets_upload=sheets_upload,
        output_dir=output_dir,
    )
    return not shortfall


def _render_summary(
    run_phase1: bool, run_phase2: bool, target_count: int,
    startups: List[Any], products: List[Any], papers: List[Any],
    news: List[Any], jobs: List[Any], metrics: Dict[str, Any], output_xlsx: str,
) -> None:
    """Render the execution summary table, LLM telemetry, and deliverable note."""
    table = Table(title="FrontierAtlas Pipeline Execution Summary")
    table.add_column("Entity / Phase", style="cyan", no_wrap=True)
    table.add_column("Count Collected", style="magenta")
    table.add_column("Status", style="green")

    def _phase1_status(count: int) -> str:
        if not run_phase1:
            return "— (skipped)"
        return "✅ Complete" if count >= target_count else f"⚠️ Shortfall ({count}/{target_count})"

    def _phase2_status(count: int) -> str:
        if not run_phase2:
            return "— (skipped)"
        return "✅ Complete (<24h)" if count > 0 else "⚠️ No fresh items"

    rows = [
        ("Startups (Phase I)", len(startups), _phase1_status(len(startups))),
        ("Products (Phase I)", len(products), _phase1_status(len(products))),
        ("Research Papers (Phase I)", len(papers), _phase1_status(len(papers))),
        ("Fresh News Articles (Phase II)", len(news), _phase2_status(len(news))),
        ("Fresh Job Postings (Phase II)", len(jobs), _phase2_status(len(jobs))),
        ("Knowledge Graph Nodes", metrics["total_nodes"], "✅ Connected"),
        ("Knowledge Graph Edges", metrics["total_edges"], "✅ Connected"),
    ]
    for label, count, status in rows:
        table.add_row(label, str(count), status)
    console.print(table)
    _display_llm_telemetry()
    console.print(f"[bold green]✨ Multi-Tab Excel exported to: {output_xlsx}[/bold green]")


async def run_pipeline(
    run_phase1: bool = True,
    run_phase2: bool = True,
    target_count: int = 1000,
    output_xlsx: str = "exports/FrontierAtlas_Intelligence.xlsx",
    upload_sheets: bool = False,
    progress_interval: float = 180.0,
    fresh: bool = False,
):
    """Execute async pipeline phases and export structured deliverables."""
    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop)
    console.print("[bold blue]🚀 Launching FrontierAtlas AI Data Intelligence Pipeline...[/bold blue]")

    if fresh:
        console.print("[yellow]--fresh flag enabled: resetting per-source freshness history and truncating WAL checkpoints.[/yellow]")
        reset_source_freshness()

    _reset_crawler_state()

    run_started = time.monotonic()
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    monitor_task, cache_save_task = _start_background_tasks(progress_interval, run_phase1, run_phase2)

    out_dir = os.path.dirname(output_xlsx) or "exports"

    try:
        collected = await _run_crawlers(run_phase1, run_phase2, target_count, reset_wal=fresh)
    except asyncio.CancelledError:
        _report_interrupted(run_id, run_started, target_count, run_phase1, run_phase2, output_dir=out_dir)
        raise
    finally:
        await _cancel_background_tasks(monitor_task, cache_save_task)

    startups, products, papers, news, jobs = (
        collected[k] for k in ("startups", "products", "papers", "news", "jobs")
    )

    # Phase IV: Entity Resolution Audit Logs
    # Every resolve() call during crawls already appended to the central audit log.
    # Re-resolving here would double-log entries.
    logs = entity_resolver.audit_log

    # Persist learned entities & domain grounding so subsequent runs start warm.
    await asyncio.to_thread(entity_resolver.save_cache)

    metrics = await _run_exports(startups=startups, products=products, papers=papers,
                                 jobs=jobs, news=news, logs=logs, output_xlsx=output_xlsx)
    _render_summary(run_phase1, run_phase2, target_count, startups, products, papers,
                    news, jobs, metrics, output_xlsx)

    return await _finalize_run(
        run_id, run_started, run_phase1, run_phase2, target_count,
        upload_sheets, startups, products, papers, news, jobs, logs,
        output_dir=out_dir,
    )


DEFAULT_OUTPUT_XLSX = "exports/FrontierAtlas_Intelligence.xlsx"
PHASE1_OUTPUT_XLSX = "exports/phase1_test.xlsx"


@click.command()
@click.option("--phase", type=click.Choice(["1", "2", "all"]), default="all", help="Phase to execute")
@click.option("--target", type=int, default=1000, help="Target record count for Phase I")
@click.option("--output", type=str, default=None, help="Output Excel path (default: phase1_test.xlsx for --phase 1, else FrontierAtlas_Intelligence.xlsx)")
@click.option("--sheets", is_flag=True, default=False, help="Upload deliverables to Google Sheets (requires GOOGLE_SERVICE_ACCOUNT_PATH)")
@click.option("--progress-interval", type=int, default=180, help="Seconds between live progress updates (0 disables)")
@click.option("--fresh", is_flag=True, default=False, help="Ignore WAL checkpoints, truncate WAL files, and reset run state freshness")
def main(phase: str, target: int, output: str, sheets: bool, progress_interval: int, fresh: bool):
    """FrontierAtlas AI Intelligence Pipeline CLI."""
    setup_logging()
    run_phase1 = phase in ("1", "all")
    run_phase2 = phase in ("2", "all")
    _warn_config(run_phase1, sheets, target, fresh=fresh)
    # Default output aligns with scripts/verify_phase1.py expectations per phase.
    output_xlsx = output or (PHASE1_OUTPUT_XLSX if phase == "1" else DEFAULT_OUTPUT_XLSX)
    try:
        success = asyncio.run(
            run_pipeline(
                run_phase1=run_phase1,
                run_phase2=run_phase2,
                target_count=target,
                output_xlsx=output_xlsx,
                upload_sheets=sheets,
                progress_interval=progress_interval,
                fresh=fresh,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[bold yellow]Pipeline stopped gracefully. State and cache preserved.[/bold yellow]")
        return
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

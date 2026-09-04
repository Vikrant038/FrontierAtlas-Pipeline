"""
CLI entrypoint for running the FrontierAtlas AI Intelligence Pipeline.
Supports executing individual phases or running the end-to-end extraction and export.
"""

import asyncio
import signal
from typing import Callable, List, Optional
import click
from rich.console import Console
from rich.table import Table

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

console = Console()


async def _crawl(crawler_cls, **kwargs):
    """Execute a crawler safely within an async context manager for connection cleanup."""
    async with crawler_cls(**kwargs) as crawler:
        return await crawler.crawl()


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


async def run_pipeline(
    run_phase1: bool = True,
    run_phase2: bool = True,
    target_count: int = 1000,
    output_xlsx: str = "exports/FrontierAtlas_Intelligence.xlsx",
    upload_sheets: bool = False,
):
    """Execute async pipeline phases and export structured deliverables."""
    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop)
    console.print("[bold blue]🚀 Launching FrontierAtlas AI Data Intelligence Pipeline...[/bold blue]")

    startups, products, papers, jobs, news = [], [], [], [], []

    try:
        phase1_tasks = []
        if run_phase1:
            console.print(f"[yellow]Launching Phase I: Bulk Acquisition (Target: {target_count})...[/yellow]")
            phase1_tasks = [
                _crawl(ResearchPapersCrawler, target_count=target_count),
                _crawl(StartupsCrawler, target_count=target_count),
                _crawl(ProductsCrawler, target_count=target_count),
            ]

        phase2_tasks = []
        if run_phase2:
            console.print("[yellow]Launching Phase II: 24h Signal Monitoring (5 News + 5 Job Boards)...[/yellow]")
            phase2_tasks = [
                _crawl(NewsCrawler),
                _crawl(JobsCrawler),
            ]

        if phase1_tasks and phase2_tasks:
            results = await _safe_gather([*phase1_tasks, *phase2_tasks])
            papers, startups, products = results[0], results[1], results[2]
            news, jobs = results[3], results[4]
        elif phase1_tasks:
            papers, startups, products = await _safe_gather(phase1_tasks)
        elif phase2_tasks:
            news, jobs = await _safe_gather(phase2_tasks)

    except asyncio.CancelledError:
        console.print("[yellow]Pipeline execution interrupted. Saving entity cache and exiting...[/yellow]")
        entity_resolver.save_cache()
        raise

    # Phase IV: Entity Resolution Audit Logs
    # Every resolve() call during crawls already appended to the central audit log.
    # Re-resolving here would double-log entries.
    logs = entity_resolver.audit_log

    # Persist learned entities & domain grounding so subsequent runs start warm.
    await asyncio.to_thread(entity_resolver.save_cache)

    # Phase VI: Export Deliverables
    console.print("[yellow]Executing Phase VI: Exporting 6-Tab Excel & Graph Construction...[/yellow]")
    exporter = ExcelExporter()
    await asyncio.to_thread(
        exporter.export,
        filepath=output_xlsx,
        startups=startups,
        products=products,
        papers=papers,
        jobs=jobs,
        news=news,
        logs=logs,
    )

    csv_exporter = CSVExporter()
    await asyncio.to_thread(
        csv_exporter.export_all,
        startups=startups,
        products=products,
        papers=papers,
        jobs=jobs,
        news=news,
        logs=logs,
    )

    # In-memory graph
    graph_builder = KnowledgeGraphBuilder()
    await asyncio.to_thread(
        graph_builder.build_graph,
        startups=startups,
        products=products,
        papers=papers,
        jobs=jobs,
        news=news,
    )
    metrics = graph_builder.get_summary_metrics()

    # Telemetry summary table
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

    if upload_sheets:
        await asyncio.to_thread(
            _handle_sheets_upload,
            startups=startups,
            products=products,
            papers=papers,
            jobs=jobs,
            news=news,
            logs=logs,
        )

    shortfall = run_phase1 and any(len(x) < target_count for x in (startups, products, papers))
    if shortfall:
        console.print("[bold red]⚠️  Phase I target shortfall detected; exit code will be 1.[/bold red]")
    return not shortfall


DEFAULT_OUTPUT_XLSX = "exports/FrontierAtlas_Intelligence.xlsx"
PHASE1_OUTPUT_XLSX = "exports/phase1_test.xlsx"


@click.command()
@click.option("--phase", type=click.Choice(["1", "2", "all"]), default="all", help="Phase to execute")
@click.option("--target", type=int, default=1000, help="Target record count for Phase I")
@click.option("--output", type=str, default=None, help="Output Excel path (default: phase1_test.xlsx for --phase 1, else FrontierAtlas_Intelligence.xlsx)")
@click.option("--sheets", is_flag=True, default=False, help="Upload deliverables to Google Sheets (requires GOOGLE_SERVICE_ACCOUNT_PATH)")
def main(phase: str, target: int, output: str, sheets: bool):
    """FrontierAtlas AI Intelligence Pipeline CLI."""
    setup_logging()
    run_phase1 = phase in ("1", "all")
    run_phase2 = phase in ("2", "all")
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
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[bold yellow]Pipeline stopped gracefully. State and cache preserved.[/bold yellow]")
        return
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

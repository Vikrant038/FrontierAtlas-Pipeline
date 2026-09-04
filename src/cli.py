"""
CLI entrypoint for running the FrontierAtlas AI Intelligence Pipeline.
Supports executing individual phases or running the end-to-end extraction and export.
"""

import asyncio
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
from src.resolution.normalizer import entity_resolver
from src.utils.logger import setup_logging

console = Console()


async def _crawl(crawler_cls, **kwargs):
    """Execute a crawler safely within an async context manager for connection cleanup."""
    async with crawler_cls(**kwargs) as crawler:
        return await crawler.crawl()


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


async def run_pipeline(
    run_phase1: bool = True,
    run_phase2: bool = True,
    target_count: int = 1000,
    output_xlsx: str = "exports/FrontierAtlas_Intelligence.xlsx",
    upload_sheets: bool = False,
):
    """Execute async pipeline phases and export structured deliverables."""
    console.print("[bold blue]🚀 Launching FrontierAtlas AI Data Intelligence Pipeline...[/bold blue]")

    startups, products, papers, jobs, news = [], [], [], [], []

    # Phase I: Bulk Data Acquisition
    if run_phase1:
        console.print(f"[yellow]Executing Phase I: Bulk Acquisition (Target: {target_count})...[/yellow]")
        papers, startups, products = await asyncio.gather(
            _crawl(ResearchPapersCrawler, target_count=target_count),
            _crawl(StartupsCrawler, target_count=target_count),
            _crawl(ProductsCrawler, target_count=target_count),
        )

    # Phase II: High-Fidelity Signal Ingestion (24h Freshness)
    if run_phase2:
        console.print("[yellow]Executing Phase II: 24h Signal Monitoring (5 News + 5 Job Boards)...[/yellow]")
        news, jobs = await asyncio.gather(
            _crawl(NewsCrawler),
            _crawl(JobsCrawler),
        )

    # Phase IV: Entity Resolution Audit Logs
    # Every resolve() call during crawls already appended to the central audit log.
    # Re-resolving here would double-log entries.
    logs = entity_resolver.audit_log

    # Persist learned entities & domain grounding so subsequent runs start warm.
    entity_resolver.save_cache()

    # Phase VI: Export Deliverables
    console.print("[yellow]Executing Phase VI: Exporting 6-Tab Excel & Graph Construction...[/yellow]")
    exporter = ExcelExporter()
    exporter.export(
        filepath=output_xlsx,
        startups=startups,
        products=products,
        papers=papers,
        jobs=jobs,
        news=news,
        logs=logs,
    )

    csv_exporter = CSVExporter()
    csv_exporter.export_all(
        startups=startups,
        products=products,
        papers=papers,
        jobs=jobs,
        news=news,
        logs=logs,
    )

    # In-memory graph
    graph_builder = KnowledgeGraphBuilder()
    graph_builder.build_graph(startups=startups, products=products, papers=papers, jobs=jobs, news=news)
    metrics = graph_builder.get_summary_metrics()

    # Telemetry summary table
    table = Table(title="FrontierAtlas Pipeline Execution Summary")
    table.add_column("Entity / Phase", style="cyan", no_wrap=True)
    table.add_column("Count Collected", style="magenta")
    table.add_column("Status", style="green")

    rows = [
        ("Startups (Phase I)", len(startups), "✅ Complete"),
        ("Products (Phase I)", len(products), "✅ Complete"),
        ("Research Papers (Phase I)", len(papers), "✅ Complete"),
        ("Fresh News Articles (Phase II)", len(news), "✅ Complete (<24h)"),
        ("Fresh Job Postings (Phase II)", len(jobs), "✅ Complete (<24h)"),
        ("Knowledge Graph Nodes", metrics["total_nodes"], "✅ Connected"),
        ("Knowledge Graph Edges", metrics["total_edges"], "✅ Connected"),
    ]
    for label, count, status in rows:
        table.add_row(label, str(count), status)

    console.print(table)
    console.print(f"[bold green]✨ Multi-Tab Excel exported to: {output_xlsx}[/bold green]")

    if upload_sheets:
        _handle_sheets_upload(
            startups=startups,
            products=products,
            papers=papers,
            jobs=jobs,
            news=news,
            logs=logs,
        )


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
    asyncio.run(
        run_pipeline(
            run_phase1=run_phase1,
            run_phase2=run_phase2,
            target_count=target,
            output_xlsx=output_xlsx,
            upload_sheets=sheets,
        )
    )


if __name__ == "__main__":
    main()

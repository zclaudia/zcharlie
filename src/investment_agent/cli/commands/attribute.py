"""ia attribute sub-commands: run / show."""

import asyncio
import json
from datetime import date

import typer
from rich import print as rprint

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import AttributionReport

app = typer.Typer(help="Attribution commands (why did X move?)")


@app.command("run")
def run_attribution(
    ticker: str,
    market: str = typer.Option("NASDAQ", help="Market code"),
    as_of: str = typer.Option(
        date.today().isoformat(), help="Analysis date (YYYY-MM-DD)",
    ),
    benchmark: str = typer.Option("SPY", help="Benchmark ticker"),
) -> None:
    """Run Attribution pipeline for one ticker."""
    from investment_agent.attribution.attribution_agent import AttributionAgent
    from investment_agent.attribution.pipeline import AttributionPipeline
    from investment_agent.config import Settings
    from investment_agent.sources.yfinance_source import YFinanceSource

    settings = Settings()  # type: ignore[call-arg]
    agent = AttributionAgent(tier3_config=settings.llm_tier3)
    pipeline = AttributionPipeline(
        data_source=YFinanceSource(
            max_concurrent=settings.yfinance_max_concurrent,
        ),  # type: ignore[arg-type]
        agent=agent,
        settings=settings,
        benchmark_ticker=benchmark,
    )
    run_id = asyncio.run(
        pipeline.run(ticker, date.fromisoformat(as_of), market_code=market),
    )
    rprint(f"[green]AttributionRun {run_id} complete[/green]")


@app.command("show")
def show_attribution(run_id: int) -> None:
    """Show one AttributionReport by run_id."""

    async def _do() -> AttributionReport | None:
        async with session_scope() as session:
            return await session.get(AttributionReport, run_id)

    report = asyncio.run(_do())
    if report is None:
        rprint(f"[red]No AttributionReport for run {run_id}[/red]")
        raise typer.Exit(1)
    rprint(f"[bold]Attribution run {report.pipeline_run_id}[/bold]")
    rprint(f"Listing: {report.listing_id} · Date: {report.analysis_date}")
    rprint(f"Price change: {float(report.price_change_pct):.2%}")
    rprint(f"Narrative: {report.narrative}")
    if report.markdown_path:
        rprint(f"Markdown: {report.markdown_path}")
    rprint("Events:")
    rprint(json.dumps(report.triggering_events, indent=2, ensure_ascii=False))

"""ia pipeline sub-commands: discover / list / show."""

import asyncio
from datetime import date

import typer
from rich import print as rprint
from rich.table import Table
from sqlalchemy import select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun

app = typer.Typer(help="Pipeline commands")


@app.command("discover")
def discover(
    universe: str = typer.Option("NASDAQ_100", help="Universe code"),
    as_of: str = typer.Option(
        date.today().isoformat(), help="Date (YYYY-MM-DD)"
    ),
) -> None:
    """Run Discovery pipeline once."""
    from investment_agent.config import Settings
    from investment_agent.pipelines.discovery import DiscoveryPipeline
    from investment_agent.sources.yfinance_source import YFinanceSource
    from investment_agent.strategies.registry import load_all_strategies

    settings = Settings()  # type: ignore[call-arg]
    strategies = load_all_strategies(
        tier2_config=settings.llm_tier2,
        tier3_config=settings.llm_tier3,
    )
    pipeline = DiscoveryPipeline(
        # YFinanceSource satisfies DataSource at runtime; protocol mismatch
        # is purely Awaitable-vs-Coroutine return-annotation noise.
        data_source=YFinanceSource(  # type: ignore[arg-type]
            max_concurrent=settings.yfinance_max_concurrent
        ),
        strategies=strategies,
        settings=settings,
    )
    run_id = asyncio.run(pipeline.run(universe, date.fromisoformat(as_of)))
    rprint(f"[green]PipelineRun {run_id} complete[/green]")


@app.command("list")
def list_runs(limit: int = typer.Option(20, help="Max rows to show")) -> None:
    """List recent pipeline runs (newest first)."""

    async def _do() -> list[PipelineRun]:
        async with session_scope() as session:
            stmt = (
                select(PipelineRun)
                .order_by(PipelineRun.started_at.desc())
                .limit(limit)
            )
            return list((await session.execute(stmt)).scalars().all())

    rows = asyncio.run(_do())
    table = Table(title=f"Pipeline Runs (latest {limit})")
    table.add_column("ID")
    table.add_column("Kind")
    table.add_column("Universe")
    table.add_column("Status")
    table.add_column("Stage")
    table.add_column("Attempt")
    table.add_column("Started")
    table.add_column("Finished")
    for r in rows:
        table.add_row(
            str(r.id),
            r.kind,
            r.universe_code or "-",
            r.status,
            r.stage,
            f"{r.attempt_count}/{r.max_attempts}",
            r.started_at.isoformat(timespec="seconds"),
            r.finished_at.isoformat(timespec="seconds") if r.finished_at else "-",
        )
    rprint(table)


@app.command("show")
def show_run(run_id: int) -> None:
    """Show one pipeline run + its scores summary."""
    from investment_agent.storage.repositories.stock_score import StockScoreRepository

    async def _do() -> tuple[PipelineRun | None, int]:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            if run is None:
                return None, 0
            scores = await StockScoreRepository(session).by_run(run_id)
            return run, len(scores)

    run, n_scores = asyncio.run(_do())
    if run is None:
        rprint(f"[red]PipelineRun {run_id} not found[/red]")
        raise typer.Exit(1)
    rprint(f"[bold]Run {run.id}[/bold] kind={run.kind} status={run.status}")
    rprint(
        f"Stage: {run.stage} | Attempt: {run.attempt_count}/{run.max_attempts} "
        f"| Worker: {run.worker_id or '-'}"
    )
    rprint(f"Universe: {run.universe_code}")
    rprint(f"Started: {run.started_at} | Finished: {run.finished_at}")
    rprint(f"Scores recorded: {n_scores}")
    if run.error_summary:
        rprint(f"[red]Error: {run.error_summary}[/red]")

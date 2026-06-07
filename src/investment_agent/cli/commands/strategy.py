"""ia strategy sub-commands: list / show / sync."""

import asyncio
import json

import typer
from rich import print as rprint
from rich.table import Table

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import Strategy
from investment_agent.storage.repositories.strategy import StrategyRepository

app = typer.Typer(help="Strategy management commands")


@app.command("list")
def list_strategies() -> None:
    """List all strategies registered in DB."""

    async def _do() -> list[Strategy]:
        async with session_scope() as session:
            repo = StrategyRepository(session)
            return await repo.list_all()

    rows = asyncio.run(_do())
    table = Table(title="Strategies")
    table.add_column("Code")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Enabled")
    for s in rows:
        table.add_row(s.code, s.name, s.version, "yes" if s.enabled else "no")
    rprint(table)


@app.command("show")
def show_strategy(code: str) -> None:
    """Show one strategy's config."""

    async def _do() -> Strategy | None:
        async with session_scope() as session:
            repo = StrategyRepository(session)
            return await repo.get(code)

    s = asyncio.run(_do())
    if s is None:
        rprint(f"[red]Strategy '{code}' not found[/red]")
        raise typer.Exit(1)
    rprint(f"[bold]{s.name}[/bold] (code={s.code}, version={s.version})")
    rprint(f"Description: {s.description}")
    rprint("Config:")
    rprint(json.dumps(s.config_json, indent=2, ensure_ascii=False))


@app.command("sync")
def sync_strategies() -> None:
    """Sync strategies/*.yaml → DB."""
    from investment_agent.strategies.loader import sync_strategies_to_db
    from investment_agent.strategies.registry import DEFAULT_STRATEGIES_DIR

    n = asyncio.run(sync_strategies_to_db(DEFAULT_STRATEGIES_DIR))
    rprint(f"[green]Synced {n} strategies to DB[/green]")

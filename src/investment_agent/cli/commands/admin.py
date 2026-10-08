"""admin sub-commands: cost summary, db migrate, provider-call ledger prune."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.storage.database import run_with_engine, session_scope
from investment_agent.storage.models.ops import LLMCall
from investment_agent.storage.repositories.provider_call import ProviderCallRepository

app = typer.Typer(help="Admin commands")
_console = Console()


@app.command("cost")
def cost_summary(
    month: str = typer.Option(
        date.today().strftime("%Y-%m"),
        help="Month in YYYY-MM format",
    ),
) -> None:
    """Print monthly LLM cost grouped by model/provider/purpose."""
    year, mo = month.split("-")
    start = datetime(int(year), int(mo), 1, tzinfo=UTC)
    if int(mo) == 12:
        end = datetime(int(year) + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(int(year), int(mo) + 1, 1, tzinfo=UTC)

    async def _run() -> list[tuple[str, str, str, int, int, Decimal, int]]:
        async with session_scope() as session:
            stmt = (
                select(
                    LLMCall.provider,
                    LLMCall.model,
                    LLMCall.purpose,
                    func.sum(LLMCall.prompt_tokens),
                    func.sum(LLMCall.completion_tokens),
                    func.sum(LLMCall.cost_usd),
                    func.count(LLMCall.id),
                )
                .where(LLMCall.started_at >= start, LLMCall.started_at < end)
                .group_by(LLMCall.provider, LLMCall.model, LLMCall.purpose)
                .order_by(func.sum(LLMCall.cost_usd).desc())
            )
            return list((await session.execute(stmt)).all())  # type: ignore[arg-type]

    settings = Settings()  # type: ignore[call-arg]
    rows = run_with_engine(settings.database_url, _run)
    table = Table(title=f"LLM Cost Summary — {month}")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Purpose")
    table.add_column("Prompt Tokens", justify="right")
    table.add_column("Completion Tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")
    table.add_column("Calls", justify="right")
    total = Decimal(0)
    for provider, model, purpose, pt, ct, cost, n in rows:
        table.add_row(
            provider, model, purpose,
            f"{pt:,}", f"{ct:,}", f"${cost:.4f}", str(n),
        )
        total += cost
    _console.print(table)
    _console.print(f"[bold]Total: ${total:.4f}[/bold]")


@app.command("db-migrate")
def db_migrate() -> None:
    """Run alembic upgrade head."""
    import subprocess
    subprocess.run(["alembic", "upgrade", "head"], check=True)


@app.command("prune-provider-calls")
def prune_provider_calls(
    days: int = typer.Option(90, min=1, help="Keep provider_call rows younger than this many days"),
) -> None:
    """Delete ``provider_call`` ledger rows older than ``--days`` (default 90)."""

    before = datetime.now(UTC) - timedelta(days=days)

    async def _run() -> int:
        async with session_scope() as session:
            return await ProviderCallRepository(session).prune(before=before)

    settings = Settings()  # type: ignore[call-arg]
    removed = run_with_engine(settings.database_url, _run)
    _console.print(
        f"Removed {removed} provider_call rows older than {days} days "
        f"(before {before.isoformat(timespec='seconds')})."
    )

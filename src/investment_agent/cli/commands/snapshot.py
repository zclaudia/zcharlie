"""``ia snapshot``: enqueue (or run inline) PIT snapshot collection jobs."""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import date

import typer
from pydantic import ValidationError
from rich import print as rprint
from rich.table import Table

from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.providers.baostock_reference import INDEX_CODES, IndexCode
from investment_agent.snapshots.jobs import (
    SNAPSHOT_KINDS,
    SNAPSHOT_PROVIDERS,
    SNAPSHOT_SCOPES,
    FinancialSnapshotPayload,
    IndustrySyncPayload,
    MarketSnapshotPayload,
    SnapshotPayload,
    SnapshotProvider,
    SnapshotScope,
    UniverseSyncPayload,
    enqueue_snapshot_job,
)

app = typer.Typer(help="Point-in-time snapshot collection (universe, industry, market, financial)")

_UNIVERSE_HELP = f"Universe code: {', '.join(INDEX_CODES)}"
_PROVIDER_HELP = (
    "Data source: baostock (target pool breadth, default) or ifind (watchlist only; "
    "spends the iFinD gateway quota)"
)
_SCOPE_HELP = (
    "universe (members of --universe) or watchlist (enabled watchlist items); "
    "default: universe for baostock, watchlist for ifind"
)


def _date(value: str | None, name: str) -> date:
    if value is None:
        return shanghai_today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint=name) from exc


def _universe(value: str) -> IndexCode:
    code = value.strip().upper()
    if code not in INDEX_CODES:
        raise typer.BadParameter(_UNIVERSE_HELP, param_hint="--universe")
    return code


def _provider(value: str) -> SnapshotProvider:
    provider = value.strip().lower()
    if provider not in SNAPSHOT_PROVIDERS:
        raise typer.BadParameter(", ".join(SNAPSHOT_PROVIDERS), param_hint="--provider")
    return provider


def _routing(
    provider: str, scope: str | None, universe: str
) -> tuple[SnapshotProvider, SnapshotScope, IndexCode | None]:
    chosen = _provider(provider)
    if scope is None:
        scope = "watchlist" if chosen == "ifind" else "universe"
    scope = scope.strip().lower()
    if scope not in SNAPSHOT_SCOPES:
        raise typer.BadParameter(", ".join(SNAPSHOT_SCOPES), param_hint="--scope")
    if chosen == "ifind" and scope != "watchlist":
        raise typer.BadParameter(
            "iFinD only collects the watchlist scope; use --provider baostock for target pools",
            param_hint="--scope",
        )
    code = _universe(universe) if scope == "universe" else None
    return chosen, scope, code


async def _submit(settings: Settings, payload: SnapshotPayload, *, now: bool) -> None:
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.snapshots.handlers import snapshot_job_handlers
    from investment_agent.storage.database import init_engine, session_scope

    init_engine(settings.database_url)
    async with session_scope() as session:
        run, created = await enqueue_snapshot_job(session, payload, triggered_by="cli")
        run_id, kind, status, key = run.id, run.kind, run.status, run.idempotency_key
    if created:
        rprint(f"[green]Queued[/green] run #{run_id} ({kind}, key={key})")
    else:
        rprint(f"[yellow]Existing[/yellow] run #{run_id} ({kind}, key={key}) status={status}")
    if not now:
        return
    if status != "queued":
        rprint(f"Run #{run_id} is {status}; nothing to run inline")
        return
    worker = ResearchWorker(
        settings=settings,
        worker_id=f"cli:{socket.gethostname()}:{os.getpid()}",
        handlers=snapshot_job_handlers(settings),
    )
    await worker.run_once(run_id=run_id)
    async with session_scope() as session:
        from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

        finished = await PipelineRunRepository(session).get(run_id)
        assert finished is not None
        rprint(f"Run #{run_id} finished: status={finished.status}")
        _print_completeness(finished.metadata_json, finished.error_summary)


def _print_completeness(metadata: dict[str, object], error: str | None) -> None:
    completeness = metadata.get("completeness")
    if isinstance(completeness, dict):
        rprint(
            f"Completeness {completeness.get('ratio')}: expected {completeness.get('expected')}, "
            f"collected {completeness.get('collected')}, failed {completeness.get('failed_count')}, "
            f"pending {completeness.get('pending_count')}"
        )
    if error:
        rprint(f"Error: {error}")


def _run(payload: SnapshotPayload, now: bool) -> None:
    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(_submit(settings, payload, now=now))


_NOW_HELP = "Also execute the job in this process (debugging) instead of leaving it to the worker"


@app.command("sync-universe")
def sync_universe(
    universe: str = typer.Option("CSI300", help=_UNIVERSE_HELP),
    as_of: str | None = typer.Option(None, help="Constituents as of (YYYY-MM-DD, default today)"),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue a baostock constituent sync for one universe."""

    _run(
        UniverseSyncPayload(universe_code=_universe(universe), as_of=_date(as_of, "--as-of")),
        now,
    )


@app.command("sync-industry")
def sync_industry(
    universe: list[str] | None = typer.Option(  # noqa: B008
        None, help="Limit to members of these universes (repeatable); default: whole market"
    ),
    as_of: str | None = typer.Option(None, help="Classification as of (YYYY-MM-DD)"),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue a baostock CSRC industry sync."""

    codes = tuple(sorted({_universe(code) for code in universe or []}))
    _run(IndustrySyncPayload(as_of=_date(as_of, "--as-of"), universe_codes=codes), now)


@app.command("collect-market")
def collect_market(
    universe: str = typer.Option("CSI300", help=_UNIVERSE_HELP),
    provider: str = typer.Option("baostock", help=_PROVIDER_HELP),
    scope: str | None = typer.Option(None, help=_SCOPE_HELP),
    trade_date: str | None = typer.Option(
        None, "--date", help="Trade date (YYYY-MM-DD, default today in Asia/Shanghai)"
    ),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue incremental daily bars (raw + backward) for the scope's listings."""

    chosen, chosen_scope, code = _routing(provider, scope, universe)
    _run(
        MarketSnapshotPayload(
            provider=chosen,
            scope=chosen_scope,
            universe_code=code,
            trade_date=_date(trade_date, "--date"),
        ),
        now,
    )


@app.command("collect-financial")
def collect_financial(
    period: str = typer.Option(..., help="Report period, a quarter end such as 2026-06-30"),
    universe: str = typer.Option("CSI300", help=_UNIVERSE_HELP),
    provider: str = typer.Option("baostock", help=_PROVIDER_HELP),
    scope: str | None = typer.Option(None, help=_SCOPE_HELP),
    as_of: str | None = typer.Option(None, help="Membership date (YYYY-MM-DD, default today)"),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue one report period for listings that do not have it yet."""

    chosen, chosen_scope, code = _routing(provider, scope, universe)
    try:
        payload = FinancialSnapshotPayload(
            provider=chosen,
            scope=chosen_scope,
            universe_code=code,
            report_period=_date(period, "--period"),
            as_of=_date(as_of, "--as-of"),
        )
    except ValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--period") from exc
    _run(payload, now)


async def _status(settings: Settings, limit: int) -> Table:
    from investment_agent.storage.database import init_engine, session_scope
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

    init_engine(settings.database_url)
    table = Table(title="Snapshot runs")
    for column in (
        "run", "kind", "provider", "scope", "request", "status", "completeness", "finished"
    ):
        table.add_column(column)
    async with session_scope() as session:
        repository = PipelineRunRepository(session)
        for kind in SNAPSHOT_KINDS:
            for run in await repository.latest_by_kind(kind, limit=limit):
                request = run.request_json or {}
                detail = ", ".join(
                    f"{key}={value}"
                    for key, value in request.items()
                    if key in {"trade_date", "as_of", "report_period"}
                )
                completeness = (run.metadata_json or {}).get("completeness")
                ratio = (
                    f"{completeness.get('collected')}/{completeness.get('expected')} "
                    f"({completeness.get('ratio')})"
                    if isinstance(completeness, dict)
                    else "-"
                )
                table.add_row(
                    str(run.id),
                    kind,
                    str(request.get("provider", "baostock")),
                    run.universe_code or str(request.get("scope", "-")),
                    detail,
                    run.status,
                    ratio,
                    run.finished_at.isoformat(timespec="seconds") if run.finished_at else "-",
                )
    return table


@app.command("status")
def status(limit: int = typer.Option(3, min=1, max=50, help="Runs per kind")) -> None:
    """Latest runs per snapshot kind with their completeness."""

    settings = Settings()  # type: ignore[call-arg]
    rprint(asyncio.run(_status(settings, limit)))

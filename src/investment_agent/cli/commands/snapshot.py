"""``ia snapshot``: enqueue (or run inline) PIT snapshot collection jobs."""

from __future__ import annotations

import asyncio
import os
import re
import socket
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from pydantic import ValidationError
from rich import print as rprint
from rich.table import Table

from investment_agent.clock import shanghai_today
from investment_agent.config import Settings, SnapshotScheduleConfig, load_app_yaml
from investment_agent.providers.baostock_reference import INDEX_CODES, IndexCode
from investment_agent.snapshots.jobs import (
    SNAPSHOT_BACKFILL,
    SNAPSHOT_KINDS,
    SNAPSHOT_PROVIDERS,
    SNAPSHOT_SCOPES,
    FinancialSnapshotPayload,
    IndustrySyncPayload,
    MarketSnapshotPayload,
    SnapshotBackfillPayload,
    SnapshotPayload,
    SnapshotProvider,
    SnapshotScope,
    UniverseSyncPayload,
    enqueue_snapshot_job,
)

if TYPE_CHECKING:
    from investment_agent.snapshots.backfill import (
        BackfillEstimate,
        BackfillPlanResult,
        PartStatus,
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
_CONFIG_HELP = "App config providing snapshot.universes (the target pool)"


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


def target_pool_universes(config_path: Path) -> tuple[IndexCode, ...]:
    """``snapshot.universes`` from the app config (scheduler's target pool)."""

    snapshot = load_app_yaml(config_path).snapshot or SnapshotScheduleConfig()
    return tuple(sorted({_universe(code) for code in snapshot.universes}))


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
        None,
        help="Limit to members of these universes (repeatable); "
        "default: the configured target pool (snapshot.universes)",
    ),
    all_market: bool = typer.Option(
        False,
        "--all-market",
        help="Classify the whole market (~5000 listings are created) instead of the target pool",
    ),
    as_of: str | None = typer.Option(None, help="Classification as of (YYYY-MM-DD)"),
    config: Path = typer.Option(Path("config/app.yaml"), help=_CONFIG_HELP),  # noqa: B008
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue a baostock CSRC industry sync (target pool unless --all-market)."""

    if all_market and universe:
        raise typer.BadParameter("--all-market excludes --universe", param_hint="--all-market")
    codes: tuple[IndexCode, ...]
    if all_market:
        codes = ()
    elif universe:
        codes = tuple(sorted({_universe(code) for code in universe}))
    else:
        codes = target_pool_universes(config)
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
                    if key in {"trade_date", "as_of", "report_period", "part", "chunk"}
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


# --------------------------------------------------------------------------- #
# Historical backfill (P1-A4)
# --------------------------------------------------------------------------- #

_YEARS = re.compile(r"^\s*(\d{1,2})\s*y?\s*$", re.IGNORECASE)


def _years(value: str) -> int:
    match = _YEARS.fullmatch(value)
    if match is None or not 1 <= int(match.group(1)) <= 20:
        raise typer.BadParameter(
            "expected a number of years such as 5y (1-20)", param_hint="--prices"
        )
    return int(match.group(1))


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _print_estimate(estimate: BackfillEstimate) -> None:
    rprint(
        f"[bold]Backfill plan[/bold] {estimate.universe_code}: window "
        f"{estimate.window_start.isoformat()} .. {estimate.as_of.isoformat()}"
    )
    rprint(
        f"Symbols: {estimate.symbols} (current {estimate.current} + removed {estimate.removed}); "
        f"source: {estimate.source}"
    )
    rprint("Report periods: " + ", ".join(period.isoformat() for period in estimate.periods))
    table = Table(title="Estimated baostock work (upper bound; stored data is skipped)")
    for column in ("part", "jobs", "queries", "wall time"):
        table.add_column(column)
    for part in estimate.parts:
        table.add_row(part.part, str(part.jobs), str(part.queries), _duration(part.seconds))
    table.add_row(
        "total",
        str(sum(part.jobs for part in estimate.parts)),
        str(estimate.queries),
        _duration(estimate.seconds),
    )
    rprint(table)
    rprint("report_overdue is not backfilled (live detection only).")


def _print_plan(plan: BackfillPlanResult) -> None:
    membership = plan.membership
    rprint(f"membership: {membership.action} (run #{membership.run_id})")
    if not plan.followups:
        if membership.action != "done":
            rprint(
                "Industry, price and financial jobs are queued by the membership job when it "
                "succeeds (they need the survivorship set)."
            )
        return
    summary: dict[str, dict[str, int]] = {}
    for action in plan.followups:
        bucket = summary.setdefault(action.part, {})
        bucket[action.action] = bucket.get(action.action, 0) + 1
    for part, counts in summary.items():
        rprint(f"{part}: " + ", ".join(f"{count} {name}" for name, count in sorted(counts.items())))


def _print_backfill_status(
    as_of: date | None, statuses: list[PartStatus], universe: str
) -> None:
    if as_of is None:
        rprint(f"No backfill runs for {universe}")
        return
    table = Table(title=f"Backfill {universe} as of {as_of.isoformat()}")
    for column in ("part", "jobs", "job status", "symbols done", "failed", "detail"):
        table.add_column(column)
    for status in statuses:
        failed = str(len(status.failed_symbols))
        if status.failed_symbols:
            failed += f" ({', '.join(status.failed_symbols[:5])})"
        table.add_row(
            status.part,
            str(status.jobs),
            ", ".join(f"{name} {count}" for name, count in sorted(status.statuses.items()))
            or "-",
            f"{status.done_symbols}/{status.total_symbols}",
            failed,
            status.detail,
        )
    rprint(table)


async def _backfill(
    settings: Settings,
    payload: SnapshotBackfillPayload,
    *,
    dry_run: bool,
    show_status: bool,
    as_of_given: bool,
    now: bool,
) -> None:
    from investment_agent.snapshots.backfill import (
        backfill_status,
        dry_run_estimate,
        plan_backfill,
    )
    from investment_agent.storage.database import init_engine, session_scope

    init_engine(settings.database_url)
    if show_status:
        async with session_scope() as session:
            as_of, statuses = await backfill_status(
                session, payload.universe_code, payload.as_of if as_of_given else None
            )
        _print_backfill_status(as_of, statuses, payload.universe_code)
        return
    if dry_run:
        from investment_agent.providers.baostock_market import BaostockMarketClient

        _print_estimate(await dry_run_estimate(payload, client=BaostockMarketClient()))
        return
    async with session_scope() as session:
        plan = await plan_backfill(session, payload, triggered_by="cli")
    _print_plan(plan)
    if not now:
        rprint("Jobs run on the worker; re-run this command to resume, --status for progress.")
        return
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.snapshots.handlers import snapshot_job_handlers

    worker = ResearchWorker(
        settings=settings,
        worker_id=f"cli:{socket.gethostname()}:{os.getpid()}",
        handlers=snapshot_job_handlers(settings),
        kinds=[SNAPSHOT_BACKFILL],
    )
    executed = 0
    while (run_id := await worker.run_once()) is not None:
        executed += 1
        rprint(f"ran backfill job #{run_id}")
    async with session_scope() as session:
        as_of, statuses = await backfill_status(session, payload.universe_code, payload.as_of)
    rprint(f"{executed} job(s) executed inline")
    _print_backfill_status(as_of, statuses, payload.universe_code)


@app.command("backfill")
def backfill(
    universe: str = typer.Option("CSI300", help=_UNIVERSE_HELP),
    prices: str = typer.Option("5y", help="Price / membership history depth (D5: 5y)"),
    annual: int = typer.Option(6, min=0, max=20, help="Annual reports to backfill (D5: 6)"),
    quarters: int = typer.Option(4, min=0, max=12, help="Latest periodic reports (D5: 4)"),
    as_of: str | None = typer.Option(None, help="Backfill end date (YYYY-MM-DD, default today)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan and estimates; fetches no data"
    ),
    show_status: bool = typer.Option(False, "--status", help="Show progress per part"),
    now: bool = typer.Option(False, "--now", help="Also run the queued backfill jobs inline"),
) -> None:
    """Plan / resume the baostock history backfill (membership, industry, prices,
    financials). Re-running the command resumes unfinished parts."""

    day = _date(as_of, "--as-of")
    if day > shanghai_today():
        raise typer.BadParameter("--as-of cannot be in the future", param_hint="--as-of")
    payload = SnapshotBackfillPayload(
        universe_code=_universe(universe),
        as_of=day,
        part="membership",
        price_years=_years(prices),
        annual_years=annual,
        quarters=quarters,
    )
    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(
        _backfill(
            settings,
            payload,
            dry_run=dry_run,
            show_status=show_status,
            as_of_given=as_of is not None,
            now=now,
        )
    )

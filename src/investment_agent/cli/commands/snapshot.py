"""``ia snapshot``: enqueue (or run inline) PIT snapshot collection jobs."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from pydantic import ValidationError
from rich import print as rprint
from rich.table import Table

from investment_agent.cli.parsing import UNIVERSE_HELP, date_option, universe_code
from investment_agent.cli.runtime import bootstrap_settings, inline_worker, run_inline
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.providers.baostock_reference import IndexCode
from investment_agent.settings.loader import load_runtime, load_settings
from investment_agent.snapshots.jobs import (
    AKSHARE_FINANCIAL_PARTS,
    EARNINGS_EVENT_PARTS,
    INDUSTRY_SYSTEMS,
    SNAPSHOT_BACKFILL,
    SNAPSHOT_KINDS,
    SNAPSHOT_PROVIDERS,
    SNAPSHOT_SCOPES,
    AkshareFinancialPart,
    CloseQuotePayload,
    DividendSnapshotPayload,
    EarningsEventPart,
    EarningsEventPayload,
    FinancialSnapshotPayload,
    IndustrySyncPayload,
    IndustrySystem,
    MarketSnapshotPayload,
    SnapshotBackfillPayload,
    SnapshotPayload,
    SnapshotProvider,
    SnapshotScope,
    UniverseSyncPayload,
    enqueue_snapshot_job,
)
from investment_agent.storage.database import run_with_engine, session_scope

if TYPE_CHECKING:
    from investment_agent.snapshots.backfill import (
        BackfillEstimate,
        BackfillPlanResult,
        PartStatus,
    )

app = typer.Typer(help="Point-in-time snapshot collection (universe, industry, market, financial, dividends)")

_PROVIDER_HELP = (
    "Data source: baostock (target pool breadth, default), ifind (watchlist only; "
    "spends the iFinD gateway quota) or akshare (Eastmoney financials only); "
    "tencent serves collect-close-quote only"
)
_PART_HELP = (
    "akshare only: summary (whole-market period summaries, default) or cash_flow "
    "(per-stock cash-flow statements, ~20 s per stock)"
)
_SCOPE_HELP = (
    "universe (members of --universe) or watchlist (enabled watchlist items); "
    "default: universe for baostock, watchlist for ifind"
)
_CONFIG_HELP = (
    "App config; its snapshot.universes is the target pool only until the settings "
    "import ran (afterwards: the schedule.snapshot.universes setting)"
)


def _period(value: str | None, name: str, *, default: date) -> date:
    """``YYYY-MM-DD`` or the Eastmoney ``YYYYMMDD`` spelling; ``default`` when absent."""

    if value is None:
        return default
    text = value.strip()
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise typer.BadParameter("expected YYYY-MM-DD or YYYYMMDD", param_hint=name) from exc


def _earnings_part(value: str) -> EarningsEventPart:
    part = value.strip().lower()
    for item in EARNINGS_EVENT_PARTS:
        if item == part:
            return item
    raise typer.BadParameter(" | ".join(EARNINGS_EVENT_PARTS), param_hint="--part")


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
    code = universe_code(universe) if scope == "universe" else None
    return chosen, scope, code


def _part(value: str | None, provider: SnapshotProvider) -> AkshareFinancialPart | None:
    if value is None:
        return None
    if provider != "akshare":
        raise typer.BadParameter("--part only applies to --provider akshare", param_hint="--part")
    part = value.strip().lower()
    for item in AKSHARE_FINANCIAL_PARTS:
        if item == part:
            return item
    raise typer.BadParameter(" or ".join(AKSHARE_FINANCIAL_PARTS), param_hint="--part")


def _systems(values: list[str] | None) -> tuple[IndustrySystem, ...]:
    if not values:
        return INDUSTRY_SYSTEMS
    chosen: list[IndustrySystem] = []
    for value in values:
        system = value.strip().lower()
        if system not in INDUSTRY_SYSTEMS:
            raise typer.BadParameter(" or ".join(INDUSTRY_SYSTEMS), param_hint="--system")
        chosen.append(system)
    return tuple(item for item in INDUSTRY_SYSTEMS if item in chosen)


async def target_pool_universes(settings: Settings, config_path: Path) -> tuple[IndexCode, ...]:
    """The scheduler's target pool: the ``schedule.snapshot.universes`` setting
    (``snapshot.universes`` of ``config_path`` before the settings import)."""

    async with session_scope() as session:
        runtime = await load_runtime(session, settings, config_path)
    return tuple(sorted({universe_code(code) for code in runtime.schedule.snapshot.universes}))


async def _loaded[T](bootstrap: Settings, main: Callable[[Settings], Awaitable[T]]) -> T:
    """``main`` with the effective settings (runtime layer read from the DB)."""

    return await main(await load_settings(base=bootstrap))


PayloadFactory = Callable[[Settings], Awaitable[SnapshotPayload]]


async def _submit(
    settings: Settings, payload: SnapshotPayload | PayloadFactory, *, now: bool
) -> None:
    from investment_agent.snapshots.handlers import snapshot_job_handlers

    if callable(payload):
        payload = await payload(settings)
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
    finished = await run_inline(
        settings, run_id=run_id, handlers=snapshot_job_handlers(settings)
    )
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


def _run(payload: SnapshotPayload | PayloadFactory, now: bool) -> None:
    bootstrap = bootstrap_settings()
    run_with_engine(
        bootstrap.database_url,
        lambda: _loaded(bootstrap, lambda settings: _submit(settings, payload, now=now)),
    )


_NOW_HELP = "Also execute the job in this process (debugging) instead of leaving it to the worker"


@app.command("sync-universe")
def sync_universe(
    universe: str = typer.Option("CSI300", help=UNIVERSE_HELP),
    as_of: str | None = typer.Option(None, help="Constituents as of (YYYY-MM-DD, default today)"),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue a baostock constituent sync for one universe."""

    _run(
        UniverseSyncPayload(universe_code=universe_code(universe), as_of=date_option(as_of, "--as-of")),
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
    system: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--system",
        help="Classification system: sw2021 (Shenwan 2021 via AkShare) or csrc (baostock); "
        "repeatable; default: both",
    ),
    config: Path = typer.Option(Path("config/app.yaml"), help=_CONFIG_HELP),  # noqa: B008
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue an industry sync: CSRC (baostock) and Shenwan 2021 (AkShare).

    Target pool unless --all-market. CSRC creates listings for the whole
    market with --all-market; Shenwan only updates existing listings.
    """

    systems = _systems(system)
    if all_market and universe:
        raise typer.BadParameter("--all-market excludes --universe", param_hint="--all-market")
    day = date_option(as_of, "--as-of")
    if all_market or universe:
        codes = tuple(sorted({universe_code(code) for code in universe or []}))
        _run(IndustrySyncPayload(as_of=day, universe_codes=codes, systems=systems), now)
        return

    async def target_pool(settings: Settings) -> SnapshotPayload:
        codes = await target_pool_universes(settings, config)
        return IndustrySyncPayload(as_of=day, universe_codes=codes, systems=systems)

    _run(target_pool, now)


@app.command("collect-market")
def collect_market(
    universe: str = typer.Option("CSI300", help=UNIVERSE_HELP),
    provider: str = typer.Option("baostock", help=_PROVIDER_HELP),
    scope: str | None = typer.Option(None, help=_SCOPE_HELP),
    trade_date: str | None = typer.Option(
        None, "--date", help="Trade date (YYYY-MM-DD, default today in Asia/Shanghai)"
    ),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue incremental daily bars (raw + backward) for the scope's listings."""

    chosen, chosen_scope, code = _routing(provider, scope, universe)
    try:
        payload = MarketSnapshotPayload(
            provider=chosen,
            scope=chosen_scope,
            universe_code=code,
            trade_date=date_option(trade_date, "--date"),
        )
    except ValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--provider") from exc
    _run(payload, now)


@app.command("collect-close-quote")
def collect_close_quote(
    universe: str = typer.Option("CSI300", help=UNIVERSE_HELP),
    scope: str | None = typer.Option(
        None,
        help="universe (members of --universe plus watchlist / candidates / holdings, "
        "default) or watchlist (the user's pools only)",
    ),
    trade_date: str | None = typer.Option(
        None, "--date", help="Trade date (YYYY-MM-DD, default today in Asia/Shanghai)"
    ),
    retry: bool = typer.Option(
        False, "--retry", help="The 16:00 retry slot (separate run; stored listings are skipped)"
    ),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue the Tencent same-day close (15:10 close quote) for the scope's listings."""

    _, chosen_scope, code = _routing("tencent", scope, universe)
    try:
        payload = CloseQuotePayload(
            provider="tencent",
            scope=chosen_scope,
            universe_code=code,
            trade_date=date_option(trade_date, "--date"),
            slot="retry" if retry else "close",
        )
    except ValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--date") from exc
    _run(payload, now)


@app.command("collect-earnings-events")
def collect_earnings_events(
    part: str = typer.Option(
        ...,
        help="forecast (业绩预告) | schedule (预约披露时间) | segments (主营构成, user pools only)",
    ),
    period: str | None = typer.Option(
        None,
        help="Report period (YYYY-MM-DD or YYYYMMDD, e.g. 20250930); default: the latest "
        "quarter end before --as-of",
    ),
    universe: str = typer.Option("CSI300", help=UNIVERSE_HELP),
    scope: str | None = typer.Option(
        None,
        help="universe (members of --universe plus watchlist / candidates / holdings, "
        "default) or watchlist (the user's pools only); segments always use the user's pools",
    ),
    as_of: str | None = typer.Option(None, help="Membership date (YYYY-MM-DD, default today)"),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue Eastmoney earnings events (forecasts, disclosure schedule, segments)."""

    from investment_agent.scheduler.snapshot_jobs import earnings_report_period

    _, chosen_scope, code = _routing("akshare", scope, universe)
    membership = date_option(as_of, "--as-of")
    try:
        payload = EarningsEventPayload(
            provider="akshare",
            part=_earnings_part(part),
            scope=chosen_scope,
            universe_code=code,
            report_period=_period(period, "--period", default=earnings_report_period(membership)),
            as_of=membership,
        )
    except ValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--period") from exc
    _run(payload, now)


@app.command("collect-financial")
def collect_financial(
    period: str = typer.Option(..., help="Report period, a quarter end such as 2026-06-30"),
    universe: str = typer.Option("CSI300", help=UNIVERSE_HELP),
    provider: str = typer.Option("baostock", help=_PROVIDER_HELP),
    scope: str | None = typer.Option(None, help=_SCOPE_HELP),
    as_of: str | None = typer.Option(None, help="Membership date (YYYY-MM-DD, default today)"),
    part: str | None = typer.Option(None, help=_PART_HELP),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue one report period for listings that do not have it yet
    (akshare: store every changed Eastmoney capture as a new version)."""

    chosen, chosen_scope, code = _routing(provider, scope, universe)
    chosen_part = _part(part, chosen)
    try:
        payload = FinancialSnapshotPayload(
            provider=chosen,
            scope=chosen_scope,
            universe_code=code,
            report_period=date_option(period, "--period"),
            as_of=date_option(as_of, "--as-of"),
            part=chosen_part,
        )
    except ValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--period") from exc
    _run(payload, now)


@app.command("collect-dividends")
def collect_dividends(
    universe: str = typer.Option("CSI300", help=UNIVERSE_HELP),
    scope: str | None = typer.Option(None, help=_SCOPE_HELP),
    as_of: str | None = typer.Option(None, help="Membership date (YYYY-MM-DD, default today)"),
    year: list[int] | None = typer.Option(  # noqa: B008
        None,
        "--year",
        help="Ex-date year to collect (repeatable); default: as-of year and the year before",
    ),
    now: bool = typer.Option(False, "--now", help=_NOW_HELP),
) -> None:
    """Enqueue baostock dividend events (by ex-date year) for the scope's listings."""

    _, chosen_scope, code = _routing("baostock", scope, universe)
    try:
        payload = DividendSnapshotPayload(
            provider="baostock",
            scope=chosen_scope,
            universe_code=code,
            as_of=date_option(as_of, "--as-of"),
            years=tuple(year or ()),
        )
    except ValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--year") from exc
    _run(payload, now)


async def _status(settings: Settings, limit: int) -> Table:
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

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

    settings = bootstrap_settings()
    rprint(run_with_engine(settings.database_url, lambda: _status(settings, limit)))


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
    from investment_agent.storage.database import session_scope

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
    from investment_agent.snapshots.handlers import snapshot_job_handlers

    worker = inline_worker(settings, snapshot_job_handlers(settings), kinds=[SNAPSHOT_BACKFILL])
    executed = 0
    while (run_id := await worker.run_once()) is not None:
        executed += 1
        rprint(f"ran backfill job #{run_id}")
    async with session_scope() as session:
        as_of, statuses = await backfill_status(session, payload.universe_code, payload.as_of)
    rprint(f"{executed} job(s) executed inline")
    _print_backfill_status(as_of, statuses, payload.universe_code)


async def _akshare_backfill(
    settings: Settings,
    universe_code: IndexCode,
    as_of: date,
    *,
    annual: int,
    quarters: int,
    with_cash_flow: bool,
    dry_run: bool,
    show_status: bool,
    now: bool,
) -> None:
    from investment_agent.snapshots.eastmoney import (
        akshare_backfill_payloads,
        estimate_akshare_backfill,
        plan_akshare_backfill,
    )
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.repositories.universe import UniverseRepository

    payloads = akshare_backfill_payloads(
        universe_code,
        as_of,
        annual_years=annual,
        quarters=quarters,
        with_cash_flow=with_cash_flow,
    )
    periods = sorted({payload.report_period for payload in payloads if payload.part == "summary"})
    rprint(
        f"[bold]AkShare (Eastmoney) backfill[/bold] {universe_code} as of {as_of.isoformat()}: "
        f"{len(periods)} period(s) " + ", ".join(period.isoformat() for period in periods)
    )
    if dry_run:
        async with session_scope() as session:
            members = await UniverseRepository(session).members_between(
                universe_code, periods[0] if periods else as_of, as_of
            )
        listings = len({member.ticker for member in members})
        calls, seconds = estimate_akshare_backfill(payloads, listings=listings)
        rprint(
            f"Survivorship set: {listings} listing(s); estimated {calls} AkShare call(s), "
            f"~{_duration(seconds)} (cash-flow statements: "
            f"{'yes' if with_cash_flow else 'no, add --with-cash-flow'})"
        )
        return
    if show_status:
        from sqlalchemy import select

        from investment_agent.snapshots.jobs import idempotency_key
        from investment_agent.storage.models.analysis import PipelineRun

        table = Table(title=f"AkShare backfill {universe_code} as of {as_of.isoformat()}")
        for column in ("part", "period", "run", "status", "completeness"):
            table.add_column(column)
        async with session_scope() as session:
            for payload in payloads:
                key = idempotency_key(payload)
                run = (
                    await session.execute(
                        select(PipelineRun)
                        .where(PipelineRun.idempotency_key.like(f"{key}%"))
                        .order_by(PipelineRun.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                completeness = (run.metadata_json or {}).get("completeness") if run else None
                ratio = completeness.get("ratio") if isinstance(completeness, dict) else None
                table.add_row(
                    str(payload.part),
                    payload.report_period.isoformat(),
                    f"#{run.id}" if run else "-",
                    run.status if run else "not queued",
                    "-" if ratio is None else str(ratio),
                )
        rprint(table)
        return
    async with session_scope() as session:
        actions = await plan_akshare_backfill(session, payloads, triggered_by="cli")
    for action in actions:
        verb = "queued" if action.created else action.status
        rprint(f"{action.part} {action.report_period.isoformat()}: {verb} (run #{action.run_id})")
    if not now:
        rprint("Jobs run on the worker; re-run this command to resume, --status for progress.")
        return
    from investment_agent.snapshots.handlers import snapshot_job_handlers

    worker = inline_worker(settings, snapshot_job_handlers(settings))
    for action in actions:
        if action.status == "queued":
            await worker.run_once(run_id=action.run_id)
            rprint(f"ran {action.part} {action.report_period.isoformat()} (run #{action.run_id})")


@app.command("backfill")
def backfill(
    universe: str = typer.Option("CSI300", help=UNIVERSE_HELP),
    prices: str = typer.Option("5y", help="Price / membership history depth (D5: 5y)"),
    annual: int = typer.Option(6, min=0, max=20, help="Annual reports to backfill (D5: 6)"),
    quarters: int = typer.Option(4, min=0, max=12, help="Latest periodic reports (D5: 4)"),
    as_of: str | None = typer.Option(None, help="Backfill end date (YYYY-MM-DD, default today)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan and estimates; fetches no data"
    ),
    show_status: bool = typer.Option(False, "--status", help="Show progress per part"),
    now: bool = typer.Option(False, "--now", help="Also run the queued backfill jobs inline"),
    provider: str = typer.Option(
        "baostock",
        help="baostock (membership, industry, prices, financials; default) or akshare "
        "(Eastmoney period summaries of the same report periods)",
    ),
    with_cash_flow: bool = typer.Option(
        False,
        "--with-cash-flow",
        help="akshare only: also backfill per-stock cash-flow statements (~20 s per stock)",
    ),
) -> None:
    """Plan / resume the history backfill. baostock: membership, industry, prices,
    financials; akshare: Eastmoney period summaries (+ cash-flow statements).
    Re-running the command resumes unfinished parts."""

    day = date_option(as_of, "--as-of")
    if day > shanghai_today():
        raise typer.BadParameter("--as-of cannot be in the future", param_hint="--as-of")
    chosen = provider.strip().lower()
    if chosen not in {"baostock", "akshare"}:
        raise typer.BadParameter("baostock or akshare", param_hint="--provider")
    if with_cash_flow and chosen != "akshare":
        raise typer.BadParameter(
            "--with-cash-flow only applies to --provider akshare", param_hint="--with-cash-flow"
        )
    bootstrap = bootstrap_settings()
    if chosen == "akshare":
        code = universe_code(universe)
        run_with_engine(
            bootstrap.database_url,
            lambda: _loaded(
                bootstrap,
                lambda settings: _akshare_backfill(
                    settings,
                    code,
                    day,
                    annual=annual,
                    quarters=quarters,
                    with_cash_flow=with_cash_flow,
                    dry_run=dry_run,
                    show_status=show_status,
                    now=now,
                ),
            ),
        )
        return
    payload = SnapshotBackfillPayload(
        universe_code=universe_code(universe),
        as_of=day,
        part="membership",
        price_years=_years(prices),
        annual_years=annual,
        quarters=quarters,
    )
    run_with_engine(
        bootstrap.database_url,
        lambda: _loaded(
            bootstrap,
            lambda settings: _backfill(
                settings,
                payload,
                dry_run=dry_run,
                show_status=show_status,
                as_of_given=as_of is not None,
                now=now,
            ),
        ),
    )

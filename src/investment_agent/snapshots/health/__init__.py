"""Data health read model (P1-E2): one read-only aggregate of the data layer.

``GET /health/data`` and the ``/app/health`` page render :class:`DataHealth`;
``/dashboard/today`` merges its ``warn`` / ``error`` warnings. Everything is
derived from rows the collectors already write - nothing here calls a
provider or changes the database:

* ``pipeline_run`` - snapshot runs and their completeness metadata
  (:class:`~investment_agent.snapshots.collect.Completeness`), stop reasons,
  AkShare cross-check conflicts (``provider_conflict``) and backfill progress;
* ``universe_membership`` / ``industry_classification`` / ``market_snapshot`` /
  ``financial_snapshot`` / ``dividend_coverage`` - freshness and coverage of
  the current target-pool members;
* ``ifind_call_ledger`` / ``verification_request`` - iFinD budget and queue;
* ``provider_call`` - upstream calls / failures of the free providers in the
  last 24 hours and whether their circuit breaker is open (A2; the breaker's
  in-memory state is passed in by the worker when available, the API process
  falls back to the breaker's own ``circuit`` ledger rows);
* ``report_overdue`` - open overdue reports.

Accumulation (P1 exit metric "快照连续积累 ≥ 8 周且完整率达标"). Live market
snapshots are ``market_snapshot`` runs (baostock, target universe; history
backfill uses ``snapshot_backfill`` and never counts) that finished
``success`` / ``partial`` with at least one listing collected. Per trade date
the best ratio of its runs counts; an ISO week (Monday-Sunday, by trade date)
*qualifies* when it has at least one such trade date and its lowest per-day
ratio is at least ``SNAPSHOT_COMPLETENESS_THRESHOLD``. Walking the weeks from
the first live snapshot to the current week: a qualifying week extends the
streak; a week without trading days (holiday week, per the trading calendar)
and the still-running current week neither count nor break it; any other week
resets the streak to 0. Progress is the streak against :data:`TARGET_WEEKS`.

Queries stay aggregate (one bounded scan of recent runs, per-member index
seeks for freshness) so the endpoint answers in well under a second on a
full-size database.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.snapshots.backfill import backfill_status
from investment_agent.snapshots.collect import PROVIDER_STOP_REASON, QUOTA_STOP_REASON

# Everything listed below is re-exported so that the historical single-module
# path - ``from investment_agent.snapshots.health import X`` - keeps working for
# every name that resolved before the split (the rules and queries moved to
# submodules). ``noqa: F401`` marks the names that this file does not itself
# use as deliberate re-exports, not dead imports.
from investment_agent.snapshots.health.accumulation import (  # noqa: F401
    LiveSnapshot,
    _week_label,
    _week_start,
    compute_accumulation,
    is_trading_day,
    previous_trading_day,
    trading_days_between,
)
from investment_agent.snapshots.health.models import (  # noqa: F401
    _CONFLICT_RUN_SCAN,
    _FINANCIAL_PERIODS_SHOWN,
    _KIND_LABELS,
    _MAX_CHANGES,
    _MAX_CONFLICTS,
    _MAX_LISTED,
    _MAX_MISSING,
    _PROVIDER_WINDOW,
    _RECENT_STOP,
    _RUN_SCAN_LIMIT,
    _TREND_RUNS,
    _VERIFICATION_SCAN,
    _WEEKS_SHOWN,
    COLLECTION_KINDS,
    COUNTED_STATUSES,
    DIRECTORY_STALE_DAYS,
    MISSING_KINDS,
    TARGET_WEEKS,
    TERMINAL_STATUSES,
    Accumulation,
    BackfillHealth,
    BackfillPartHealth,
    BudgetHealth,
    CollectionHealth,
    ConflictItem,
    DataHealth,
    DirectoryHealth,
    DividendFreshness,
    Freshness,
    HealthSettings,
    HealthWarning,
    IndustryHealth,
    IndustrySystemSync,
    Level,
    MarketFreshness,
    MembershipChange,
    MissingTicker,
    OverdueHealth,
    OverdueItem,
    PeriodCoverage,
    ProviderHealth,
    RunSummary,
    TrendPoint,
    UniverseHealth,
    UniverseSyncInfo,
    VerificationHealth,
    WeekStat,
    WeekStatus,
)
from investment_agent.snapshots.health.queries import (  # noqa: F401
    LightRun,
    _completeness,
    _date,
    _float,
    _Group,
    _int,
    _latest_member_bar,
    _str,
    _timestamp,
    _universe_sync_info,
    collection_health,
    current_member_ids,
    directory_freshness,
    dividend_freshness,
    financial_freshness,
    group_collections,
    industry_coverage,
    industry_system_syncs,
    live_market_snapshots,
    market_freshness,
    membership_changes,
    overdue_health,
    provider_health,
    recent_runs,
    run_metadata,
    run_summary,
    verification_health,
)
from investment_agent.snapshots.health.rules import (  # noqa: F401
    _INDUSTRY_SYNC_STALE_DAYS,
    _MARKET_LAG_ERROR,
    _RECENT_CONFLICT,
    _UNIVERSE_SYNC_STALE_DAYS,
    WarningsInput,
    collection_label,
    derive_warnings,
    derive_warnings_from,
)
from investment_agent.snapshots.jobs import (
    FINANCIAL_SNAPSHOT,
    INDUSTRY_SYNC,
    UNIVERSE_SYNC,
)

# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


async def build_data_health(
    session: AsyncSession,
    config: HealthSettings,
    *,
    now: datetime,
    today: date,
) -> DataHealth:
    target = config.universes[0]
    calendar = config.calendar
    runs = await recent_runs(session)
    groups = group_collections(runs)
    last_universe_sync: dict[str, LightRun] = {}
    for run in runs:
        if run.kind == UNIVERSE_SYNC and run.status in COUNTED_STATUSES:
            last_universe_sync.setdefault(run.scope, run)
    industry_runs = [run for run in runs if run.kind == INDUSTRY_SYNC][: _TREND_RUNS * 2]
    akshare_runs = [
        run
        for run in runs
        if run.kind == FINANCIAL_SNAPSHOT
        and run.provider == "akshare"
        and run.status in COUNTED_STATUSES
    ][:_CONFLICT_RUN_SCAN]
    wanted = {run.id for group in groups for run in group.terminal}
    wanted |= {run.id for run in last_universe_sync.values()}
    wanted |= {run.id for run in industry_runs}
    metadata = await run_metadata(session, wanted)

    collections = [collection_health(group, metadata) for group in groups]
    stopped: list[tuple[CollectionHealth, RunSummary]] = []
    for group, item in zip(groups, collections, strict=True):
        for run in group.terminal:
            meta = metadata.get(run.id, {})
            reason = meta.get("stop_reason")
            at = _timestamp(run)
            if reason in (QUOTA_STOP_REASON, PROVIDER_STOP_REASON) and at and now - at <= _RECENT_STOP:
                stopped.append((item, run_summary(run, meta)))
                break

    members_by_universe = {
        code: await current_member_ids(session, code, today) for code in config.universes
    }
    target_members = members_by_universe[target]
    universes = [
        UniverseHealth(
            code=code,
            members=len(members_by_universe[code]),
            last_sync=(
                _universe_sync_info(last_universe_sync[code], metadata.get(last_universe_sync[code].id, {}))
                if code in last_universe_sync
                else None
            ),
            changes=await membership_changes(session, code, today),
        )
        for code in config.universes
    ]
    sw, csrc, unclassified = await industry_coverage(session, target_members, today)
    industry = IndustryHealth(
        universe_code=target,
        members=len(target_members),
        sw2021=sw,
        csrc_fallback=csrc,
        unclassified=unclassified,
        systems=industry_system_syncs(industry_runs, metadata),
    )
    freshness = Freshness(
        market=await market_freshness(
            session, target, target_members, today=today, calendar=calendar
        ),
        financial=await financial_freshness(session, target_members),
        dividend=await dividend_freshness(session, target_members, today.year),
        directory=await directory_freshness(session, today=today),
    )
    accumulation = compute_accumulation(
        await live_market_snapshots(session, target),
        universe_code=target,
        today=today,
        threshold=config.threshold,
        calendar=calendar,
    )
    backfill_as_of, parts = await backfill_status(session, target)
    backfill = BackfillHealth(
        universe_code=target,
        as_of=backfill_as_of,
        parts=[
            BackfillPartHealth(
                part=part.part,
                jobs=part.jobs,
                statuses=dict(sorted(part.statuses.items())),
                done=part.done_symbols,
                total=part.total_symbols,
                failed=len(part.failed_symbols),
                failed_symbols=part.failed_symbols[:_MAX_LISTED],
                detail=part.detail,
            )
            for part in parts
        ],
        akshare_financials=dict(
            sorted(
                Counter(
                    run.status
                    for run in runs
                    if run.kind == FINANCIAL_SNAPSHOT and run.provider == "akshare" and run.backfill
                ).items()
            )
        ),
    )
    verification = await verification_health(
        session,
        now=now,
        cap=config.budget_cap,
        window=config.budget_window,
        akshare_runs=akshare_runs,
    )
    overdue = await overdue_health(session)
    providers = await provider_health(session, now=now, circuit_states=config.circuit_states)
    warnings = derive_warnings_from(
        WarningsInput(
            now=now,
            threshold=config.threshold,
            accumulation=accumulation,
            freshness=freshness,
            universes=universes,
            industry=industry,
            collections=collections,
            stopped_runs=stopped,
            verification=verification,
            overdue=overdue,
            calendar_warnings=config.calendar_warnings,
            providers=providers,
        )
    )
    return DataHealth(
        generated_at=now,
        as_of=today,
        threshold=config.threshold,
        target_universe=target,
        warnings=warnings,
        accumulation=accumulation,
        freshness=freshness,
        universes=universes,
        industry=industry,
        collections=collections,
        backfill=backfill,
        verification=verification,
        overdue=overdue,
        providers=providers,
    )


__all__ = [
    "TARGET_WEEKS",
    "DIRECTORY_STALE_DAYS",
    "DataHealth",
    "DirectoryHealth",
    "HealthSettings",
    "HealthWarning",
    "LiveSnapshot",
    "ProviderHealth",
    "build_data_health",
    "compute_accumulation",
    "derive_warnings",
    "provider_health",
]

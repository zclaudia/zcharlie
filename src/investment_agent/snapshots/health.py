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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.providers.circuit import CircuitStatus
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    UnknownTradingCalendarYearError,
)
from investment_agent.snapshots.backfill import backfill_status
from investment_agent.snapshots.collect import PROVIDER_STOP_REASON, QUOTA_STOP_REASON
from investment_agent.snapshots.jobs import (
    DIVIDEND_SNAPSHOT,
    FINANCIAL_SNAPSHOT,
    INDUSTRY_SYNC,
    INDUSTRY_SYSTEMS,
    MARKET_SNAPSHOT,
    UNIVERSE_SYNC,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import (
    DividendCoverage,
    FinancialSnapshot,
    IndustryClassification,
    MarketSnapshot,
    ReportOverdue,
)
from investment_agent.storage.models.reference import Listing, UniverseMembership
from investment_agent.storage.models.verification import VerificationRequest
from investment_agent.storage.repositories.provider_call import ProviderCallRepository
from investment_agent.verification.budget import budget_status

TARGET_WEEKS = 8
"""P1 exit: snapshots accumulated for at least 8 consecutive weeks."""

COLLECTION_KINDS: tuple[str, ...] = (
    UNIVERSE_SYNC,
    INDUSTRY_SYNC,
    MARKET_SNAPSHOT,
    FINANCIAL_SNAPSHOT,
    DIVIDEND_SNAPSHOT,
)
TERMINAL_STATUSES = frozenset({"success", "partial", "failed"})
COUNTED_STATUSES = ("success", "partial")
MISSING_KINDS: tuple[str, ...] = ("failed", "stopped", "quota", "no_data", "pending")

_RUN_SCAN_LIMIT = 1000
_TREND_RUNS = 8
_MAX_MISSING = 20
_MAX_CHANGES = 10
_MAX_LISTED = 20
_MAX_CONFLICTS = 20
_CONFLICT_RUN_SCAN = 20
_VERIFICATION_SCAN = 50
_WEEKS_SHOWN = 12
_FINANCIAL_PERIODS_SHOWN = 2
_RECENT_STOP = timedelta(hours=24)
_PROVIDER_WINDOW = timedelta(hours=24)
_RECENT_CONFLICT = timedelta(days=7)
_UNIVERSE_SYNC_STALE_DAYS = 14
_INDUSTRY_SYNC_STALE_DAYS = 40
_MARKET_LAG_ERROR = 3

Level = Literal["info", "warn", "error"]
WeekStatus = Literal["qualified", "below_threshold", "missing", "no_trading", "in_progress"]


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #


class HealthWarning(BaseModel):
    code: str
    level: Level
    message: str
    section: str
    """Page section the warning belongs to (anchor on ``/app/health``)."""


class WeekStat(BaseModel):
    week: str
    """ISO week label, e.g. ``2026-W40``."""
    start: date
    trading_days: int
    collected_days: int
    min_ratio: float | None
    status: WeekStatus


class Accumulation(BaseModel):
    universe_code: str
    target_weeks: int
    threshold: float
    streak_weeks: int
    """Consecutive qualifying weeks up to now (the progress value)."""
    weeks_with_snapshots: int
    weeks_meeting_threshold: int
    first_live_trade_date: date | None
    latest_live_trade_date: date | None
    reached: bool
    weeks: list[WeekStat]
    """Most recent weeks first (at most 12)."""


class MarketFreshness(BaseModel):
    universe_code: str
    members: int
    latest_trade_date: date | None
    """Latest baostock raw (unadjusted) bar stored for any current member."""
    members_at_latest: int
    last_trading_day: date | None
    """Latest trading day on or before today (calendar)."""
    expected_trade_date: date | None
    """Latest trading day before today: its bars should be stored by now."""
    lag_trading_days: int | None


class PeriodCoverage(BaseModel):
    report_period: date
    listings: int
    members: int
    coverage: float | None


class DividendFreshness(BaseModel):
    year: int
    covered: int
    members: int
    coverage: float | None


class Freshness(BaseModel):
    market: MarketFreshness
    financial: list[PeriodCoverage]
    """baostock financial periods of the current members, newest first."""
    dividend: DividendFreshness


class MembershipChange(BaseModel):
    date: date
    added: int
    removed: int


class UniverseSyncInfo(BaseModel):
    run_id: int
    status: str
    at: datetime | None
    as_of: str | None
    effective_date: str | None
    members: int | None
    added: list[str]
    removed: list[str]
    added_count: int
    removed_count: int


class UniverseHealth(BaseModel):
    code: str
    members: int
    last_sync: UniverseSyncInfo | None
    changes: list[MembershipChange]


class IndustrySystemSync(BaseModel):
    system: str
    run_id: int | None
    status: str | None
    at: datetime | None
    detail: str | None


class IndustryHealth(BaseModel):
    universe_code: str
    members: int
    sw2021: int
    csrc_fallback: int
    """Members without a current Shenwan class (factor layer falls back to CSRC)."""
    unclassified: int
    systems: list[IndustrySystemSync]


class MissingTicker(BaseModel):
    ticker: str
    kind: str
    reason: str


class RunSummary(BaseModel):
    run_id: int
    status: str
    subject: str | None
    started_at: datetime | None
    finished_at: datetime | None
    ratio: float | None
    expected: int | None
    collected: int | None
    missing_by_kind: dict[str, int]
    missing_total: int
    missing: list[MissingTicker]
    """Top missing tickers (hard failures first), at most 20."""
    stop_reason: str | None
    stop_error: str | None
    error_summary: str | None


class TrendPoint(BaseModel):
    run_id: int
    subject: str | None
    status: str
    finished_at: datetime | None
    ratio: float | None


class CollectionHealth(BaseModel):
    key: str
    kind: str
    provider: str
    part: str | None
    scope: str
    last_run: RunSummary | None
    in_flight: int
    """Newer runs still queued / running."""
    trend: list[TrendPoint]
    """Last terminal runs, oldest first (at most 8)."""


class BackfillPartHealth(BaseModel):
    part: str
    jobs: int
    statuses: dict[str, int]
    done: int
    total: int
    failed: int
    failed_symbols: list[str]
    detail: str


class BackfillHealth(BaseModel):
    universe_code: str
    as_of: date | None
    parts: list[BackfillPartHealth]
    akshare_financials: dict[str, int]
    """Status counts of AkShare financial backfill runs (recent scan)."""


class BudgetHealth(BaseModel):
    cap: int
    window_hours: float
    used: int
    remaining: int
    next_free_at: datetime | None


class ConflictItem(BaseModel):
    source: Literal["verification", "akshare_crosscheck"]
    ticker: str
    fields: list[str]
    report_period: str | None
    at: datetime | None
    run_id: int | None
    request_id: int | None
    backfill: bool = False
    """From a history backfill run (listed, but never raises a warning)."""


class VerificationHealth(BaseModel):
    budget: BudgetHealth
    by_status: dict[str, int]
    open_by_trigger: dict[str, int]
    """pending / deferred / running requests per trigger."""
    conflicts: list[ConflictItem]
    """Newest first, at most 20 (``conflicts_total`` counts all scanned)."""
    conflicts_total: int


class OverdueItem(BaseModel):
    ticker: str
    report_period: date
    deadline: date
    detected_on: date


class OverdueHealth(BaseModel):
    open: int
    items: list[OverdueItem]


class ProviderHealth(BaseModel):
    """Upstream calls of one free provider in the last 24 hours (A2)."""

    provider: str
    calls_24h: int
    failed_24h: int
    skipped_24h: int
    """Calls refused by the open circuit (``skipped_open`` rows)."""
    open_until: datetime | None
    """Circuit open until this moment (``None`` when closed / unknown)."""
    last_error_at: datetime | None
    last_error: str | None
    last_open_at: datetime | None
    """When the circuit last opened (ledger ``circuit`` row or breaker state)."""


class DataHealth(BaseModel):
    generated_at: datetime
    as_of: date
    threshold: float
    target_universe: str
    warnings: list[HealthWarning] = Field(default_factory=list)
    accumulation: Accumulation
    freshness: Freshness
    universes: list[UniverseHealth]
    industry: IndustryHealth
    collections: list[CollectionHealth]
    backfill: BackfillHealth
    verification: VerificationHealth
    overdue: OverdueHealth
    providers: list[ProviderHealth] = Field(default_factory=list)


@dataclass(frozen=True)
class HealthSettings:
    universes: tuple[str, ...]
    threshold: float
    budget_cap: int
    budget_window: timedelta
    calendar: TradingCalendar | None
    calendar_warnings: tuple[HealthWarning, ...] = ()
    circuit_states: tuple[CircuitStatus, ...] = ()
    """The worker's circuit breaker snapshot (``CircuitBreaker.snapshot().values()``)
    when the caller has it; the API process passes nothing and the ledger's
    ``circuit`` rows stand in."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def is_trading_day(calendar: TradingCalendar | None, day: date) -> bool:
    """Calendar answer; weekdays when the year is not reviewed (or no calendar)."""

    if calendar is not None:
        try:
            return calendar.is_trading_day(day)
        except UnknownTradingCalendarYearError:
            pass
    return day.weekday() < 5


def previous_trading_day(calendar: TradingCalendar | None, day: date, *, inclusive: bool) -> date:
    current = day if inclusive else day - timedelta(days=1)
    for _ in range(60):
        if is_trading_day(calendar, current):
            return current
        current -= timedelta(days=1)
    return current


def trading_days_between(
    calendar: TradingCalendar | None, after: date, until: date
) -> int:
    """Trading days in ``(after, until]``."""

    count = 0
    current = after + timedelta(days=1)
    while current <= until:
        if is_trading_day(calendar, current):
            count += 1
        current += timedelta(days=1)
    return count


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _week_label(start: date) -> str:
    year, week, _ = start.isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(frozen=True)
class LightRun:
    id: int
    kind: str
    status: str
    universe_code: str | None
    request: Mapping[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    error_summary: str | None

    @property
    def provider(self) -> str:
        if self.kind in (UNIVERSE_SYNC, INDUSTRY_SYNC):
            return "baostock+akshare" if self.kind == INDUSTRY_SYNC else "baostock"
        return str(self.request.get("provider") or "baostock")

    @property
    def scope(self) -> str:
        if self.kind == INDUSTRY_SYNC:
            codes = self.request.get("universe_codes")
            return "+".join(sorted(map(str, codes))) if isinstance(codes, list) and codes else "ALL"
        if self.universe_code:
            return self.universe_code
        return str(self.request.get("universe_code") or self.request.get("scope") or "-")

    @property
    def part(self) -> str | None:
        if self.kind == FINANCIAL_SNAPSHOT and self.provider == "akshare":
            return str(self.request.get("part") or "summary")
        if self.kind == INDUSTRY_SYNC:
            systems = self.request.get("systems")
            if isinstance(systems, list) and systems:
                return "+".join(map(str, systems))
            return "+".join(INDUSTRY_SYSTEMS)
        return None

    @property
    def backfill(self) -> bool:
        return self.request.get("backfill") is True

    @property
    def subject(self) -> str | None:
        for key in ("trade_date", "report_period", "as_of"):
            value = self.request.get(key)
            if isinstance(value, str):
                return value
        return None

    @property
    def group_key(self) -> str:
        return ":".join(
            part for part in (self.kind, self.provider, self.part or "", self.scope) if part
        )


def _timestamp(run: LightRun) -> datetime | None:
    return as_utc(run.finished_at or run.started_at)


# --------------------------------------------------------------------------- #
# Accumulation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LiveSnapshot:
    trade_date: date
    ratio: float


def compute_accumulation(
    snapshots: Iterable[LiveSnapshot],
    *,
    universe_code: str,
    today: date,
    threshold: float,
    calendar: TradingCalendar | None,
    target_weeks: int = TARGET_WEEKS,
) -> Accumulation:
    """Week counting for the P1 exit metric (see the module docstring)."""

    best: dict[date, float] = {}
    for item in snapshots:
        if item.trade_date > today:
            continue
        best[item.trade_date] = max(best.get(item.trade_date, 0.0), item.ratio)
    if not best:
        return Accumulation(
            universe_code=universe_code,
            target_weeks=target_weeks,
            threshold=threshold,
            streak_weeks=0,
            weeks_with_snapshots=0,
            weeks_meeting_threshold=0,
            first_live_trade_date=None,
            latest_live_trade_date=None,
            reached=False,
            weeks=[],
        )
    by_week: dict[date, list[float]] = {}
    for day, ratio in best.items():
        by_week.setdefault(_week_start(day), []).append(ratio)
    first = min(best)
    current_week = _week_start(today)
    weeks: list[WeekStat] = []
    streak = 0
    meeting = 0
    start = _week_start(first)
    while start <= current_week:
        ratios = by_week.get(start, [])
        trading = sum(
            1 for offset in range(7) if is_trading_day(calendar, start + timedelta(days=offset))
        )
        min_ratio = round(min(ratios), 4) if ratios else None
        status: WeekStatus
        if ratios and min_ratio is not None and min_ratio >= threshold:
            status = "qualified"
            streak += 1
            meeting += 1
        elif start == current_week:
            status = "in_progress"
        elif not ratios and trading == 0:
            status = "no_trading"
        else:
            status = "below_threshold" if ratios else "missing"
            streak = 0
        weeks.append(
            WeekStat(
                week=_week_label(start),
                start=start,
                trading_days=trading,
                collected_days=len(ratios),
                min_ratio=min_ratio,
                status=status,
            )
        )
        start += timedelta(days=7)
    return Accumulation(
        universe_code=universe_code,
        target_weeks=target_weeks,
        threshold=threshold,
        streak_weeks=streak,
        weeks_with_snapshots=len(by_week),
        weeks_meeting_threshold=meeting,
        first_live_trade_date=first,
        latest_live_trade_date=max(best),
        reached=streak >= target_weeks,
        weeks=list(reversed(weeks))[:_WEEKS_SHOWN],
    )


async def live_market_snapshots(
    session: AsyncSession, universe_code: str
) -> list[LiveSnapshot]:
    ratio = PipelineRun.metadata_json[("completeness", "ratio")].as_float()
    collected = PipelineRun.metadata_json[("completeness", "collected")].as_integer()
    stmt = select(PipelineRun.request_json, ratio, collected).where(
        PipelineRun.kind == MARKET_SNAPSHOT,
        PipelineRun.universe_code == universe_code,
        PipelineRun.status.in_(COUNTED_STATUSES),
    )
    snapshots: list[LiveSnapshot] = []
    for request, value, count in (await session.execute(stmt)).all():
        request = request or {}
        if str(request.get("provider") or "baostock") != "baostock":
            continue
        if request.get("scope", "universe") != "universe":
            continue
        day = _date(request.get("trade_date"))
        if day is None or value is None or not count:
            continue
        snapshots.append(LiveSnapshot(day, float(value)))
    return snapshots


# --------------------------------------------------------------------------- #
# Members, freshness, universes, industry
# --------------------------------------------------------------------------- #


async def current_member_ids(session: AsyncSession, universe_code: str, today: date) -> list[int]:
    stmt = select(UniverseMembership.listing_id).where(
        UniverseMembership.universe_code == universe_code,
        UniverseMembership.valid_from <= today,
        or_(UniverseMembership.valid_to.is_(None), UniverseMembership.valid_to > today),
    )
    return sorted(set((await session.execute(stmt)).scalars().all()))


async def market_freshness(
    session: AsyncSession,
    universe_code: str,
    member_ids: Sequence[int],
    *,
    today: date,
    calendar: TradingCalendar | None,
) -> MarketFreshness:
    latest: date | None = None
    at_latest = 0
    if member_ids:
        inner = (
            select(MarketSnapshot.trade_date)
            .where(
                MarketSnapshot.listing_id == UniverseMembership.listing_id,
                MarketSnapshot.provider == "baostock",
                MarketSnapshot.adjust == "none",
            )
            .order_by(MarketSnapshot.trade_date.desc())
            .limit(1)
            .correlate(UniverseMembership)
            .scalar_subquery()
        )
        stmt = select(func.max(inner)).where(
            UniverseMembership.universe_code == universe_code,
            UniverseMembership.listing_id.in_(member_ids),
        )
        latest = _date((await session.execute(stmt)).scalar_one_or_none())
        if latest is not None:
            at_latest = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(MarketSnapshot.listing_id))).where(
                            MarketSnapshot.listing_id.in_(member_ids),
                            MarketSnapshot.trade_date == latest,
                            MarketSnapshot.provider == "baostock",
                            MarketSnapshot.adjust == "none",
                        )
                    )
                ).scalar_one()
            )
    last_trading = previous_trading_day(calendar, today, inclusive=True)
    expected = previous_trading_day(calendar, today, inclusive=False)
    lag = None
    if latest is not None:
        lag = trading_days_between(calendar, latest, expected) if latest < expected else 0
    return MarketFreshness(
        universe_code=universe_code,
        members=len(member_ids),
        latest_trade_date=latest,
        members_at_latest=at_latest,
        last_trading_day=last_trading,
        expected_trade_date=expected,
        lag_trading_days=lag,
    )


async def financial_freshness(
    session: AsyncSession, member_ids: Sequence[int]
) -> list[PeriodCoverage]:
    if not member_ids:
        return []
    stmt = (
        select(FinancialSnapshot.report_period, func.count(func.distinct(FinancialSnapshot.listing_id)))
        .where(
            FinancialSnapshot.listing_id.in_(member_ids),
            FinancialSnapshot.provider == "baostock",
        )
        .group_by(FinancialSnapshot.report_period)
        .order_by(FinancialSnapshot.report_period.desc())
        .limit(_FINANCIAL_PERIODS_SHOWN)
    )
    members = len(member_ids)
    return [
        PeriodCoverage(
            report_period=period,
            listings=int(count),
            members=members,
            coverage=round(int(count) / members, 4),
        )
        for period, count in (await session.execute(stmt)).all()
    ]


async def dividend_freshness(
    session: AsyncSession, member_ids: Sequence[int], year: int
) -> DividendFreshness:
    covered = 0
    if member_ids:
        covered = int(
            (
                await session.execute(
                    select(func.count(func.distinct(DividendCoverage.listing_id))).where(
                        DividendCoverage.listing_id.in_(member_ids),
                        DividendCoverage.provider == "baostock",
                        DividendCoverage.year == year,
                    )
                )
            ).scalar_one()
        )
    members = len(member_ids)
    return DividendFreshness(
        year=year,
        covered=covered,
        members=members,
        coverage=round(covered / members, 4) if members else None,
    )


async def membership_changes(
    session: AsyncSession, universe_code: str, today: date
) -> list[MembershipChange]:
    added_stmt = (
        select(UniverseMembership.valid_from, func.count())
        .where(UniverseMembership.universe_code == universe_code, UniverseMembership.valid_from <= today)
        .group_by(UniverseMembership.valid_from)
        .order_by(UniverseMembership.valid_from.desc())
        .limit(_MAX_CHANGES)
    )
    removed_stmt = (
        select(UniverseMembership.valid_to, func.count())
        .where(
            UniverseMembership.universe_code == universe_code,
            UniverseMembership.valid_to.is_not(None),
            UniverseMembership.valid_to <= today,
        )
        .group_by(UniverseMembership.valid_to)
        .order_by(UniverseMembership.valid_to.desc())
        .limit(_MAX_CHANGES)
    )
    changes: dict[date, list[int]] = {}
    for day, count in (await session.execute(added_stmt)).all():
        changes.setdefault(day, [0, 0])[0] = int(count)
    for day, count in (await session.execute(removed_stmt)).all():
        if day is not None:
            changes.setdefault(day, [0, 0])[1] = int(count)
    return [
        MembershipChange(date=day, added=counts[0], removed=counts[1])
        for day, counts in sorted(changes.items(), reverse=True)[:_MAX_CHANGES]
    ]


def _universe_sync_info(run: LightRun, metadata: Mapping[str, Any]) -> UniverseSyncInfo:
    added = [str(item) for item in metadata.get("added") or [] if isinstance(item, str)]
    removed = [str(item) for item in metadata.get("removed") or [] if isinstance(item, str)]
    return UniverseSyncInfo(
        run_id=run.id,
        status=run.status,
        at=_timestamp(run),
        as_of=_str(run.request.get("as_of")),
        effective_date=_str(metadata.get("effective_date")),
        members=_int(metadata.get("members")),
        added=added[:_MAX_LISTED],
        removed=removed[:_MAX_LISTED],
        added_count=len(added),
        removed_count=len(removed),
    )


async def industry_coverage(
    session: AsyncSession, member_ids: Sequence[int], today: date
) -> tuple[int, int, int]:
    """(with sw2021, CSRC only, neither) among the current members."""

    if not member_ids:
        return 0, 0, 0
    stmt = (
        select(IndustryClassification.listing_id, IndustryClassification.system)
        .where(
            IndustryClassification.listing_id.in_(member_ids),
            IndustryClassification.valid_from <= today,
            or_(
                IndustryClassification.valid_to.is_(None),
                IndustryClassification.valid_to > today,
            ),
        )
        .distinct()
    )
    systems: dict[int, set[str]] = {}
    for listing_id, system in (await session.execute(stmt)).all():
        systems.setdefault(listing_id, set()).add(system)
    sw = sum(1 for listing_id in member_ids if "sw2021" in systems.get(listing_id, set()))
    csrc = sum(
        1
        for listing_id in member_ids
        if "sw2021" not in systems.get(listing_id, set())
        and "csrc" in systems.get(listing_id, set())
    )
    return sw, csrc, len(member_ids) - sw - csrc


def industry_system_syncs(
    runs: Sequence[LightRun], metadata: Mapping[int, Mapping[str, Any]]
) -> list[IndustrySystemSync]:
    """Latest terminal ``industry_sync`` outcome per system (runs newest first)."""

    result: dict[str, IndustrySystemSync] = {}
    for run in runs:
        if run.kind != INDUSTRY_SYNC or run.status not in TERMINAL_STATUSES:
            continue
        meta = metadata.get(run.id, {})
        systems = run.part.split("+") if run.part else list(INDUSTRY_SYSTEMS)
        failed_raw = meta.get("failed_systems")
        failed = {str(item) for item in failed_raw} if isinstance(failed_raw, list) else set()
        errors = meta.get("errors") if isinstance(meta.get("errors"), dict) else {}
        for system in systems:
            if system in result:
                continue
            ran = "applied_entries" in meta if system == "csrc" else "sw2021" in meta
            if run.status != "failed" and system not in failed and not ran:
                continue  # an older run that did not sync this system
            status = "failed" if run.status == "failed" or system in failed else "success"
            detail: str | None
            if status == "failed":
                detail = _str(errors.get(system)) if isinstance(errors, dict) else None
                detail = (detail or run.error_summary or "")[:200] or None
            elif system == "csrc":
                detail = (
                    f"applied {meta.get('applied_entries')}, changed {meta.get('changed')}"
                    if "applied_entries" in meta
                    else None
                )
            else:
                sw = meta.get("sw2021")
                detail = (
                    ", ".join(f"{key} {value}" for key, value in sw.items() if isinstance(value, int))[:200]
                    or None
                    if isinstance(sw, dict)
                    else None
                )
            result[system] = IndustrySystemSync(
                system=system, run_id=run.id, status=status, at=_timestamp(run), detail=detail
            )
    return [
        result.get(system)
        or IndustrySystemSync(system=system, run_id=None, status=None, at=None, detail=None)
        for system in INDUSTRY_SYSTEMS
    ]


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


async def recent_runs(session: AsyncSession) -> list[LightRun]:
    """Newest first; light columns only (metadata is fetched per selected run)."""

    stmt = (
        select(
            PipelineRun.id,
            PipelineRun.kind,
            PipelineRun.status,
            PipelineRun.universe_code,
            PipelineRun.request_json,
            PipelineRun.started_at,
            PipelineRun.finished_at,
            PipelineRun.error_summary,
        )
        .where(PipelineRun.kind.in_(COLLECTION_KINDS))
        .order_by(PipelineRun.id.desc())
        .limit(_RUN_SCAN_LIMIT)
    )
    return [
        LightRun(
            id=row.id,
            kind=row.kind,
            status=row.status,
            universe_code=row.universe_code,
            request=row.request_json or {},
            started_at=row.started_at,
            finished_at=row.finished_at,
            error_summary=row.error_summary,
        )
        for row in (await session.execute(stmt)).all()
    ]


async def run_metadata(
    session: AsyncSession, run_ids: Iterable[int]
) -> dict[int, Mapping[str, Any]]:
    ids = sorted(set(run_ids))
    if not ids:
        return {}
    stmt = select(PipelineRun.id, PipelineRun.metadata_json).where(PipelineRun.id.in_(ids))
    return {
        run_id: (metadata if isinstance(metadata, dict) else {})
        for run_id, metadata in (await session.execute(stmt)).all()
    }


def _completeness(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = metadata.get("completeness")
    return value if isinstance(value, dict) else None


def run_summary(run: LightRun, metadata: Mapping[str, Any]) -> RunSummary:
    completeness = _completeness(metadata) or {}
    missing_raw = completeness.get("missing")
    missing: list[MissingTicker] = []
    counts: Counter[str] = Counter()
    if isinstance(missing_raw, dict):
        for ticker, info in missing_raw.items():
            if not isinstance(info, dict):
                continue
            kind = str(info.get("kind") or "failed")
            counts[kind] += 1
            missing.append(MissingTicker(ticker=str(ticker), kind=kind, reason=str(info.get("reason") or "")[:300]))
    order = {kind: index for index, kind in enumerate(MISSING_KINDS)}
    missing.sort(key=lambda item: (order.get(item.kind, len(order)), item.ticker))
    for kind in MISSING_KINDS:
        count = _int(completeness.get(f"{kind}_count"))
        if count is not None and count > counts.get(kind, 0):
            counts[kind] = count
    missing_total = _int(completeness.get("missing_count"))
    return RunSummary(
        run_id=run.id,
        status=run.status,
        subject=run.subject,
        started_at=as_utc(run.started_at),
        finished_at=as_utc(run.finished_at),
        ratio=_float(completeness.get("ratio")),
        expected=_int(completeness.get("expected")),
        collected=_int(completeness.get("collected")),
        missing_by_kind={kind: counts[kind] for kind in MISSING_KINDS if counts.get(kind)},
        missing_total=missing_total if missing_total is not None else len(missing),
        missing=missing[:_MAX_MISSING],
        stop_reason=_str(metadata.get("stop_reason")),
        stop_error=_str(metadata.get("stop_error")),
        error_summary=(run.error_summary or None) and str(run.error_summary)[:300],
    )


@dataclass
class _Group:
    key: str
    sample: LightRun
    terminal: list[LightRun]
    in_flight: int = 0


def group_collections(runs: Sequence[LightRun]) -> list[_Group]:
    """Live collection runs grouped by kind / provider / part / scope (backfill excluded)."""

    groups: dict[str, _Group] = {}
    for run in runs:  # newest first
        if run.backfill:
            continue
        group = groups.get(run.group_key)
        if group is None:
            group = groups[run.group_key] = _Group(run.group_key, run, [])
        if run.status in TERMINAL_STATUSES:
            if len(group.terminal) < _TREND_RUNS:
                group.terminal.append(run)
        elif not group.terminal:
            group.in_flight += 1
    order = {kind: index for index, kind in enumerate(COLLECTION_KINDS)}
    return sorted(groups.values(), key=lambda g: (order.get(g.sample.kind, 99), g.key))


def collection_health(
    group: _Group, metadata: Mapping[int, Mapping[str, Any]]
) -> CollectionHealth:
    last = group.terminal[0] if group.terminal else None
    trend = [
        TrendPoint(
            run_id=run.id,
            subject=run.subject,
            status=run.status,
            finished_at=_timestamp(run),
            ratio=_float((_completeness(metadata.get(run.id, {})) or {}).get("ratio")),
        )
        for run in reversed(group.terminal)
    ]
    return CollectionHealth(
        key=group.key,
        kind=group.sample.kind,
        provider=group.sample.provider,
        part=group.sample.part,
        scope=group.sample.scope,
        last_run=run_summary(last, metadata.get(last.id, {})) if last is not None else None,
        in_flight=group.in_flight,
        trend=trend,
    )


# --------------------------------------------------------------------------- #
# Verification, conflicts, overdue
# --------------------------------------------------------------------------- #


async def verification_health(
    session: AsyncSession,
    *,
    now: datetime,
    cap: int,
    window: timedelta,
    akshare_runs: Sequence[LightRun],
) -> VerificationHealth:
    status = await budget_status(session, now=now, cap=cap, window=window)
    by_status = {
        str(name): int(count)
        for name, count in (
            await session.execute(
                select(VerificationRequest.status, func.count()).group_by(VerificationRequest.status)
            )
        ).all()
    }
    open_by_trigger = {
        str(name): int(count)
        for name, count in (
            await session.execute(
                select(VerificationRequest.trigger, func.count())
                .where(VerificationRequest.status.in_(("pending", "deferred", "running")))
                .group_by(VerificationRequest.trigger)
            )
        ).all()
    }
    conflicts: list[ConflictItem] = []
    done = (
        await session.execute(
            select(
                VerificationRequest.id,
                VerificationRequest.ticker,
                VerificationRequest.result,
                VerificationRequest.finished_at,
                VerificationRequest.run_id,
                VerificationRequest.report_period,
            )
            .where(VerificationRequest.status == "done")
            .order_by(VerificationRequest.finished_at.desc(), VerificationRequest.id.desc())
            .limit(_VERIFICATION_SCAN)
        )
    ).all()
    for request_id, ticker, result, finished_at, run_id, report_period in done:
        names = result.get("conflicts") if isinstance(result, dict) else None
        if not isinstance(names, list) or not names:
            continue
        conflicts.append(
            ConflictItem(
                source="verification",
                ticker=ticker,
                fields=sorted(str(name) for name in names),
                report_period=_str(result.get("report_period"))
                or (report_period.isoformat() if report_period else None),
                at=as_utc(finished_at),
                run_id=run_id,
                request_id=request_id,
            )
        )
    if akshare_runs:
        stmt = select(
            PipelineRun.id, PipelineRun.metadata_json["provider_conflict"]
        ).where(PipelineRun.id.in_([run.id for run in akshare_runs]))
        by_id = {run.id: run for run in akshare_runs}
        for run_id, check in (await session.execute(stmt)).all():
            items = check.get("items") if isinstance(check, dict) else None
            if not isinstance(items, dict):
                continue
            run = by_id[run_id]
            for ticker, fields in items.items():
                conflicts.append(
                    ConflictItem(
                        source="akshare_crosscheck",
                        ticker=str(ticker),
                        fields=sorted(map(str, fields)) if isinstance(fields, dict) else [],
                        report_period=_str(run.request.get("report_period")),
                        at=_timestamp(run),
                        run_id=run_id,
                        request_id=None,
                        backfill=run.backfill,
                    )
                )
    epoch = datetime.min.replace(tzinfo=UTC)
    conflicts.sort(key=lambda item: (not item.backfill, item.at or epoch, item.ticker), reverse=True)
    next_free = status.available_at(1) if status.remaining == 0 else None
    return VerificationHealth(
        budget=BudgetHealth(
            cap=status.cap,
            window_hours=status.window.total_seconds() / 3600,
            used=status.used,
            remaining=status.remaining,
            next_free_at=next_free,
        ),
        by_status=dict(sorted(by_status.items())),
        open_by_trigger=dict(sorted(open_by_trigger.items())),
        conflicts=conflicts[:_MAX_CONFLICTS],
        conflicts_total=len(conflicts),
    )


async def overdue_health(session: AsyncSession) -> OverdueHealth:
    condition = ReportOverdue.resolved_on.is_(None)
    total = int(
        (
            await session.execute(select(func.count()).select_from(ReportOverdue).where(condition))
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(ReportOverdue, Listing.ticker, Listing.market_code)
            .join(Listing, Listing.id == ReportOverdue.listing_id)
            .where(condition)
            .order_by(ReportOverdue.report_period.desc(), Listing.ticker)
            .limit(50)
        )
    ).all()
    return OverdueHealth(
        open=total,
        items=[
            OverdueItem(
                ticker=format_listing_ticker(ticker=ticker, market_code=market_code),
                report_period=row.report_period,
                deadline=row.deadline,
                detected_on=row.detected_on,
            )
            for row, ticker, market_code in rows
        ],
    )


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #

_KIND_LABELS = {
    UNIVERSE_SYNC: "成分同步",
    INDUSTRY_SYNC: "行业同步",
    MARKET_SNAPSHOT: "日线快照",
    FINANCIAL_SNAPSHOT: "财报快照",
    DIVIDEND_SNAPSHOT: "分红快照",
}


async def provider_health(
    session: AsyncSession,
    *,
    now: datetime,
    circuit_states: Sequence[CircuitStatus] = (),
) -> list[ProviderHealth]:
    """24-hour ledger counts per provider merged with the breaker state (A2)."""

    stats = {
        stat.provider: stat
        for stat in await ProviderCallRepository(session).summary(now - _PROVIDER_WINDOW)
    }
    by_provider: dict[str, list[CircuitStatus]] = {}
    for status in circuit_states:
        by_provider.setdefault(status.provider, []).append(status)
    items: list[ProviderHealth] = []
    for provider in sorted(set(stats) | set(by_provider)):
        stat = stats.get(provider)
        states = by_provider.get(provider, [])
        open_entries = [state for state in states if state.is_open(now)]
        open_until = max(state.open_until for state in open_entries) if open_entries else None  # type: ignore[type-var]
        last_open_at = (
            max((state.last_failure_at for state in open_entries if state.last_failure_at), default=None)
            if open_entries
            else None
        )
        failed_states = [state for state in states if state.last_failure_at is not None]
        newest_state = (
            max(failed_states, key=lambda state: state.last_failure_at or now)
            if failed_states
            else None
        )
        if open_until is None and stat is not None and stat.last_open_until is not None:
            # No breaker in this process: trust the ledger's last "opened" row.
            if stat.last_open_until > now:
                open_until = stat.last_open_until
            last_open_at = stat.last_open_at
        last_error_at: datetime | None
        last_error: str | None
        if newest_state is not None and (
            stat is None
            or stat.last_error_at is None
            or (newest_state.last_failure_at or now) >= stat.last_error_at
        ):
            last_error_at = newest_state.last_failure_at
            last_error = f"{newest_state.endpoint}: {newest_state.last_error}"
        else:
            last_error_at = stat.last_error_at if stat is not None else None
            last_error = stat.last_error if stat is not None else None
        items.append(
            ProviderHealth(
                provider=provider,
                calls_24h=stat.calls if stat is not None else 0,
                failed_24h=stat.failed if stat is not None else 0,
                skipped_24h=stat.skipped if stat is not None else 0,
                open_until=open_until,
                last_error_at=last_error_at,
                last_error=last_error,
                last_open_at=last_open_at,
            )
        )
    return items


def collection_label(item: CollectionHealth) -> str:
    label = _KIND_LABELS.get(item.kind, item.kind)
    extra = " ".join(part for part in (item.provider, item.part, item.scope) if part)
    return f"{label}（{extra}）"


def derive_warnings(
    *,
    now: datetime,
    threshold: float,
    accumulation: Accumulation,
    freshness: Freshness,
    universes: Sequence[UniverseHealth],
    industry: IndustryHealth,
    collections: Sequence[CollectionHealth],
    stopped_runs: Sequence[tuple[CollectionHealth, RunSummary]],
    verification: VerificationHealth,
    overdue: OverdueHealth,
    calendar_warnings: Sequence[HealthWarning] = (),
    providers: Sequence[ProviderHealth] = (),
) -> list[HealthWarning]:
    """Rules (see README "数据健康"); ``error`` > ``warn`` > ``info`` in the output order."""

    warnings: list[HealthWarning] = list(calendar_warnings)
    market = freshness.market
    if market.latest_trade_date is None:
        warnings.append(
            HealthWarning(
                code="market_never_collected",
                level="info",
                message=f"{market.universe_code} 尚无日线快照（baostock 不复权）。",
                section="freshness",
            )
        )
    elif market.lag_trading_days:
        warnings.append(
            HealthWarning(
                code="market_stale",
                level="error" if market.lag_trading_days >= _MARKET_LAG_ERROR else "warn",
                message=(
                    f"{market.universe_code} 最新日线 {market.latest_trade_date.isoformat()}，"
                    f"落后应有交易日 {market.expected_trade_date} 共 {market.lag_trading_days} 个交易日。"
                ),
                section="freshness",
            )
        )
    if (
        market.latest_trade_date is not None
        and market.members
        and market.members_at_latest < market.members * threshold
    ):
        warnings.append(
            HealthWarning(
                code="market_coverage_low",
                level="warn",
                message=(
                    f"{market.latest_trade_date.isoformat()} 日线只覆盖 "
                    f"{market.members_at_latest}/{market.members} 只当前成分股。"
                ),
                section="freshness",
            )
        )

    for item in collections:
        run = item.last_run
        if run is None:
            continue
        label = collection_label(item)
        if run.status == "failed":
            warnings.append(
                HealthWarning(
                    code="collection_failed",
                    level="error",
                    message=f"{label} 最近一次运行 #{run.run_id} 失败：{run.error_summary or '无摘要'}",
                    section="collections",
                )
            )
        elif run.ratio is not None and run.ratio < threshold:
            warnings.append(
                HealthWarning(
                    code="completeness_low",
                    level="warn",
                    message=(
                        f"{label} 最近一次运行 #{run.run_id}（{run.subject or '-'}）完整率 "
                        f"{run.ratio:.1%}，低于阈值 {threshold:.0%}。"
                    ),
                    section="collections",
                )
            )
    for item, run in stopped_runs:
        reason = "iFinD 额度耗尽" if run.stop_reason == QUOTA_STOP_REASON else "数据源连续失败"
        warnings.append(
            HealthWarning(
                code="quota_stop" if run.stop_reason == QUOTA_STOP_REASON else "provider_stop",
                level="warn",
                message=(
                    f"{collection_label(item)} 运行 #{run.run_id} 24 小时内因{reason}提前停止，"
                    f"未采 {run.missing_by_kind.get('quota', 0) + run.missing_by_kind.get('stopped', 0)} 只；"
                    "下次调度会续采。"
                ),
                section="collections",
            )
        )

    for universe in universes:
        sync = universe.last_sync
        if sync is None or sync.at is None:
            warnings.append(
                HealthWarning(
                    code="universe_never_synced",
                    level="info",
                    message=f"{universe.code} 尚无成分同步记录。",
                    section="universes",
                )
            )
        elif now - sync.at > timedelta(days=_UNIVERSE_SYNC_STALE_DAYS):
            warnings.append(
                HealthWarning(
                    code="universe_sync_stale",
                    level="warn",
                    message=(
                        f"{universe.code} 最近一次成分同步是 {sync.at.date().isoformat()}，"
                        f"超过 {_UNIVERSE_SYNC_STALE_DAYS} 天（应每周一同步）。"
                    ),
                    section="universes",
                )
            )
    for system in industry.systems:
        if system.at is not None and now - system.at > timedelta(days=_INDUSTRY_SYNC_STALE_DAYS):
            warnings.append(
                HealthWarning(
                    code="industry_sync_stale",
                    level="info",
                    message=(
                        f"{system.system} 行业最近一次同步是 {system.at.date().isoformat()}，"
                        f"超过 {_INDUSTRY_SYNC_STALE_DAYS} 天（应每月同步）。"
                    ),
                    section="industry",
                )
            )

    weeks = accumulation.weeks
    completed = [week for week in weeks if week.status not in ("in_progress", "no_trading")]
    if completed and completed[0].status in ("missing", "below_threshold"):
        week = completed[0]
        warnings.append(
            HealthWarning(
                code="accumulation_reset",
                level="warn",
                message=(
                    f"{week.week} 周"
                    + ("没有日线快照" if week.status == "missing" else f"完整率 {week.min_ratio:.1%} 未达标")
                    + f"，快照连续积累周数归零（目标 {accumulation.target_weeks} 周）。"
                ),
                section="accumulation",
            )
        )

    budget = verification.budget
    if budget.remaining == 0:
        when = budget.next_free_at.isoformat(timespec="minutes") if budget.next_free_at else "-"
        warnings.append(
            HealthWarning(
                code="budget_exhausted",
                level="warn",
                message=(
                    f"iFinD 预算已用尽（{budget.used}/{budget.cap} 次 / {budget.window_hours:g} 小时），"
                    f"核对请求将延后，最早 {when} 恢复。"
                ),
                section="verification",
            )
        )
    recent = [
        item
        for item in verification.conflicts
        if not item.backfill and item.at is not None and now - item.at <= _RECENT_CONFLICT
    ]
    if recent:
        tickers = sorted({item.ticker for item in recent})
        warnings.append(
            HealthWarning(
                code="provider_conflict",
                level="warn",
                message=(
                    f"近 7 天有 {len(tickers)} 只股票存在数据源字段冲突："
                    + "、".join(tickers[:5])
                    + ("等。" if len(tickers) > 5 else "。")
                ),
                section="verification",
            )
        )
    if overdue.open:
        warnings.append(
            HealthWarning(
                code="report_overdue",
                level="warn",
                message=f"{overdue.open} 份定期报告逾期未披露。",
                section="overdue",
            )
        )
    for upstream in providers:
        if upstream.open_until is not None and upstream.open_until > now:
            until = upstream.open_until.astimezone(SHANGHAI_TZ).strftime("%H:%M:%S")
            warnings.append(
                HealthWarning(
                    code="provider_circuit_open",
                    level="warn",
                    message=(
                        f"{upstream.provider} 熔断中，至 {until}（24 小时内调用 "
                        f"{upstream.calls_24h} 次，失败 {upstream.failed_24h} 次）；"
                        "窗口内的采集任务会直接 partial，窗口过后自动试探。"
                    ),
                    section="providers",
                )
            )
    rank = {"error": 0, "warn": 1, "info": 2}
    return sorted(warnings, key=lambda item: rank[item.level])


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
    warnings = derive_warnings(
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
    "DataHealth",
    "HealthSettings",
    "HealthWarning",
    "LiveSnapshot",
    "ProviderHealth",
    "build_data_health",
    "compute_accumulation",
    "derive_warnings",
    "provider_health",
]

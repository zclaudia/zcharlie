"""Response models and constant tables of the data health read model.

Pure contracts: the pydantic response shapes, :class:`HealthSettings` and the
kind / limit constants. No session and no SQL - every query lives in
:mod:`investment_agent.snapshots.health.queries` (the package docstring
describes the read path as a whole).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from investment_agent.domain.pipeline_status import TERMINAL_RUN_STATUSES
from investment_agent.providers.circuit import CircuitStatus
from investment_agent.scheduler.trading_calendar import TradingCalendar
from investment_agent.snapshots.jobs import (
    CLOSE_QUOTE,
    DIVIDEND_SNAPSHOT,
    EARNINGS_EVENT,
    FINANCIAL_SNAPSHOT,
    INDUSTRY_SYNC,
    MARKET_SNAPSHOT,
    UNIVERSE_SYNC,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

TARGET_WEEKS = 8
"""P1 exit: snapshots accumulated for at least 8 consecutive weeks."""

COLLECTION_KINDS: tuple[str, ...] = (
    UNIVERSE_SYNC,
    INDUSTRY_SYNC,
    MARKET_SNAPSHOT,
    CLOSE_QUOTE,
    FINANCIAL_SNAPSHOT,
    DIVIDEND_SNAPSHOT,
    EARNINGS_EVENT,
)
TERMINAL_STATUSES = TERMINAL_RUN_STATUSES
"""Re-export of the single home (see :mod:`investment_agent.domain.pipeline_status`)."""
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
DIRECTORY_STALE_DAYS = 14
"""The directory is refreshed weekly; older than two weeks is a warning."""

Level = Literal["info", "warn", "error"]
WeekStatus = Literal["qualified", "below_threshold", "missing", "no_trading", "in_progress"]

_KIND_LABELS = {
    UNIVERSE_SYNC: "成分同步",
    INDUSTRY_SYNC: "行业同步",
    MARKET_SNAPSHOT: "日线快照",
    CLOSE_QUOTE: "收盘快照",
    FINANCIAL_SNAPSHOT: "财报快照",
    DIVIDEND_SNAPSHOT: "分红快照",
    EARNINGS_EVENT: "财报事件",
}


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
    latest_close_quote_date: date | None = None
    """Latest Tencent close quote (mrd-intake B2) stored for any current member;
    informational only - ``market_stale`` still judges the baostock bars."""
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


class DirectoryHealth(BaseModel):
    """Local stock directory (mrd-intake D4) behind ``GET /stocks/search``."""

    synced_on: date | None
    """Trading day of the last ``query_all_stock`` answer; ``None`` before the first sync."""
    total: int
    """Rows in ``stock_directory`` (``delisted`` ones included)."""
    age_days: int | None
    """``today - synced_on``; a warning past :data:`DIRECTORY_STALE_DAYS`."""


class Freshness(BaseModel):
    market: MarketFreshness
    financial: list[PeriodCoverage]
    """baostock financial periods of the current members, newest first."""
    dividend: DividendFreshness
    directory: DirectoryHealth = Field(
        default_factory=lambda: DirectoryHealth(synced_on=None, total=0, age_days=None)
    )


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

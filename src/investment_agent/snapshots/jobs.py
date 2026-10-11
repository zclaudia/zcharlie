"""Snapshot job kinds, request payloads, idempotency keys and enqueueing.

Market and financial payloads carry a ``provider`` and a ``scope`` (decision
D6): baostock serves the target pool (``scope="universe"`` + a
``universe_code``) and may also serve the watchlist; iFinD serves only the
watchlist (``scope="watchlist"``, enabled watchlist items), so its limited
gateway quota is never spent on breadth collection. Payloads persisted before
P1-A7 (no provider / scope) validate as baostock + universe.

AkShare / Eastmoney (P1-A11) only collects financials: ``provider="akshare"``
with a ``part`` - ``summary`` (whole-market period summaries) or
``cash_flow`` (per-stock cash-flow statements, also storing
``extra_periods`` from the same answer); ``backfill=True`` marks history
backfill runs (rows ``is_backfill``; scope = every member since the period).

Dividends (F2 / Q2): ``dividend_snapshot`` collects baostock dividend events
(``query_dividend_data``, by ex-date year) for the payload's scope; the
weekly financial schedule enqueues it for the current and previous year.

Close quotes (mrd-intake B2): ``close_quote`` stores the same-day close of the
target pool, the watchlist, open candidates and holdings from the Tencent
batch quote feed (``provider="tencent"``) at 15:10, hours before the baostock
bars; readers prefer baostock once it is there (decision D2). The 16:00
``slot="retry"`` run re-collects what was still ``pending`` at 15:10.

Earnings events (mrd-intake C1 / C2): ``earnings_event`` collects Eastmoney
(``provider="akshare"``) 业绩预告 (``part="forecast"``) and 预约披露时间
(``part="schedule"``) of one ``report_period`` for the target pool plus the
user's pools, and 主营构成 (``part="segments"``) of the user's pools only
(per-stock calls); scheduled every trading day at 19:00 inside the
disclosure-season months (segments once a week).
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.providers.baostock_reference import IndexCode
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.universe import UniverseRepository

UNIVERSE_SYNC = "universe_sync"
INDUSTRY_SYNC = "industry_sync"
MARKET_SNAPSHOT = "market_snapshot"
FINANCIAL_SNAPSHOT = "financial_snapshot"
SNAPSHOT_BACKFILL = "snapshot_backfill"
DIVIDEND_SNAPSHOT = "dividend_snapshot"
CLOSE_QUOTE = "close_quote"
EARNINGS_EVENT = "earnings_event"
CALENDAR_PROBE = "calendar_probe"
SNAPSHOT_KINDS: tuple[str, ...] = (
    UNIVERSE_SYNC,
    INDUSTRY_SYNC,
    MARKET_SNAPSHOT,
    FINANCIAL_SNAPSHOT,
    SNAPSHOT_BACKFILL,
    DIVIDEND_SNAPSHOT,
    CLOSE_QUOTE,
    EARNINGS_EVENT,
    CALENDAR_PROBE,
)

SnapshotProvider = Literal["baostock", "ifind", "akshare", "tencent"]
SnapshotScope = Literal["universe", "watchlist"]
SNAPSHOT_PROVIDERS: tuple[SnapshotProvider, ...] = ("baostock", "ifind", "akshare", "tencent")
CloseQuoteSlot = Literal["close", "retry"]
CLOSE_QUOTE_SLOTS: tuple[CloseQuoteSlot, ...] = ("close", "retry")
AkshareFinancialPart = Literal["summary", "cash_flow"]
AKSHARE_FINANCIAL_PARTS: tuple[AkshareFinancialPart, ...] = ("summary", "cash_flow")
EarningsEventPart = Literal["forecast", "schedule", "segments"]
EARNINGS_EVENT_PARTS: tuple[EarningsEventPart, ...] = ("forecast", "schedule", "segments")
SNAPSHOT_SCOPES: tuple[SnapshotScope, ...] = ("universe", "watchlist")
WATCHLIST_SCOPE_LABEL = "watchlist"

PeriodKind = Literal["annual", "semi", "q1", "q3"]
_PERIOD_KINDS: dict[tuple[int, int], PeriodKind] = {
    (3, 31): "q1",
    (6, 30): "semi",
    (9, 30): "q3",
    (12, 31): "annual",
}


def period_kind(report_period: date) -> PeriodKind:
    """``2026-06-30`` -> ``semi``; raises for a non quarter-end date."""

    kind = _PERIOD_KINDS.get((report_period.month, report_period.day))
    if kind is None:
        raise ValueError(
            f"report period must be a quarter end (03-31/06-30/09-30/12-31): "
            f"{report_period.isoformat()}"
        )
    return kind


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseSyncPayload(_Payload):
    universe_code: IndexCode
    as_of: date


IndustrySystem = Literal["csrc", "sw2021"]
INDUSTRY_SYSTEMS: tuple[IndustrySystem, ...] = ("csrc", "sw2021")


class IndustrySyncPayload(_Payload):
    as_of: date
    universe_codes: tuple[IndexCode, ...] = ()
    """Only classify members of these universes (as of ``as_of``); empty = whole market
    (CSRC creates the listings; Shenwan only updates existing listings)."""
    systems: tuple[IndustrySystem, ...] = INDUSTRY_SYSTEMS
    """Classification systems to sync: ``csrc`` (baostock) and / or ``sw2021`` (AkShare)."""

    @field_validator("systems")
    @classmethod
    def _systems(cls, value: tuple[IndustrySystem, ...]) -> tuple[IndustrySystem, ...]:
        if not value:
            raise ValueError("at least one industry system is required")
        return tuple(system for system in INDUSTRY_SYSTEMS if system in value)


class _CollectionPayload(_Payload):
    provider: SnapshotProvider = "baostock"
    scope: SnapshotScope = "universe"
    universe_code: IndexCode | None = None
    """Required for ``scope="universe"``; must be empty for the watchlist."""

    @model_validator(mode="after")
    def _routing(self) -> Self:
        if self.scope == "universe" and self.universe_code is None:
            raise ValueError("scope 'universe' needs a universe_code")
        if self.scope == "watchlist" and self.universe_code is not None:
            raise ValueError("scope 'watchlist' takes no universe_code")
        if self.provider == "ifind" and self.scope != "watchlist":
            raise ValueError(
                "iFinD collection only serves the watchlist scope (D6); "
                "target pools are collected with provider 'baostock'"
            )
        return self

    @property
    def scope_label(self) -> str:
        """``CSI300`` for a universe, ``watchlist`` otherwise."""

        return self.universe_code or WATCHLIST_SCOPE_LABEL


class MarketSnapshotPayload(_CollectionPayload):
    trade_date: date

    @model_validator(mode="after")
    def _market_provider(self) -> Self:
        if self.provider == "akshare":
            raise ValueError("AkShare (Eastmoney) collects financials only, not daily bars")
        if self.provider == "tencent":
            raise ValueError("Tencent serves the close_quote job only, not daily bar history")
        return self


class FinancialSnapshotPayload(_CollectionPayload):
    report_period: date
    as_of: date
    """Membership date; also the day the job was scheduled for."""
    part: AkshareFinancialPart | None = None
    """AkShare only: ``summary`` (default) or ``cash_flow``."""
    extra_periods: tuple[date, ...] = ()
    """AkShare ``cash_flow`` only: further periods stored from the same answers."""
    backfill: bool = False
    """AkShare only: history backfill (rows ``is_backfill``)."""

    @field_validator("report_period")
    @classmethod
    def _quarter_end(cls, value: date) -> date:
        period_kind(value)
        return value

    @field_validator("extra_periods")
    @classmethod
    def _extra_quarter_ends(cls, value: tuple[date, ...]) -> tuple[date, ...]:
        for item in value:
            period_kind(item)
        return tuple(sorted(set(value), reverse=True))

    @model_validator(mode="after")
    def _akshare_options(self) -> Self:
        if self.provider == "tencent":
            raise ValueError("Tencent serves the close_quote job only, not financials")
        if self.provider != "akshare":
            if self.part is not None or self.extra_periods or self.backfill:
                raise ValueError("part / extra_periods / backfill are AkShare options")
            return self
        if self.part is None:
            object.__setattr__(self, "part", "summary")
        if self.part != "cash_flow" and self.extra_periods:
            raise ValueError("extra_periods only applies to part 'cash_flow'")
        if self.report_period in self.extra_periods:
            object.__setattr__(
                self,
                "extra_periods",
                tuple(p for p in self.extra_periods if p != self.report_period),
            )
        return self

    @property
    def periods(self) -> tuple[date, ...]:
        """``report_period`` first, then ``extra_periods`` (newest first)."""

        return (self.report_period, *self.extra_periods)


class DividendSnapshotPayload(_CollectionPayload):
    """baostock dividend events of the scope for whole ex-date years."""

    as_of: date
    """Membership date; also the day the job was scheduled for."""
    years: tuple[int, ...] = ()
    """Ex-date years to (re)collect; default: ``as_of`` year and the year before
    (the trailing-365-day dividend window spans both)."""

    @model_validator(mode="after")
    def _dividend_options(self) -> Self:
        if self.provider != "baostock":
            raise ValueError("dividend events are collected from baostock only")
        years = self.years or (self.as_of.year - 1, self.as_of.year)
        if any(year < 1990 or year > self.as_of.year + 1 for year in years):
            raise ValueError(f"dividend years out of range: {sorted(years)}")
        object.__setattr__(self, "years", tuple(sorted(set(years))))
        return self


class CloseQuotePayload(_CollectionPayload):
    """Same-day close of the scope (plus watchlist, open candidates and holdings).

    ``scope="universe"`` covers the target pool and the user's pools;
    ``scope="watchlist"`` only the user's pools. ``slot`` tells the 15:10
    collection from the 16:00 retry (same date, separate run; listings
    already stored by either run - or by baostock - are skipped).
    """

    provider: SnapshotProvider = "tencent"
    trade_date: date
    slot: CloseQuoteSlot = "close"

    @model_validator(mode="after")
    def _close_quote_provider(self) -> Self:
        if self.provider != "tencent":
            raise ValueError("close quotes are collected from tencent only")
        return self


class EarningsEventPayload(_CollectionPayload):
    """Eastmoney earnings events of one report period (mrd-intake C1).

    ``forecast`` / ``schedule`` are whole-market calls filtered to the scope
    (``universe``: target pool + enabled watchlist + open candidates +
    holdings; ``watchlist``: the user's pools only). ``segments`` walks the
    user's pools one listing at a time whatever the scope says (the
    ``universe_code`` only names the run). ``as_of`` is the membership date
    and the day the job was scheduled for.
    """

    provider: SnapshotProvider = "akshare"
    part: EarningsEventPart
    report_period: date
    as_of: date

    @field_validator("report_period")
    @classmethod
    def _quarter_end(cls, value: date) -> date:
        period_kind(value)
        return value

    @model_validator(mode="after")
    def _earnings_provider(self) -> Self:
        if self.provider != "akshare":
            raise ValueError("earnings events are collected from akshare (Eastmoney) only")
        return self


BackfillPart = Literal["membership", "industry", "prices", "financials", "dividends"]
BACKFILL_PARTS: tuple[BackfillPart, ...] = (
    "membership",
    "industry",
    "prices",
    "financials",
    "dividends",
)
BACKFILL_CHUNKED_PARTS: frozenset[str] = frozenset({"prices", "financials", "dividends"})
_CANONICAL_TICKER = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class SnapshotBackfillPayload(_Payload):
    """One unit of the historical backfill (P1-A4, decision D5; all via baostock).

    ``membership`` and ``industry`` are single jobs for the whole universe;
    ``prices`` and ``financials`` run per chunk of ``symbols``. Depths are part
    of the payload so a plan is reproducible.
    """

    universe_code: IndexCode
    as_of: date
    part: BackfillPart
    symbols: tuple[str, ...] = ()
    chunk: int | None = None
    price_years: int = Field(default=5, ge=1, le=20)
    annual_years: int = Field(default=6, ge=0, le=20)
    quarters: int = Field(default=4, ge=0, le=12)

    @model_validator(mode="after")
    def _chunking(self) -> Self:
        if self.part in BACKFILL_CHUNKED_PARTS:
            if not self.symbols or self.chunk is None:
                raise ValueError(f"backfill part {self.part!r} needs symbols and a chunk index")
        elif self.symbols or self.chunk is not None:
            raise ValueError(f"backfill part {self.part!r} takes no symbols / chunk")
        bad = [symbol for symbol in self.symbols if not _CANONICAL_TICKER.fullmatch(symbol)]
        if bad:
            raise ValueError(f"not canonical A-share tickers: {bad[:5]}")
        return self

    @property
    def depth_label(self) -> str:
        return f"p{self.price_years}a{self.annual_years}q{self.quarters}"


class CalendarProbePayload(_Payload):
    """One dual-source check of the next unreviewed calendar year.

    The scheduler enqueues one probe per week while the committed calendar is
    inside its expiry window; the worker fetches baostock's day-by-day answers
    and Sina's list (via AkShare) and records a candidate file only when both
    agree on every trading day. An agreeing candidate is applied from the
    health page (``POST /calendar/apply``) or by
    ``tools/refresh_trading_calendar.py --write``: see
    :mod:`investment_agent.snapshots.calendar_probe`.

    ``force`` marks a manually requested check: it skips the expiry-window
    gate (never the publication or agreement gates).
    """

    as_of: date
    year: int = Field(ge=2000, le=2200)
    force: bool = False


SnapshotPayload = (
    UniverseSyncPayload
    | IndustrySyncPayload
    | MarketSnapshotPayload
    | FinancialSnapshotPayload
    | SnapshotBackfillPayload
    | DividendSnapshotPayload
    | CloseQuotePayload
    | EarningsEventPayload
    | CalendarProbePayload
)

PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    UNIVERSE_SYNC: UniverseSyncPayload,
    INDUSTRY_SYNC: IndustrySyncPayload,
    MARKET_SNAPSHOT: MarketSnapshotPayload,
    FINANCIAL_SNAPSHOT: FinancialSnapshotPayload,
    SNAPSHOT_BACKFILL: SnapshotBackfillPayload,
    DIVIDEND_SNAPSHOT: DividendSnapshotPayload,
    CLOSE_QUOTE: CloseQuotePayload,
    EARNINGS_EVENT: EarningsEventPayload,
    CALENDAR_PROBE: CalendarProbePayload,
}


def payload_kind(payload: SnapshotPayload) -> str:
    for kind, model in PAYLOAD_MODELS.items():
        if type(payload) is model:
            return kind
    raise TypeError(f"not a snapshot payload: {type(payload).__name__}")


def idempotency_key(payload: SnapshotPayload) -> str:
    """kind (+ provider) + scope + date (+ period): one run per scheduled slot."""

    kind = payload_kind(payload)
    if isinstance(payload, CalendarProbePayload):
        return f"{kind}:{payload.year}:{payload.as_of.isoformat()}"
    if isinstance(payload, UniverseSyncPayload):
        return f"{kind}:{payload.universe_code}:{payload.as_of.isoformat()}"
    if isinstance(payload, IndustrySyncPayload):
        scope = "+".join(sorted(payload.universe_codes)) or "ALL"
        key = f"{kind}:{scope}:{payload.as_of.isoformat()}"
        if payload.systems != INDUSTRY_SYSTEMS:
            key += ":" + "+".join(payload.systems)
        return key
    if isinstance(payload, SnapshotBackfillPayload):
        key = (
            f"{kind}:{payload.universe_code}:{payload.as_of.isoformat()}:"
            f"{payload.part}:{payload.depth_label}"
        )
        return key if payload.chunk is None else f"{key}:c{payload.chunk:03d}"
    if isinstance(payload, DividendSnapshotPayload):
        years = "+".join(str(year) for year in payload.years)
        return (
            f"{kind}:{payload.provider}:{payload.scope_label}:{payload.as_of.isoformat()}:{years}"
        )
    if isinstance(payload, MarketSnapshotPayload):
        return (
            f"{kind}:{payload.provider}:{payload.scope_label}:{payload.trade_date.isoformat()}"
        )
    if isinstance(payload, CloseQuotePayload):
        key = f"{kind}:{payload.provider}:{payload.scope_label}:{payload.trade_date.isoformat()}"
        return key if payload.slot == "close" else f"{key}:{payload.slot}"
    if isinstance(payload, EarningsEventPayload):
        return (
            f"{kind}:{payload.provider}:{payload.scope_label}:"
            f"{payload.report_period.isoformat()}:{payload.as_of.isoformat()}:{payload.part}"
        )
    key = (
        f"{kind}:{payload.provider}:{payload.scope_label}:"
        f"{payload.report_period.isoformat()}:{payload.as_of.isoformat()}"
    )
    if payload.provider == "akshare":
        key += f":{payload.part}"
        if payload.extra_periods:
            # count + digest keeps the key within 128 characters (D5: 8 extra periods)
            listed = ",".join(p.strftime("%Y%m%d") for p in payload.extra_periods)
            digest = hashlib.sha1(listed.encode(), usedforsecurity=False).hexdigest()[:8]
            key += f":+{len(payload.extra_periods)}p{digest}"
        if payload.backfill:
            key += ":backfill"
    return key


def payload_universe(payload: SnapshotPayload) -> str | None:
    return getattr(payload, "universe_code", None)


async def enqueue_snapshot_job(
    session: AsyncSession,
    payload: SnapshotPayload,
    *,
    triggered_by: str,
    key: str | None = None,
    max_attempts: int = 3,
) -> tuple[PipelineRun, bool]:
    """Queue one snapshot job; an existing run with the same key is returned."""

    universe_code = payload_universe(payload)
    universes = UniverseRepository(session)
    if universe_code is not None:
        await universes.ensure_universe(universe_code)
    return await PipelineRunRepository(session).enqueue(
        kind=payload_kind(payload),
        idempotency_key=key or idempotency_key(payload),
        triggered_by=triggered_by,
        universe_code=universe_code,
        request_json=request_json(payload),
        max_attempts=max_attempts,
    )


_AKSHARE_ONLY_FIELDS = frozenset({"part", "extra_periods", "backfill"})


def request_json(payload: SnapshotPayload) -> dict[str, object]:
    """The persisted request; AkShare-only options are omitted for other providers."""

    data = payload.model_dump(mode="json")
    if isinstance(payload, FinancialSnapshotPayload) and payload.provider != "akshare":
        for name in _AKSHARE_ONLY_FIELDS:
            data.pop(name, None)
    return data

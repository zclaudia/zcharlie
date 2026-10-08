"""Snapshot collection schedule (P1-A5): pure trigger decisions + enqueueing.

| job                  | cron (Asia/Shanghai) | gate                                         |
|----------------------|----------------------|----------------------------------------------|
| universe_sync        | Mon 08:00            | none (trading-day agnostic)                  |
| industry_sync        | daily 08:00          | first trading day of the month               |
| market_snapshot      | Mon-Fri 18:30        | trading day                                  |
| close_quote          | Mon-Fri 15:10        | trading day                                  |
| close_quote_retry    | Mon-Fri 16:00        | trading day                                  |
| financial_weekly     | Sat 09:00            | none                                         |
| financial_deadline   | daily 19:00          | first trading day after 04-30 / 08-31 / 10-31 |

Each fire enqueues one job per configured universe (industry: one job for
the union, syncing both CSRC via baostock and Shenwan 2021 via AkShare) with the default per-date idempotency key, so a misfire catch-up
or a restart never duplicates work. The deadline-triggered financial job also
runs the disclosure-overdue scan (P1-A6, :mod:`investment_agent.snapshots.disclosure`);
it fires in the evening so reports published on the deadline day itself have
reached baostock first. Market and financial jobs collect the
target pool from baostock (``provider="baostock"``, ``scope="universe"``,
decision D6); no iFinD job is scheduled for breadth data. The financial jobs collect the most
recent report period(s) whose statutory disclosure deadline has passed plus
any period still inside its disclosure season.

Eastmoney via AkShare (P1-A11): both financial jobs also enqueue the period
summaries (``provider="akshare"``, ``part="summary"``) after the baostock
jobs, so the summaries find the baostock pubDates; ``financial_weekly``
additionally enqueues one per-stock cash-flow statement job
(``part="cash_flow"``, all due periods from one call per stock) inside the
disclosure-season windows :data:`CASH_FLOW_WINDOWS` only (~1.4 h for CSI 300).

Dividends (F2 / Q2): ``financial_weekly`` also enqueues one baostock
``dividend_snapshot`` per universe for the current and the previous ex-date
year (~600 queries for CSI 300, a few minutes).

Close quotes (mrd-intake B2): ``close_quote`` enqueues one Tencent
``close_quote`` job per universe at 15:10 of a trading day (the target pool
plus the watchlist, open candidates and holdings; ~6 requests for CSI 300);
``close_quote_retry`` enqueues the ``slot="retry"`` run of the same date at
16:00 for listings that were still ``pending`` at 15:10.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, cast

import structlog

from investment_agent.providers.baostock_reference import INDEX_CODES, IndexCode
from investment_agent.scheduler.trading_calendar import TradingCalendar
from investment_agent.snapshots.disclosure import statutory_deadline
from investment_agent.snapshots.jobs import (
    INDUSTRY_SYSTEMS,
    CloseQuotePayload,
    DividendSnapshotPayload,
    FinancialSnapshotPayload,
    IndustrySyncPayload,
    MarketSnapshotPayload,
    SnapshotPayload,
    UniverseSyncPayload,
    enqueue_snapshot_job,
)
from investment_agent.storage.database import session_scope

log = structlog.get_logger(__name__)

SnapshotScheduleJob = Literal[
    "universe_sync",
    "industry_sync",
    "market_snapshot",
    "close_quote",
    "close_quote_retry",
    "financial_weekly",
    "financial_deadline",
]
SNAPSHOT_SCHEDULE_JOBS: tuple[SnapshotScheduleJob, ...] = (
    "universe_sync",
    "industry_sync",
    "market_snapshot",
    "close_quote",
    "close_quote_retry",
    "financial_weekly",
    "financial_deadline",
)
# Jobs whose gate does not consult the trading calendar.
CALENDAR_FREE_JOBS = frozenset({"universe_sync", "financial_weekly"})

# Statutory disclosure deadlines (month, day) after which the next trading
# day triggers an extra financial collection.
FINANCIAL_DEADLINES: tuple[tuple[int, int], ...] = ((4, 30), (8, 31), (10, 31))
_QUARTER_ENDS: tuple[tuple[int, int], ...] = ((3, 31), (6, 30), (9, 30), (12, 31))
# Disclosure-season windows ((month, day) inclusive) in which the weekly
# financial job also refreshes Eastmoney per-stock cash-flow statements.
CASH_FLOW_WINDOWS: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (
    ((4, 15), (5, 15)),
    ((8, 15), (9, 15)),
    ((10, 15), (11, 15)),
)


def _recent_periods(today: date, count: int = 8) -> list[date]:
    periods = [
        date(year, month, day)
        for year in range(today.year - 2, today.year + 1)
        for month, day in _QUARTER_ENDS
    ]
    return sorted((p for p in periods if p < today), reverse=True)[:count]


def financial_periods_due(today: date) -> list[date]:
    """Report periods worth collecting on ``today``, oldest first.

    * every period whose deadline is the most recent deadline already passed
      (on 2026-05-02 that is both 2026-03-31 and 2025-12-31);
    * every period that has ended but whose deadline has not passed yet
      (disclosure season in progress).
    """

    periods = _recent_periods(today)
    passed = [p for p in periods if statutory_deadline(p) < today]
    due: set[date] = set()
    if passed:
        latest_deadline = max(statutory_deadline(p) for p in passed)
        due.update(p for p in passed if statutory_deadline(p) == latest_deadline)
    due.update(p for p in periods if statutory_deadline(p) >= today)
    return sorted(due)


def is_first_trading_day_of_month(calendar: TradingCalendar, day: date) -> bool:
    if not calendar.is_trading_day(day):
        return False
    cursor = day.replace(day=1)
    while cursor < day:
        if calendar.is_trading_day(cursor):
            return False
        cursor += timedelta(days=1)
    return True


def is_first_trading_day_after(calendar: TradingCalendar, day: date, anchor: date) -> bool:
    if day <= anchor or not calendar.is_trading_day(day):
        return False
    cursor = anchor + timedelta(days=1)
    while cursor < day:
        if calendar.is_trading_day(cursor):
            return False
        cursor += timedelta(days=1)
    return True


def is_financial_deadline_trigger(calendar: TradingCalendar, day: date) -> bool:
    """First trading day after 04-30, 08-31 or 10-31 (same or previous year)."""

    for year in (day.year, day.year - 1):
        for month, month_day in FINANCIAL_DEADLINES:
            anchor = date(year, month, month_day)
            if anchor < day <= anchor + timedelta(days=31) and is_first_trading_day_after(
                calendar, day, anchor
            ):
                return True
    return False


def _universes(codes: Sequence[str]) -> list[IndexCode]:
    unknown = sorted(set(codes) - set(INDEX_CODES))
    if unknown:
        raise ValueError(f"unsupported snapshot universes: {unknown}")
    return [cast(IndexCode, code) for code in dict.fromkeys(codes)]


def snapshot_payloads(
    job: SnapshotScheduleJob,
    *,
    today: date,
    calendar: TradingCalendar | None,
    universes: Sequence[str],
) -> list[SnapshotPayload]:
    """What one fire of ``job`` on ``today`` should enqueue (pure).

    ``calendar`` may be ``None`` only for :data:`CALENDAR_FREE_JOBS`.
    """

    codes = _universes(universes)
    if job == "universe_sync":
        return [UniverseSyncPayload(universe_code=code, as_of=today) for code in codes]
    if job == "financial_weekly":
        # Dividends (minutes) before the per-stock cash-flow statements (hours).
        return [
            *_financial(codes, today),
            *_dividends(codes, today),
            *_akshare_cash_flow(codes, today),
        ]
    if calendar is None:
        raise ValueError(f"{job} needs the trading calendar")
    if job == "industry_sync":
        if not is_first_trading_day_of_month(calendar, today):
            return []
        return [
            IndustrySyncPayload(
                as_of=today, universe_codes=tuple(sorted(codes)), systems=INDUSTRY_SYSTEMS
            )
        ]
    if job == "market_snapshot":
        if not calendar.is_trading_day(today):
            return []
        return [
            MarketSnapshotPayload(
                provider="baostock", scope="universe", universe_code=code, trade_date=today
            )
            for code in codes
        ]
    if job in ("close_quote", "close_quote_retry"):
        if not calendar.is_trading_day(today):
            return []
        return [
            CloseQuotePayload(
                provider="tencent",
                scope="universe",
                universe_code=code,
                trade_date=today,
                slot="close" if job == "close_quote" else "retry",
            )
            for code in codes
        ]
    if job == "financial_deadline":
        return _financial(codes, today) if is_financial_deadline_trigger(calendar, today) else []
    raise ValueError(f"unknown snapshot schedule job: {job!r}")


def _financial(codes: Sequence[IndexCode], today: date) -> list[SnapshotPayload]:
    """baostock first, then the Eastmoney period summaries of the same periods."""

    periods = financial_periods_due(today)
    baostock: list[SnapshotPayload] = [
        FinancialSnapshotPayload(
            provider="baostock",
            scope="universe",
            universe_code=code,
            report_period=period,
            as_of=today,
        )
        for code in codes
        for period in periods
    ]
    summaries: list[SnapshotPayload] = [
        FinancialSnapshotPayload(
            provider="akshare",
            part="summary",
            scope="universe",
            universe_code=code,
            report_period=period,
            as_of=today,
        )
        for code in codes
        for period in periods
    ]
    return [*baostock, *summaries]


def _dividends(codes: Sequence[IndexCode], today: date) -> list[SnapshotPayload]:
    return [
        DividendSnapshotPayload(provider="baostock", scope="universe", universe_code=code, as_of=today)
        for code in codes
    ]


def in_cash_flow_window(day: date) -> bool:
    """Whether ``day`` is inside a disclosure-season window (:data:`CASH_FLOW_WINDOWS`)."""

    key = (day.month, day.day)
    return any(start <= key <= end for start, end in CASH_FLOW_WINDOWS)


def _akshare_cash_flow(codes: Sequence[IndexCode], today: date) -> list[SnapshotPayload]:
    if not in_cash_flow_window(today):
        return []
    periods = sorted(financial_periods_due(today), reverse=True)
    if not periods:
        return []
    return [
        FinancialSnapshotPayload(
            provider="akshare",
            part="cash_flow",
            scope="universe",
            universe_code=code,
            report_period=periods[0],
            extra_periods=tuple(periods[1:]),
            as_of=today,
        )
        for code in codes
    ]


@dataclass(frozen=True)
class EnqueueOutcome:
    kind: str
    key: str | None
    run_id: int | None
    created: bool
    error: str | None = None


async def enqueue_snapshot_payloads(
    payloads: Sequence[SnapshotPayload], *, triggered_by: str = "scheduler"
) -> list[EnqueueOutcome]:
    """Enqueue each payload in its own transaction; one failure skips only itself."""

    outcomes: list[EnqueueOutcome] = []
    for payload in payloads:
        try:
            async with session_scope() as session:
                run, created = await enqueue_snapshot_job(
                    session, payload, triggered_by=triggered_by
                )
                outcomes.append(
                    EnqueueOutcome(run.kind, run.idempotency_key, run.id, created)
                )
        except Exception as exc:
            log.exception("scheduler.snapshot.enqueue_failed", payload=payload.model_dump())
            outcomes.append(
                EnqueueOutcome(type(payload).__name__, None, None, False, f"{exc}")
            )
    return outcomes

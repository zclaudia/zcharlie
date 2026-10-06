"""Historical backfill of the PIT snapshot tables via baostock (P1-A4, D5 / D6).

``ia snapshot backfill`` plans resumable ``snapshot_backfill`` jobs; each job
runs one *part* (:class:`~investment_agent.snapshots.jobs.SnapshotBackfillPayload`):

``membership`` (one job)
    Constituent snapshots over the window ``[as_of - price_years, as_of]``:
    a monthly scan, refined weekly only between months whose member sets
    differ (baostock publishes at most weekly; the snapshot's ``updateDate``
    is the effective date). :meth:`UniverseRepository.rebuild_history`
    recomputes the window and merges it with live rows. On success the job
    enqueues the other parts for the *survivorship set*: every listing that
    was a member at any time in the window (current + removed).

``industry`` (one job)
    One whole-market CSRC query per year start in the window, plus the
    window start and ``as_of``; applied with
    :meth:`IndustryRepository.rebuild_history` to the backfill set only.
    Granularity is therefore one year: a reclassification is dated at the
    effective date of the first yearly sample that shows it. The same job
    also writes the Shenwan 2021 history (``sw2021``, exact change dates)
    of the backfill set from one AkShare call (:mod:`.shenwan`).

``prices`` (chunks of :data:`CHUNK_SIZE` symbols)
    Raw and backward-adjusted daily bars from the window start to the day
    before the earliest stored baostock bar (``as_of`` if none): one query per
    symbol and series. Rows are ``is_backfill=True``.

``financials`` (chunks)
    The last ``annual_years`` annual reports and the last ``quarters``
    periodic reports whose statutory deadline is before ``as_of``
    (:func:`report_periods`), skipping periods already stored for baostock;
    rows are ``is_backfill=True`` with ``available_from`` = actual pubDate.
    A report published before the listing date (baostock ``ipoDate``) is kept
    and flagged (``fields.pre_listing`` and run metadata).

``report_overdue`` is never backfilled: overdue detection only runs live.

Every chunk is idempotent (it skips stored data), failures are isolated per
symbol, and a chunk stops calling baostock after
:data:`~investment_agent.snapshots.breadth.MAX_CONSECUTIVE_TRANSIENT`
consecutive transient failures. Re-running the CLI resumes: finished parts
are kept, partial / failed chunks are re-queued with their remaining symbols.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.providers.baostock_market import (
    AdjustKind,
    DailyBarSeries,
    QuarterlyFinancials,
    StockBasic,
    quarter_of,
)
from investment_agent.providers.baostock_reference import (
    ConstituentSnapshot,
    IndexCode,
    IndustrySnapshot,
)
from investment_agent.providers.errors import ProviderError, ProviderTransientError
from investment_agent.research.retry_policy import is_transient_error
from investment_agent.snapshots.breadth import (
    BAOSTOCK_PROVIDER,
    SERIES,
    TransientBreaker,
    store_daily_bars,
    store_financial,
    write_daily_artifact,
    write_financial_artifact,
)
from investment_agent.snapshots.collect import (
    ArtifactWriter,
    CollectionReport,
    Completeness,
    SnapshotPreconditionError,
    error_reason,
    fence_writes,
)
from investment_agent.snapshots.disclosure import statutory_deadline
from investment_agent.snapshots.jobs import (
    BACKFILL_CHUNKED_PARTS,
    BACKFILL_PARTS,
    SNAPSHOT_BACKFILL,
    BackfillPart,
    SnapshotBackfillPayload,
    enqueue_snapshot_job,
    idempotency_key,
)
from investment_agent.snapshots.shenwan import (
    ShenwanClient,
    apply_sw2021_history,
    existing_listing_ids,
    write_history_artifact,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease
from investment_agent.storage.repositories.universe import UniverseRepository

log = structlog.get_logger(__name__)

CHUNK_SIZE = 20
WEEK = timedelta(days=7)
# A backfilled series whose first bar is within this many days of the window
# start is complete (the window may start on a weekend / holiday; the longest
# A-share closure, Spring Festival, is about 10 calendar days).
COVERAGE_SLACK_DAYS = 15
_MAX_LISTED = 50
_RETRY_SUFFIX = re.compile(r":r\d+$")

# Measured on 2026-10-06 (docs/product/p1-plan.md 2.1 and P1-A4).
SECONDS_RAW_5Y = 3.0
SECONDS_BACKWARD_5Y = 9.0
SECONDS_TABLE_QUERY = 0.6
SECONDS_INDUSTRY_QUERY = 60.0
SECONDS_CONSTITUENT_QUERY = 0.7
SECONDS_BASIC_QUERY = 0.2
WEEKLY_QUERIES_PER_CHANGE = 3
"""Estimate: weekly refinement queries per pair of differing months."""


class BackfillClient(Protocol):
    def session(self) -> AbstractAsyncContextManager[None]: ...

    async def fetch_constituents(
        self, index_code: IndexCode, as_of: date
    ) -> ConstituentSnapshot: ...

    async def fetch_industry(self, as_of: date, symbol: str | None = None) -> IndustrySnapshot: ...

    async def fetch_daily_bars(
        self, symbol: str, start: date, end: date, adjust: AdjustKind
    ) -> DailyBarSeries: ...

    async def fetch_quarterly_financials(
        self, symbol: str, year: int, quarter: int
    ) -> QuarterlyFinancials | None: ...

    async def fetch_stock_basic(self, symbol: str) -> StockBasic | None: ...


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #


def years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 Feb
        return day.replace(year=day.year - years, day=28)


def window_start(payload: SnapshotBackfillPayload) -> date:
    return years_before(payload.as_of, payload.price_years)


def month_samples(start: date, end: date) -> list[date]:
    """``start``, the first day of every later month up to ``end``, and ``end``."""

    days = [start]
    cursor = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    while cursor < end:
        days.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    if end > start:
        days.append(end)
    return days


def weekly_dates(after: date, before: date) -> list[date]:
    """Every 7th day strictly between ``after`` and ``before``."""

    days: list[date] = []
    cursor = after + WEEK
    while cursor < before:
        days.append(cursor)
        cursor += WEEK
    return days


def industry_samples(start: date, as_of: date) -> list[date]:
    """Window start, every 1 January inside the window, and ``as_of``."""

    days = {start, as_of}
    days.update(date(year, 1, 1) for year in range(start.year + 1, as_of.year + 1))
    return sorted(day for day in days if start <= day <= as_of)


def report_periods(as_of: date, *, annual_years: int, quarters: int) -> list[date]:
    """Newest first: the last ``quarters`` periods and ``annual_years`` annual reports
    whose statutory deadline is before ``as_of`` (D5)."""

    ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    candidates = [
        date(year, month, day)
        for year in range(as_of.year - annual_years - 3, as_of.year + 1)
        for month, day in ends
    ]
    due = sorted((p for p in candidates if statutory_deadline(p) < as_of), reverse=True)
    periods = set(due[:quarters])
    periods.update([p for p in due if (p.month, p.day) == (12, 31)][:annual_years])
    return sorted(periods, reverse=True)


def chunked(symbols: Sequence[str], size: int | None = None) -> list[tuple[str, ...]]:
    size = size or CHUNK_SIZE
    return [tuple(symbols[i : i + size]) for i in range(0, len(symbols), size)]


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConstituentScan:
    snapshots: tuple[ConstituentSnapshot, ...]
    """Ordered by effective date, one per distinct effective date."""
    monthly_queries: int
    weekly_queries: int
    differing_months: int

    @property
    def queries(self) -> int:
        return self.monthly_queries + self.weekly_queries


async def scan_constituents(
    client: BackfillClient,
    universe_code: IndexCode,
    start: date,
    as_of: date,
    *,
    refine: bool = True,
) -> ConstituentScan:
    """Monthly scan, then weekly refinement between months whose sets differ.

    A query on any date answers the snapshot effective on that date, so the
    refinement only needs dates before the later sample's effective date.
    """

    async with client.session():
        monthly = [
            await client.fetch_constituents(universe_code, day)
            for day in month_samples(start, as_of)
        ]
        refined: list[ConstituentSnapshot] = []
        differing = 0
        for earlier, later in pairwise(monthly):
            if set(earlier.members) == set(later.members):
                continue
            differing += 1
            if not refine:
                continue
            limit = min(later.as_of, later.effective_date or later.as_of)
            for day in weekly_dates(earlier.as_of, limit):
                refined.append(await client.fetch_constituents(universe_code, day))
    by_date: dict[date, ConstituentSnapshot] = {}
    for snapshot in monthly + refined:
        if snapshot.effective_date is not None:
            by_date.setdefault(snapshot.effective_date, snapshot)
    return ConstituentScan(
        snapshots=tuple(by_date[day] for day in sorted(by_date)),
        monthly_queries=len(monthly),
        weekly_queries=len(refined),
        differing_months=differing,
    )


async def backfill_symbols(
    session: AsyncSession, universe_code: str, start: date, as_of: date
) -> list[str]:
    """Survivorship set: every listing that was a member at any time in the window."""

    members = await UniverseRepository(session).members_between(universe_code, start, as_of)
    return sorted({member.ticker for member in members})


async def backfill_membership(
    payload: SnapshotBackfillPayload,
    *,
    client: BackfillClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> dict[str, object]:
    start = window_start(payload)
    scan = await scan_constituents(client, payload.universe_code, start, payload.as_of)
    raw_ref = artifacts.write(
        f"constituents-history-{payload.universe_code}-{payload.as_of.isoformat()}",
        {
            "provider": BAOSTOCK_PROVIDER,
            "universe_code": payload.universe_code,
            "window_start": start.isoformat(),
            "as_of": payload.as_of.isoformat(),
            "snapshots": [
                {
                    "as_of": snapshot.as_of.isoformat(),
                    "effective_date": (
                        snapshot.effective_date.isoformat() if snapshot.effective_date else None
                    ),
                    "captured_at": snapshot.captured_at.isoformat(),
                    "members": {ticker: snapshot.member_names.get(ticker, "") for ticker in snapshot.members},
                }
                for snapshot in scan.snapshots
            ],
        },
    )
    async with session_scope() as session:
        await fence_writes(session, lease)
        rebuild = await UniverseRepository(session).rebuild_history(
            payload.universe_code, scan.snapshots, window_start=start
        )
        symbols = await backfill_symbols(session, payload.universe_code, start, payload.as_of)
        current = {
            member.ticker
            for member in await UniverseRepository(session).members_as_of(
                payload.universe_code, payload.as_of
            )
        }
        plan = await plan_followups(session, payload, symbols, triggered_by=SNAPSHOT_BACKFILL)
    removed = sorted(set(symbols) - current)
    return {
        "part": "membership",
        "universe_code": payload.universe_code,
        "as_of": payload.as_of.isoformat(),
        "window_start": start.isoformat(),
        "baostock_queries": scan.queries,
        "monthly_queries": scan.monthly_queries,
        "weekly_queries": scan.weekly_queries,
        "differing_months": scan.differing_months,
        "snapshots": len(scan.snapshots),
        "first_effective": rebuild.first_effective.isoformat(),
        "last_effective": rebuild.last_effective.isoformat(),
        "change_dates": [day.isoformat() for day in rebuild.change_dates],
        "symbols": len(symbols),
        "current": len(current),
        "removed": len(removed),
        "removed_symbols": removed[:_MAX_LISTED],
        "intervals": {
            "kept": rebuild.kept,
            "reused": rebuild.reused,
            "deleted": rebuild.deleted,
            "inserted": rebuild.inserted,
        },
        "followups": [action.to_metadata() for action in plan],
        "raw_ref": raw_ref,
    }


# --------------------------------------------------------------------------- #
# Industry
# --------------------------------------------------------------------------- #


async def backfill_industry(
    payload: SnapshotBackfillPayload,
    *,
    client: BackfillClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    shenwan: ShenwanClient | None = None,
) -> dict[str, object]:
    start = window_start(payload)
    async with session_scope() as session:
        symbols = await backfill_symbols(session, payload.universe_code, start, payload.as_of)
    if not symbols:
        raise SnapshotPreconditionError(
            f"{payload.universe_code} has no membership history in "
            f"[{start.isoformat()}, {payload.as_of.isoformat()}]; run the membership part first"
        )
    wanted = set(symbols)
    snapshots: list[IndustrySnapshot] = []
    async with client.session():
        for day in industry_samples(start, payload.as_of):
            snapshots.append(await client.fetch_industry(day))
    raw_ref = artifacts.write(
        f"industry-history-{payload.universe_code}-{payload.as_of.isoformat()}",
        {
            "provider": BAOSTOCK_PROVIDER,
            "note": "rows of the backfill set only (whole-market answers filtered)",
            "snapshots": [
                {
                    "as_of": snapshot.as_of.isoformat(),
                    "effective_date": (
                        snapshot.effective_date.isoformat() if snapshot.effective_date else None
                    ),
                    "captured_at": snapshot.captured_at.isoformat(),
                    "provider_entries": len(snapshot.entries),
                    "rows": [
                        dict(row)
                        for row in snapshot.raw_rows
                        if _raw_ticker(row) in wanted
                    ],
                }
                for snapshot in snapshots
            ],
        },
    )
    async with session_scope() as session:
        await fence_writes(session, lease)
        result = await IndustryRepository(session).rebuild_history(snapshots, tickers=symbols)
    sw2021 = await _backfill_sw2021(payload, symbols, shenwan, artifacts, lease)
    return {
        "part": "industry",
        "universe_code": payload.universe_code,
        "as_of": payload.as_of.isoformat(),
        "window_start": start.isoformat(),
        "baostock_queries": len(snapshots),
        "samples": [snapshot.as_of.isoformat() for snapshot in snapshots],
        "symbols": len(symbols),
        "reclassified": list(result.reclassified[:_MAX_LISTED]),
        "reclassified_count": len(result.reclassified),
        "unclassified": list(result.unclassified[:_MAX_LISTED]),
        "intervals": {
            "kept": result.kept,
            "reused": result.reused,
            "deleted": result.deleted,
            "inserted": result.inserted,
        },
        "granularity": "yearly samples: a change is dated at the first sample showing it",
        "raw_ref": raw_ref,
        "sw2021": sw2021,
    }


async def _backfill_sw2021(
    payload: SnapshotBackfillPayload,
    symbols: Sequence[str],
    shenwan: ShenwanClient | None,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> dict[str, object]:
    """Shenwan 2021 history of the backfill set (one whole-market call, P1-A9).

    Transient AkShare errors propagate (the job retries; the CSRC rebuild is
    idempotent). A terminal error is recorded and does not fail the part:
    the monthly ``industry_sync`` repairs the target pool's Shenwan history.
    """

    if shenwan is None:
        return {"skipped": "no Shenwan client"}
    try:
        history = await shenwan.fetch_industry_history()
    except ProviderTransientError:
        raise
    except ProviderError as exc:
        log.warning("backfill_sw2021_failed", error=str(exc))
        return {"error": f"{type(exc).__name__}: {exc}"}
    async with session_scope() as session:
        scope = await existing_listing_ids(session, symbols)
    raw_ref = write_history_artifact(
        artifacts,
        history,
        name=f"industry-sw2021-history-{payload.universe_code}-{payload.as_of.isoformat()}",
        tickers=scope,
    )
    result = await apply_sw2021_history(history, scope=scope, as_of=payload.as_of, lease=lease)
    return {**result, "raw_ref": raw_ref}


def _raw_ticker(row: Any) -> str:
    code = str(row.get("code", ""))
    exchange, _, symbol = code.partition(".")
    return f"{symbol}.{exchange.upper()}" if symbol else ""


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #


def price_backfill_end(
    start: date, as_of: date, earliest: date | None, *, earliest_is_backfill: bool
) -> date | None:
    """Last trade date to request for one series; ``None`` when nothing is missing."""

    if earliest is None:
        return as_of
    if earliest_is_backfill and (earliest - start).days <= COVERAGE_SLACK_DAYS:
        return None
    end = earliest - timedelta(days=1)
    return end if end >= start else None


async def _listing_ids(symbols: Sequence[str]) -> dict[str, int]:
    async with session_scope() as session:
        listings = ListingRepository(session)
        ids: dict[str, int] = {}
        for ticker in symbols:
            listing = await listings.get_a_share_listing(ticker)
            if listing is not None:
                ids[ticker] = listing.id
        return ids


async def backfill_prices(
    payload: SnapshotBackfillPayload,
    *,
    client: BackfillClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> CollectionReport:
    start, as_of = window_start(payload), payload.as_of
    symbols = list(payload.symbols)
    ids = await _listing_ids(symbols)
    async with session_scope() as session:
        market = MarketSnapshotRepository(session)
        earliest = {
            adjust: await market.earliest_trade_dates(
                list(ids.values()), adjust=adjust, provider=BAOSTOCK_PROVIDER
            )
            for adjust in SERIES
        }
        earliest_backfill = {
            adjust: await market.earliest_trade_dates(
                list(ids.values()), adjust=adjust, provider=BAOSTOCK_PROVIDER, backfill_only=True
            )
            for adjust in SERIES
        }

    completeness = Completeness(expected=sorted(symbols))
    report = CollectionReport(completeness, provider=BAOSTOCK_PROVIDER)
    breaker = TransientBreaker()
    rows_inserted = dict.fromkeys(SERIES, 0)
    queries = 0
    up_to_date: list[str] = []
    windows: dict[str, dict[str, str]] = {}
    async with client.session():
        for ticker in symbols:
            listing_id = ids.get(ticker)
            if listing_id is None:
                completeness.mark_missing(
                    ticker, "failed", "no listing; run the membership part first"
                )
                report.all_failures_transient = False
                continue
            plan: dict[AdjustKind, date] = {}
            for adjust in SERIES:
                first = earliest[adjust].get(listing_id)
                end = price_backfill_end(
                    start,
                    as_of,
                    first,
                    earliest_is_backfill=first is not None
                    and earliest_backfill[adjust].get(listing_id) == first,
                )
                if end is not None:
                    plan[adjust] = end
            if not plan:
                up_to_date.append(ticker)
                completeness.mark_collected(ticker)
                continue
            if breaker.reason is not None:
                completeness.mark_missing(ticker, "failed", breaker.reason)
                continue
            try:
                fetched: dict[AdjustKind, DailyBarSeries] = {}
                for adjust, end in plan.items():
                    queries += 1
                    fetched[adjust] = await client.fetch_daily_bars(ticker, start, end, adjust)
                ref = write_daily_artifact(
                    artifacts, f"backfill-daily-{ticker}", ticker, fetched
                )
                async with session_scope() as session:
                    await fence_writes(session, lease)
                    market = MarketSnapshotRepository(session)
                    for adjust, series in fetched.items():
                        rows_inserted[adjust] += await store_daily_bars(
                            market,
                            listing_id=listing_id,
                            series=series,
                            raw_ref=ref,
                            is_backfill=True,
                        )
            except LeaseLostError:
                raise
            except Exception as exc:
                reason = error_reason(exc)
                report.record_failure(exc)
                breaker.failure(exc)
                log.warning("snapshot.backfill.prices.failed", ticker=ticker, error=reason)
                completeness.mark_missing(ticker, "failed", reason)
                continue
            breaker.success()
            windows[ticker] = {
                adjust: f"{start.isoformat()}..{end.isoformat()}" for adjust, end in plan.items()
            }
            if all(not series.bars for series in fetched.values()) and not any(
                earliest[adjust].get(listing_id) for adjust in SERIES
            ):
                completeness.mark_missing(
                    ticker, "no_data", f"no baostock bars in [{start}, {as_of}]"
                )
            else:
                completeness.mark_collected(ticker)

    report.metadata = {
        **_chunk_metadata(payload, completeness),
        "window_start": start.isoformat(),
        "baostock_queries": queries,
        "rows_inserted": rows_inserted,
        "already_backfilled": up_to_date,
        "windows": windows,
        "aborted": breaker.reason,
        "is_backfill": True,
    }
    return report


# --------------------------------------------------------------------------- #
# Financials
# --------------------------------------------------------------------------- #


async def backfill_financials(
    payload: SnapshotBackfillPayload,
    *,
    client: BackfillClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> CollectionReport:
    periods = report_periods(
        payload.as_of, annual_years=payload.annual_years, quarters=payload.quarters
    )
    symbols = list(payload.symbols)
    ids = await _listing_ids(symbols)
    async with session_scope() as session:
        repository = FinancialSnapshotRepository(session)
        present = {
            period: await repository.listings_with_period(
                list(ids.values()), period, provider=BAOSTOCK_PROVIDER
            )
            for period in periods
        }

    completeness = Completeness(expected=sorted(symbols))
    report = CollectionReport(completeness, provider=BAOSTOCK_PROVIDER)
    breaker = TransientBreaker()
    queries = 0
    stored = 0
    skipped_existing = 0
    not_disclosed: dict[str, list[str]] = {}
    pre_listing: list[str] = []
    listing_dates: dict[str, str | None] = {}
    async with client.session():
        for ticker in symbols:
            listing_id = ids.get(ticker)
            if listing_id is None:
                completeness.mark_missing(
                    ticker, "failed", "no listing; run the membership part first"
                )
                report.all_failures_transient = False
                continue
            todo = [period for period in periods if listing_id not in present[period]]
            skipped_existing += len(periods) - len(todo)
            if not todo:
                completeness.mark_collected(ticker)
                continue
            if breaker.reason is not None:
                completeness.mark_missing(ticker, "failed", breaker.reason)
                continue
            ipo_date = await _listing_date(client, ticker, breaker)
            queries += 1
            listing_dates[ticker] = ipo_date.isoformat() if ipo_date else None
            failures: list[str] = []
            for period in todo:
                if breaker.reason is not None:
                    failures.append(breaker.reason)
                    break
                year, quarter = quarter_of(period)
                try:
                    record = await client.fetch_quarterly_financials(ticker, year, quarter)
                    queries += 4 if record is not None else 1
                    if record is None:
                        not_disclosed.setdefault(ticker, []).append(period.isoformat())
                        breaker.success()
                        continue
                    flagged = ipo_date is not None and record.pub_date < ipo_date
                    ref = write_financial_artifact(artifacts, record)
                    async with session_scope() as session:
                        await fence_writes(session, lease)
                        await store_financial(
                            FinancialSnapshotRepository(session),
                            listing_id=listing_id,
                            record=record,
                            raw_ref=ref,
                            is_backfill=True,
                            extra_fields={
                                "pre_listing": flagged,
                                "listing_date": listing_dates[ticker],
                            },
                        )
                    stored += 1
                    if flagged:
                        pre_listing.append(f"{ticker}:{period.isoformat()}")
                except LeaseLostError:
                    raise
                except Exception as exc:
                    queries += 1
                    reason = f"{period.isoformat()}: {error_reason(exc)}"
                    report.record_failure(exc)
                    breaker.failure(exc)
                    log.warning("snapshot.backfill.financials.failed", ticker=ticker, error=reason)
                    failures.append(reason)
                    continue
                breaker.success()
            if failures:
                completeness.mark_missing(ticker, "failed", "; ".join(failures))
            else:
                completeness.mark_collected(ticker)

    report.metadata = {
        **_chunk_metadata(payload, completeness),
        "periods": [period.isoformat() for period in periods],
        "baostock_queries": queries,
        "stored": stored,
        "skipped_existing": skipped_existing,
        "not_disclosed": not_disclosed,
        "pre_listing": pre_listing,
        "listing_dates": listing_dates,
        "aborted": breaker.reason,
        "availability": "actual: available_from = baostock pubDate",
        "is_backfill": True,
        "report_overdue": "not backfilled (live detection only)",
    }
    return report


async def _listing_date(
    client: BackfillClient, ticker: str, breaker: TransientBreaker
) -> date | None:
    """baostock ``ipoDate``; unknown (``None``) when the lookup fails."""

    try:
        basic = await client.fetch_stock_basic(ticker)
    except LeaseLostError:
        raise
    except Exception as exc:
        breaker.failure(exc)
        log.warning(
            "snapshot.backfill.listing_date_failed",
            ticker=ticker,
            error=error_reason(exc),
            transient=is_transient_error(exc),
        )
        return None
    breaker.success()
    return basic.ipo_date if basic is not None else None


def _chunk_metadata(
    payload: SnapshotBackfillPayload, completeness: Completeness
) -> dict[str, object]:
    return {
        "part": payload.part,
        "universe_code": payload.universe_code,
        "as_of": payload.as_of.isoformat(),
        "chunk": payload.chunk,
        "symbols": len(payload.symbols),
        "collected_symbols": sorted(completeness.collected),
        "provider": BAOSTOCK_PROVIDER,
    }


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


async def run_backfill_part(
    payload: SnapshotBackfillPayload,
    *,
    client: BackfillClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    shenwan: ShenwanClient | None = None,
) -> dict[str, object] | CollectionReport:
    if payload.part == "membership":
        return await backfill_membership(payload, client=client, artifacts=artifacts, lease=lease)
    if payload.part == "industry":
        return await backfill_industry(
            payload, client=client, artifacts=artifacts, lease=lease, shenwan=shenwan
        )
    if payload.part == "prices":
        return await backfill_prices(payload, client=client, artifacts=artifacts, lease=lease)
    return await backfill_financials(payload, client=client, artifacts=artifacts, lease=lease)


# --------------------------------------------------------------------------- #
# Planning, resume and status
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlanAction:
    part: BackfillPart
    chunk: int | None
    key: str
    action: str
    """``queued`` (new), ``retry`` (re-queued remainder), ``pending``
    (queued / running), ``done``."""
    run_id: int | None
    symbols: int

    def to_metadata(self) -> dict[str, object]:
        return {
            "part": self.part,
            "chunk": self.chunk,
            "action": self.action,
            "run_id": self.run_id,
            "symbols": self.symbols,
        }


async def backfill_runs(
    session: AsyncSession, universe_code: str, as_of: date | None = None
) -> list[PipelineRun]:
    """``snapshot_backfill`` runs of one universe (and ``as_of``), oldest first."""

    stmt = (
        select(PipelineRun)
        .where(PipelineRun.kind == SNAPSHOT_BACKFILL, PipelineRun.universe_code == universe_code)
        .order_by(PipelineRun.id)
    )
    runs = list((await session.execute(stmt)).scalars().all())
    if as_of is None:
        return runs
    day = as_of.isoformat()
    return [run for run in runs if (run.request_json or {}).get("as_of") == day]


def _family(runs: Sequence[PipelineRun], key: str) -> list[PipelineRun]:
    return [
        run
        for run in runs
        if _RETRY_SUFFIX.sub("", run.idempotency_key or "") == key
    ]


def _symbols_of(run: PipelineRun) -> list[str]:
    value = (run.request_json or {}).get("symbols")
    return [str(item) for item in value] if isinstance(value, list) else []


def _count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0


def _collected(runs: Sequence[PipelineRun]) -> set[str]:
    done: set[str] = set()
    for run in runs:
        value = (run.metadata_json or {}).get("collected_symbols")
        if isinstance(value, list):
            done.update(str(item) for item in value)
    return done


async def enqueue_part(
    session: AsyncSession,
    payload: SnapshotBackfillPayload,
    *,
    runs: Sequence[PipelineRun],
    triggered_by: str,
) -> PlanAction:
    """Queue one part (or chunk) unless it is pending or done; re-queue the remainder
    of a partial / failed attempt under ``<key>:r<n>``."""

    key = idempotency_key(payload)
    family = _family(runs, key)
    latest = max(family, key=lambda run: run.id) if family else None
    if latest is None:
        run, _ = await enqueue_snapshot_job(session, payload, triggered_by=triggered_by, key=key)
        return PlanAction(payload.part, payload.chunk, key, "queued", run.id, len(payload.symbols))
    original = min(family, key=lambda run: run.id)
    if _symbols_of(original) != list(payload.symbols):
        raise ValueError(
            f"backfill plan changed for {key} (membership differs from the planned chunk); "
            "use another --as-of or inspect the membership history"
        )
    if latest.status in {"queued", "running"}:
        return PlanAction(
            payload.part, payload.chunk, key, "pending", latest.id, len(payload.symbols)
        )
    if latest.status == "success":
        return PlanAction(payload.part, payload.chunk, key, "done", latest.id, len(payload.symbols))
    remaining = payload
    if payload.part in BACKFILL_CHUNKED_PARTS:
        left = tuple(symbol for symbol in payload.symbols if symbol not in _collected(family))
        if not left:
            return PlanAction(payload.part, payload.chunk, key, "done", latest.id, 0)
        remaining = payload.model_copy(update={"symbols": left})
    retry_key = f"{key}:r{len(family) + 1}"
    run, _ = await enqueue_snapshot_job(
        session, remaining, triggered_by=triggered_by, key=retry_key
    )
    return PlanAction(
        payload.part, payload.chunk, retry_key, "retry", run.id, len(remaining.symbols)
    )


async def plan_followups(
    session: AsyncSession,
    membership: SnapshotBackfillPayload,
    symbols: Sequence[str],
    *,
    triggered_by: str,
) -> list[PlanAction]:
    """Industry job, then price and financial chunks for the survivorship set."""

    runs = await backfill_runs(session, membership.universe_code, membership.as_of)
    base = membership.model_dump(exclude={"part", "symbols", "chunk"})
    actions = [
        await enqueue_part(
            session,
            SnapshotBackfillPayload(**base, part="industry"),
            runs=runs,
            triggered_by=triggered_by,
        )
    ]
    for part in ("prices", "financials"):
        for index, chunk in enumerate(chunked(sorted(symbols))):
            actions.append(
                await enqueue_part(
                    session,
                    SnapshotBackfillPayload(**base, part=part, symbols=chunk, chunk=index),
                    runs=runs,
                    triggered_by=triggered_by,
                )
            )
    return actions


@dataclass(frozen=True)
class BackfillPlanResult:
    membership: PlanAction
    followups: list[PlanAction]


async def plan_backfill(
    session: AsyncSession, payload: SnapshotBackfillPayload, *, triggered_by: str
) -> BackfillPlanResult:
    """CLI entry: membership first; once it succeeded, every other part."""

    if payload.part != "membership":
        raise ValueError("plan_backfill takes the membership payload")
    runs = await backfill_runs(session, payload.universe_code, payload.as_of)
    membership = await enqueue_part(session, payload, runs=runs, triggered_by=triggered_by)
    followups: list[PlanAction] = []
    if membership.action == "done":
        start = window_start(payload)
        symbols = await backfill_symbols(session, payload.universe_code, start, payload.as_of)
        followups = await plan_followups(session, payload, symbols, triggered_by=triggered_by)
    return BackfillPlanResult(membership, followups)


@dataclass
class PartStatus:
    part: BackfillPart
    jobs: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    total_symbols: int = 0
    done_symbols: int = 0
    failed_symbols: list[str] = field(default_factory=list)
    detail: str = ""


async def backfill_status(
    session: AsyncSession, universe_code: str, as_of: date | None = None
) -> tuple[date | None, list[PartStatus]]:
    """Progress per part for one plan (latest ``as_of`` when not given)."""

    runs = await backfill_runs(session, universe_code)
    if as_of is None:
        dates = [(run.request_json or {}).get("as_of") for run in runs]
        known = sorted(str(day) for day in dates if isinstance(day, str))
        if not known:
            return None, []
        as_of = date.fromisoformat(known[-1])
    day = as_of.isoformat()
    runs = [run for run in runs if (run.request_json or {}).get("as_of") == day]
    statuses: list[PartStatus] = []
    for part in BACKFILL_PARTS:
        part_runs = [run for run in runs if (run.request_json or {}).get("part") == part]
        status = PartStatus(part)
        families: dict[str, list[PipelineRun]] = {}
        for run in part_runs:
            base = _RETRY_SUFFIX.sub("", run.idempotency_key or "")
            families.setdefault(base, []).append(run)
        status.jobs = len(families)
        for family in families.values():
            latest = max(family, key=lambda run: run.id)
            status.statuses[latest.status] = status.statuses.get(latest.status, 0) + 1
            if part in BACKFILL_CHUNKED_PARTS:
                original = min(family, key=lambda run: run.id)
                planned = _symbols_of(original)
                done = _collected(family) & set(planned)
                status.total_symbols += len(planned)
                status.done_symbols += len(done)
                completeness = (latest.metadata_json or {}).get("completeness")
                missing = completeness.get("missing") if isinstance(completeness, dict) else None
                if isinstance(missing, dict):
                    status.failed_symbols.extend(
                        ticker
                        for ticker, info in missing.items()
                        if isinstance(info, dict)
                        and info.get("kind") == "failed"
                        and ticker not in done
                    )
            else:
                metadata = latest.metadata_json or {}
                symbols = metadata.get("symbols")
                if isinstance(symbols, int):
                    status.total_symbols = symbols
                    status.done_symbols = symbols if latest.status == "success" else 0
                if part == "membership" and latest.status == "success":
                    status.detail = (
                        f"current {metadata.get('current')}, removed {metadata.get('removed')}, "
                        f"changes {_count(metadata.get('change_dates'))}"
                    )
                elif latest.error_summary:
                    status.detail = latest.error_summary[:120]
        status.failed_symbols.sort()
        statuses.append(status)
    return as_of, statuses


# --------------------------------------------------------------------------- #
# Estimates (dry run)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PartEstimate:
    part: BackfillPart
    jobs: int
    queries: int
    seconds: float


@dataclass(frozen=True)
class BackfillEstimate:
    universe_code: str
    as_of: date
    window_start: date
    symbols: int
    current: int
    removed: int
    periods: tuple[date, ...]
    parts: tuple[PartEstimate, ...]
    source: str
    """Where the symbol count comes from (stored history or a monthly scan)."""

    @property
    def queries(self) -> int:
        return sum(part.queries for part in self.parts)

    @property
    def seconds(self) -> float:
        return sum(part.seconds for part in self.parts)


def estimate_backfill(
    payload: SnapshotBackfillPayload,
    *,
    symbols: int,
    current: int,
    monthly_queries: int,
    differing_months: int,
    source: str,
) -> BackfillEstimate:
    start = window_start(payload)
    periods = report_periods(
        payload.as_of, annual_years=payload.annual_years, quarters=payload.quarters
    )
    chunks = math.ceil(symbols / CHUNK_SIZE)
    membership_queries = monthly_queries + differing_months * WEEKLY_QUERIES_PER_CHANGE
    industry_queries = len(industry_samples(start, payload.as_of))
    scale = payload.price_years / 5
    financial_queries = symbols * (len(periods) * 4 + 1)
    parts = (
        PartEstimate(
            "membership", 1, membership_queries, membership_queries * SECONDS_CONSTITUENT_QUERY
        ),
        PartEstimate(
            "industry", 1, industry_queries, industry_queries * SECONDS_INDUSTRY_QUERY
        ),
        PartEstimate(
            "prices",
            chunks,
            symbols * len(SERIES),
            symbols * (SECONDS_RAW_5Y + SECONDS_BACKWARD_5Y) * scale,
        ),
        PartEstimate(
            "financials",
            chunks,
            financial_queries,
            symbols * (len(periods) * 4 * SECONDS_TABLE_QUERY + SECONDS_BASIC_QUERY),
        ),
    )
    return BackfillEstimate(
        universe_code=payload.universe_code,
        as_of=payload.as_of,
        window_start=start,
        symbols=symbols,
        current=current,
        removed=symbols - current,
        periods=tuple(periods),
        parts=parts,
        source=source,
    )


async def dry_run_estimate(
    payload: SnapshotBackfillPayload, *, client: BackfillClient
) -> BackfillEstimate:
    """Estimate without fetching data: the symbol set comes from the stored history
    when the membership part already succeeded, otherwise from a monthly
    constituent scan (reference lookups only, no weekly refinement)."""

    start = window_start(payload)
    async with session_scope() as session:
        runs = await backfill_runs(session, payload.universe_code, payload.as_of)
        family = _family(runs, idempotency_key(payload))
        done = any(run.status == "success" for run in family)
        if done:
            symbols = await backfill_symbols(session, payload.universe_code, start, payload.as_of)
            current = len(
                await UniverseRepository(session).members_as_of(
                    payload.universe_code, payload.as_of
                )
            )
            months = len(month_samples(start, payload.as_of))
            return estimate_backfill(
                payload,
                symbols=len(symbols),
                current=current,
                monthly_queries=months,
                differing_months=0,
                source="stored membership history",
            )
    scan = await scan_constituents(
        client, payload.universe_code, start, payload.as_of, refine=False
    )
    union: set[str] = set()
    for snapshot in scan.snapshots:
        union.update(snapshot.members)
    latest = scan.snapshots[-1] if scan.snapshots else None
    return estimate_backfill(
        payload,
        symbols=len(union),
        current=len(latest.members) if latest else 0,
        monthly_queries=scan.monthly_queries,
        differing_months=scan.differing_months,
        source=f"monthly constituent scan ({scan.monthly_queries} reference queries)",
    )

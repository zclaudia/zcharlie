"""baostock breadth collectors for the target pool (P1-A7, decision D6).

``market_snapshot`` (provider ``baostock``)
    Per listing and series (``none`` raw / ``backward`` 后复权) the collector
    asks for ``[last stored baostock trade date + 1, trade_date]``; a listing
    without baostock history starts at ``trade_date`` (deep history is the
    backfill's job, P1-A4). baostock answers any window in one query, so one
    listing costs at most two queries. Rows carry ``pe_ttm`` / ``pb_mrq`` /
    ``ps_ttm`` / ``turnover`` / ``trade_status`` / ``is_st``; suspended days
    (``trade_status=0``) are stored as baostock reports them and listed in the
    run metadata. Total shares are *not* stored on market rows: factor-time
    market cap uses the latest baostock financial ``total_shares`` available
    on the trade date (:meth:`FinancialSnapshotRepository.total_shares_as_of`).

``financial_snapshot`` (provider ``baostock``)
    For members without a baostock capture of the period: profit / balance /
    cash-flow / growth merged into canonical fields from the field registry;
    ``availability="actual"``, ``available_from`` = baostock ``pubDate``.
    An empty answer means "not disclosed yet" -> ``pending``.
    Disclosure overdue detection (P1-A6, :mod:`investment_agent.snapshots.disclosure`):
    for the ``universe`` scope, once ``as_of`` is past the period's statutory
    deadline every member still ``pending`` is recorded in ``report_overdue``;
    a stored report resolves its open record with the actual pubDate.

Both run inside one baostock session (one login; calls are serialized by the
process-wide session lock, so no token bucket is needed). Failures are
isolated per listing; after :data:`MAX_CONSECUTIVE_TRANSIENT` consecutive
transient failures the service is considered down and the remaining listings
are marked failed without further calls, so the worker can retry the job later.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

import structlog

from investment_agent.providers.baostock_market import (
    AdjustKind,
    DailyBarSeries,
    QuarterlyFinancials,
    quarter_of,
)
from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.errors import ProviderUnavailableError
from investment_agent.research.retry_policy import is_transient_error
from investment_agent.snapshots.collect import (
    ArtifactWriter,
    CollectionReport,
    Completeness,
    error_reason,
    fence_writes,
)
from investment_agent.snapshots.disclosure import is_past_deadline, statutory_deadline
from investment_agent.snapshots.jobs import (
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
    period_kind,
)
from investment_agent.snapshots.scope import scope_members
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository

log = structlog.get_logger(__name__)

BAOSTOCK_PROVIDER = "baostock"
SERIES: tuple[AdjustKind, ...] = ("none", "backward")
MAX_CONSECUTIVE_TRANSIENT = 5
_MAX_LISTED_FLAGS = 50


class BaostockCollectorClient(Protocol):
    def session(self) -> AbstractAsyncContextManager[None]: ...

    async def fetch_daily_bars(
        self, symbol: str, start: date, end: date, adjust: AdjustKind
    ) -> DailyBarSeries: ...

    async def fetch_quarterly_financials(
        self, symbol: str, year: int, quarter: int
    ) -> QuarterlyFinancials | None: ...


@dataclass
class TransientBreaker:
    """Stops calling baostock after consecutive transient failures.

    When it trips, the cross-run :class:`CircuitBreaker` (if wired) opens for
    the whole provider, so the next baostock job fails fast with ``partial``
    instead of walking into the same outage.
    """

    limit: int = MAX_CONSECUTIVE_TRANSIENT
    consecutive: int = 0
    reason: str | None = None
    circuit: CircuitBreaker | None = None

    def success(self) -> None:
        self.consecutive = 0

    def failure(self, exc: BaseException) -> None:
        if not is_transient_error(exc):
            self.consecutive = 0
            return
        self.consecutive += 1
        if self.consecutive >= self.limit and self.reason is None:
            self.reason = (
                f"skipped: baostock unavailable after {self.consecutive} consecutive "
                f"transient failures ({error_reason(exc)})"
            )
            if self.circuit is not None:
                self.circuit.open_now(BAOSTOCK_PROVIDER, exc)


@dataclass
class _Flags:
    suspended: list[str] = field(default_factory=list)
    st: list[str] = field(default_factory=list)

    def to_metadata(self) -> dict[str, object]:
        return {
            "suspended_count": len(self.suspended),
            "suspended": sorted(self.suspended)[:_MAX_LISTED_FLAGS],
            "st_count": len(self.st),
            "st": sorted(self.st)[:_MAX_LISTED_FLAGS],
        }


def _untouched(completeness: Completeness) -> list[str]:
    """Expected tickers neither collected nor explained yet (in expected order)."""

    return [
        ticker
        for ticker in completeness.expected
        if ticker not in completeness.collected and ticker not in completeness.missing
    ]


def next_start(last_stored: date | None, trade_date: date) -> date:
    """First trade date to request for one series (incremental)."""

    return last_stored + timedelta(days=1) if last_stored is not None else trade_date


def write_daily_artifact(
    artifacts: ArtifactWriter,
    name: str,
    ticker: str,
    fetched: Mapping[AdjustKind, DailyBarSeries],
) -> str:
    """Raw k-data answers of one listing (all series) -> ``raw_ref``."""

    return artifacts.write(
        name,
        {
            "provider": BAOSTOCK_PROVIDER,
            "ticker": ticker,
            "series": {
                adjust: {
                    "start": series.start.isoformat(),
                    "end": series.end.isoformat(),
                    "captured_at": series.captured_at.isoformat(),
                    "rows": [dict(row) for row in series.raw_rows],
                }
                for adjust, series in fetched.items()
            },
        },
    )


async def store_daily_bars(
    market: MarketSnapshotRepository,
    *,
    listing_id: int,
    series: DailyBarSeries,
    raw_ref: str,
    is_backfill: bool = False,
) -> int:
    """Insert every bar of one baostock series; returns the row count."""

    for bar in series.bars:
        await market.insert(
            listing_id=listing_id,
            trade_date=bar.trade_date,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            amount=bar.amount,
            adjust=series.adjust,
            provider=BAOSTOCK_PROVIDER,
            captured_at=series.captured_at,
            raw_ref=raw_ref,
            pe_ttm=bar.pe_ttm,
            pb_mrq=bar.pb_mrq,
            ps_ttm=bar.ps_ttm,
            turnover=bar.turnover,
            trade_status=bar.trade_status,
            is_st=bar.is_st,
            is_backfill=is_backfill,
        )
    return len(series.bars)


async def collect_baostock_market(
    payload: MarketSnapshotPayload,
    *,
    client: BaostockCollectorClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    now: Callable[[], datetime],
    circuit: CircuitBreaker | None = None,
) -> CollectionReport:
    if payload.provider != BAOSTOCK_PROVIDER:
        raise ValueError(f"baostock market collector got provider {payload.provider!r}")
    del now  # captured_at comes from the provider client (its clock)
    trade_date = payload.trade_date
    async with session_scope() as session:
        listing_by_ticker = await scope_members(session, payload, trade_date)
        listing_ids = list(listing_by_ticker.values())
        repository = MarketSnapshotRepository(session)
        last: dict[str, dict[int, date]] = {
            adjust: await repository.latest_trade_dates(
                listing_ids, adjust=adjust, provider=BAOSTOCK_PROVIDER
            )
            for adjust in SERIES
        }

    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=BAOSTOCK_PROVIDER)
    rows_inserted = dict.fromkeys(SERIES, 0)
    skipped_rows: dict[str, list[str]] = {}
    flags = _Flags()
    breaker = TransientBreaker(circuit=circuit)
    queries = 0
    members = list(listing_by_ticker.items())

    try:
        async with client.session():
            for position, (ticker, listing_id) in enumerate(members):
                if breaker.reason is not None:
                    completeness.mark_missing(ticker, "failed", breaker.reason)
                    continue
                plan = {
                    adjust: start
                    for adjust in SERIES
                    if (start := next_start(last[adjust].get(listing_id), trade_date)) <= trade_date
                }
                if not plan:
                    continue  # already up to date
                try:
                    fetched: dict[AdjustKind, DailyBarSeries] = {}
                    for adjust, start in plan.items():
                        queries += 1
                        fetched[adjust] = await client.fetch_daily_bars(
                            ticker, start, trade_date, adjust
                        )
                    ref = write_daily_artifact(
                        artifacts, f"daily-{ticker}-{trade_date.isoformat()}", ticker, fetched
                    )
                    async with session_scope() as session:
                        await fence_writes(session, lease)
                        market = MarketSnapshotRepository(session)
                        for adjust, series in fetched.items():
                            rows_inserted[adjust] += await store_daily_bars(
                                market, listing_id=listing_id, series=series, raw_ref=ref
                            )
                except LeaseLostError:
                    raise
                except ProviderUnavailableError as exc:
                    # Circuit open (negative cache): nothing was called; stop here.
                    report.stop_for_provider(
                        exc, [name for name, _ in members[position:]], failures=exc.fail_count
                    )
                    break
                except Exception as exc:
                    reason = error_reason(exc)
                    report.record_failure(exc)
                    breaker.failure(exc)
                    log.warning("snapshot.baostock_market.failed", ticker=ticker, error=reason)
                    completeness.mark_missing(ticker, "failed", reason)
                    continue
                breaker.success()
                for adjust, series in fetched.items():
                    if series.skipped:
                        skipped_rows.setdefault(ticker, []).extend(
                            f"{adjust}:{day}" for day in series.skipped
                        )
                    if series.bars:
                        newest = series.bars[-1].trade_date
                        current = last[adjust].get(listing_id)
                        if current is None or newest > current:
                            last[adjust][listing_id] = newest
                raw_series = fetched.get("none")
                today = (
                    next((bar for bar in raw_series.bars if bar.trade_date == trade_date), None)
                    if raw_series is not None
                    else None
                )
                if today is not None and today.suspended:
                    flags.suspended.append(ticker)
                if today is not None and today.is_st:
                    flags.st.append(ticker)

    except ProviderUnavailableError as exc:
        # The circuit refused the baostock login: nothing was called.
        report.stop_for_provider(exc, _untouched(completeness), failures=exc.fail_count)
    for ticker, listing_id in listing_by_ticker.items():
        if all(
            (stored := last[adjust].get(listing_id)) is not None and stored >= trade_date
            for adjust in SERIES
        ):
            completeness.mark_collected(ticker)
        elif ticker not in completeness.missing:  # failed / stopped keep their reason
            completeness.mark_missing(
                ticker,
                "no_data",
                f"no baostock bar for {trade_date.isoformat()} (not listed or not trading)",
            )

    report.metadata = {
        "provider": BAOSTOCK_PROVIDER,
        "scope": payload.scope,
        "universe_code": payload.universe_code,
        "trade_date": trade_date.isoformat(),
        "series": list(SERIES),
        "baostock_queries": queries,
        "rows_inserted": rows_inserted,
        "flags": flags.to_metadata(),
        "skipped_rows": skipped_rows,
        "aborted": breaker.reason,
        "total_shares": (
            "not stored on baostock market rows; factor-time market cap uses the latest "
            "baostock financial total_shares with available_from <= trade_date"
        ),
    }
    return report


def _number(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return float(value) if value.is_finite() else None


def financial_fields(record: QuarterlyFinancials) -> dict[str, object]:
    """``financial_snapshot.fields`` for a baostock record."""

    return {
        **record.fields,
        "missing_fields": record.missing_fields,
        "source_values": {name: _number(value) for name, value in record.source_values.items()},
        "pub_date": record.pub_date.isoformat(),
        "stat_date": record.stat_date.isoformat(),
        "tables": list(record.tables),
    }


def write_financial_artifact(artifacts: ArtifactWriter, record: QuarterlyFinancials) -> str:
    """Raw quarterly table answers of one listing and period -> ``raw_ref``."""

    year, quarter = quarter_of(record.report_period)
    return artifacts.write(
        f"financial-{record.ticker}-{record.report_period.isoformat()}",
        {
            "provider": BAOSTOCK_PROVIDER,
            "ticker": record.ticker,
            "year": year,
            "quarter": quarter,
            "captured_at": record.captured_at.isoformat(),
            "tables": {table: dict(row) for table, row in record.raw_rows.items()},
        },
    )


async def store_financial(
    repository: FinancialSnapshotRepository,
    *,
    listing_id: int,
    record: QuarterlyFinancials,
    raw_ref: str,
    is_backfill: bool = False,
    extra_fields: Mapping[str, object] | None = None,
) -> None:
    """One baostock period: ``availability="actual"``, ``available_from`` = pubDate."""

    await repository.insert(
        listing_id=listing_id,
        report_period=record.report_period,
        period_kind=period_kind(record.report_period),
        fields={**financial_fields(record), **(extra_fields or {})},
        provider=BAOSTOCK_PROVIDER,
        captured_at=record.captured_at,
        availability="actual",
        available_from=record.pub_date,
        raw_ref=raw_ref,
        is_backfill=is_backfill,
    )


async def collect_baostock_financial(
    payload: FinancialSnapshotPayload,
    *,
    client: BaostockCollectorClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    now: Callable[[], datetime],
    run_id: int | None = None,
    circuit: CircuitBreaker | None = None,
) -> CollectionReport:
    if payload.provider != BAOSTOCK_PROVIDER:
        raise ValueError(f"baostock financial collector got provider {payload.provider!r}")
    del now
    report_period = payload.report_period
    kind = period_kind(report_period)
    year, quarter = quarter_of(report_period)
    async with session_scope() as session:
        listing_by_ticker = await scope_members(session, payload, payload.as_of)
        present = await FinancialSnapshotRepository(session).listings_with_period(
            list(listing_by_ticker.values()), report_period, provider=BAOSTOCK_PROVIDER
        )

    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=BAOSTOCK_PROVIDER)
    todo: list[str] = []
    for ticker, listing_id in listing_by_ticker.items():
        if listing_id in present:
            completeness.mark_collected(ticker)
        else:
            todo.append(ticker)

    breaker = TransientBreaker(circuit=circuit)
    stored: dict[str, str] = {}
    resolved: list[str] = []
    try:
        async with client.session():
            for position, ticker in enumerate(todo):
                if breaker.reason is not None:
                    completeness.mark_missing(ticker, "failed", breaker.reason)
                    continue
                listing_id = listing_by_ticker[ticker]
                try:
                    record = await client.fetch_quarterly_financials(ticker, year, quarter)
                    if record is None:
                        completeness.mark_missing(
                            ticker, "pending", f"{report_period.isoformat()} not yet disclosed"
                        )
                        breaker.success()
                        continue
                    ref = write_financial_artifact(artifacts, record)
                    async with session_scope() as session:
                        await fence_writes(session, lease)
                        await store_financial(
                            FinancialSnapshotRepository(session),
                            listing_id=listing_id,
                            record=record,
                            raw_ref=ref,
                        )
                        if await ReportOverdueRepository(session).resolve(
                            listing_id=listing_id,
                            report_period=report_period,
                            resolved_on=record.pub_date,
                            run_id=run_id,
                        ):
                            resolved.append(ticker)
                except LeaseLostError:
                    raise
                except ProviderUnavailableError as exc:
                    report.stop_for_provider(exc, todo[position:], failures=exc.fail_count)
                    break
                except Exception as exc:
                    reason = error_reason(exc)
                    report.record_failure(exc)
                    breaker.failure(exc)
                    log.warning("snapshot.baostock_financial.failed", ticker=ticker, error=reason)
                    completeness.mark_missing(ticker, "failed", reason)
                    continue
                breaker.success()
                completeness.mark_collected(ticker)
                stored[ticker] = record.pub_date.isoformat()

    except ProviderUnavailableError as exc:
        # The circuit refused the baostock login: nothing was called.
        report.stop_for_provider(exc, _untouched(completeness), failures=exc.fail_count)
    overdue = await _detect_overdue(
        payload,
        completeness=completeness,
        listing_by_ticker=listing_by_ticker,
        lease=lease,
        run_id=run_id,
    )
    overdue["resolved"] = sorted(resolved)

    report.metadata = {
        "provider": BAOSTOCK_PROVIDER,
        "scope": payload.scope,
        "universe_code": payload.universe_code,
        "report_period": report_period.isoformat(),
        "period_kind": kind,
        "as_of": payload.as_of.isoformat(),
        "already_present": len(present),
        "requested": len(todo),
        "stored": len(stored),
        "pub_dates": dict(sorted(stored.items())),
        "aborted": breaker.reason,
        "availability": "actual: available_from = baostock pubDate",
        "report_overdue": overdue,
    }
    return report


async def _detect_overdue(
    payload: FinancialSnapshotPayload,
    *,
    completeness: Completeness,
    listing_by_ticker: dict[str, int],
    lease: RunLease | None,
    run_id: int | None,
) -> dict[str, object]:
    """Record members still without the period after its deadline (target pool only).

    Only ``pending`` members (baostock answered "not disclosed") count;
    failed / skipped members are unknown and checked again by the next run.
    """

    period = payload.report_period
    deadline = statutory_deadline(period)
    result: dict[str, object] = {
        "deadline": deadline.isoformat(),
        "checked": False,
        "detected": [],
        "open": [],
    }
    if payload.scope != "universe" or not is_past_deadline(period, payload.as_of):
        return result
    detected: list[str] = []
    async with session_scope() as session:
        await fence_writes(session, lease)
        repository = ReportOverdueRepository(session)
        for ticker in completeness.pending:
            if await repository.record(
                listing_id=listing_by_ticker[ticker],
                report_period=period,
                deadline=deadline,
                detected_on=payload.as_of,
                provider=BAOSTOCK_PROVIDER,
                run_id=run_id,
            ):
                detected.append(ticker)
        ticker_by_listing = {listing_id: ticker for ticker, listing_id in listing_by_ticker.items()}
        open_rows = [
            row
            for row in await repository.for_period(period, list(ticker_by_listing))
            if row.resolved_on is None
        ]
    if detected:
        log.warning(
            "snapshot.report_overdue.detected",
            report_period=period.isoformat(),
            deadline=deadline.isoformat(),
            tickers=detected,
        )
    result.update(
        checked=True,
        detected=sorted(detected),
        open=sorted(ticker_by_listing[row.listing_id] for row in open_rows),
    )
    return result

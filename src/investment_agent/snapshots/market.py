"""iFinD ``market_snapshot`` (watchlist scope): incremental daily bars + total shares.

Target pools are collected from baostock (:mod:`investment_agent.snapshots.breadth`,
decision D6); this collector only runs for ``provider="ifind"``,
``scope="watchlist"`` payloads, so the gateway quota is spent on a few dozen
watchlist stocks, never on the whole target pool.

Two immutable series are stored per listing:

* ``adjust="none"`` - raw prices, the basis for valuation (price x shares);
* ``adjust="backward"`` (后复权) - backward-adjusted prices for returns and
  volatility. Backward adjustment keeps past values fixed, so captures never
  go stale. Forward-adjusted prices are deliberately not stored: they rewrite
  the whole history on every dividend.

Collection is incremental per (listing, series): it starts the day after the
last stored trade date (or at ``trade_date`` for a listing without history;
deep history is the backfill job's business) and requests at most three years
per call. Tickers sharing the same window are batched three per call.

Total shares: ``/stock/info`` is called (three tickers per call) for the
listings that need a raw bar this run; the value is stored on the
``trade_date`` raw row only (older rows of the window keep ``None``) and
``raw_ref`` points at the history response. Factor-time market cap uses the
latest baostock financial ``total_shares`` instead (see
:meth:`FinancialSnapshotRepository.total_shares_as_of`); these iFinD values
serve watchlist cross-checks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import structlog

from investment_agent.research.ifind_chain import IfindProxyResult
from investment_agent.snapshots.collect import (
    IFIND_PROVIDER,
    ArtifactWriter,
    CollectionReport,
    Completeness,
    ProxyBatchCaller,
    chunked,
    error_reason,
    fence_writes,
    rows_by_ticker,
)
from investment_agent.snapshots.jobs import MarketSnapshotPayload
from investment_agent.snapshots.scope import scope_members
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease

log = structlog.get_logger(__name__)

HISTORY_PATH = "/stock/history"
INFO_PATH = "/stock/info"
SERIES: tuple[str, ...] = ("none", "backward")
MAX_WINDOW_YEARS = 3
TOTAL_SHARES_FIELD = "ths_total_shares_stock"


@dataclass(frozen=True)
class HistoryRequest:
    adjust: str
    start: date
    end: date
    tickers: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "ticker": ",".join(self.tickers),
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "interval": "D",
            "adjust": self.adjust,
        }


@dataclass(frozen=True)
class Bar:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass
class _Collector:
    caller: ProxyBatchCaller
    artifacts: ArtifactWriter
    lease: RunLease | None
    now: Callable[[], datetime]
    rows_inserted: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SERIES, 0))


def add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # 29 Feb
        return value.replace(year=value.year + years, day=28)


def history_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into consecutive windows of at most three years."""

    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(end, add_years(cursor, MAX_WINDOW_YEARS) - timedelta(days=1))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def plan_history_requests(
    starts: dict[str, dict[str, date]], end: date
) -> list[HistoryRequest]:
    """Group tickers by (series, window) and batch them three per request.

    ``starts`` maps series -> ticker -> first missing trade date. Requests are
    ordered chronologically per series so a ticker's older window is always
    stored before its newer one.
    """

    groups: dict[tuple[str, date, date], list[str]] = {}
    for adjust, by_ticker in starts.items():
        for ticker, start in by_ticker.items():
            for window in history_windows(start, end):
                groups.setdefault((adjust, *window), []).append(ticker)
    requests: list[HistoryRequest] = []
    for (adjust, window_start, window_end), tickers in sorted(
        groups.items(), key=lambda item: (SERIES.index(item[0][0]), item[0][1], item[0][2])
    ):
        for batch in chunked(sorted(tickers)):
            requests.append(HistoryRequest(adjust, window_start, window_end, tuple(batch)))
    return requests


def _decimal(value: object) -> Decimal | None:
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text or text in {"--", "None", "null", "nan"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def parse_bar(row: dict[str, str]) -> Bar | None:
    try:
        trade_date = datetime.strptime(row.get("time", "").strip(), "%Y%m%d").date()
    except ValueError:
        return None
    values = [_decimal(row.get(name)) for name in ("open", "high", "low", "close", "volume")]
    if any(value is None for value in values):
        return None
    open_, high, low, close, volume = (value for value in values if value is not None)
    return Bar(trade_date, open_, high, low, close, volume)


async def collect_market_snapshot(
    payload: MarketSnapshotPayload,
    *,
    caller: ProxyBatchCaller,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    now: Callable[[], datetime],
) -> CollectionReport:
    if payload.provider != IFIND_PROVIDER:
        raise ValueError(f"iFinD market collector got provider {payload.provider!r}")
    trade_date = payload.trade_date
    async with session_scope() as session:
        listing_by_ticker = await scope_members(session, payload, trade_date)
        listing_ids = list(listing_by_ticker.values())
        repository = MarketSnapshotRepository(session)
        last = {
            adjust: await repository.latest_trade_dates(
                listing_ids, adjust=adjust, provider=IFIND_PROVIDER
            )
            for adjust in SERIES
        }

    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness)
    collector = _Collector(caller, artifacts, lease, now)

    starts: dict[str, dict[str, date]] = {adjust: {} for adjust in SERIES}
    for ticker, listing_id in listing_by_ticker.items():
        for adjust in SERIES:
            previous = last[adjust].get(listing_id)
            start = previous + timedelta(days=1) if previous is not None else trade_date
            if start <= trade_date:
                starts[adjust][ticker] = start

    shares, share_errors = await _fetch_total_shares(
        collector, sorted(starts["none"]), trade_date
    )

    requests = plan_history_requests(starts, trade_date)
    failed_series: set[tuple[str, str]] = set()
    for index, request in enumerate(requests, start=1):
        tickers = [t for t in request.tickers if (t, request.adjust) not in failed_series]
        if not tickers:
            continue
        request = HistoryRequest(request.adjust, request.start, request.end, tuple(tickers))
        try:
            result = await caller.call(HISTORY_PATH, request.payload())
            ref = artifacts.write(
                f"history-{request.adjust}-{request.start.isoformat()}-b{index:03d}",
                {"path": HISTORY_PATH, "request": request.payload(), "body": result.body},
            )
            inserted = await _store_bars(
                collector,
                request=request,
                result=result,
                ref=ref,
                listing_by_ticker=listing_by_ticker,
                shares=shares,
                trade_date=trade_date,
            )
        except LeaseLostError:
            raise
        except Exception as exc:
            reason = error_reason(exc)
            report.record_failure(exc)
            log.warning(
                "snapshot.market.batch_failed",
                adjust=request.adjust,
                tickers=tickers,
                error=reason,
            )
            for ticker in tickers:
                failed_series.add((ticker, request.adjust))
                completeness.mark_missing(ticker, "failed", f"{request.adjust}: {reason}")
            continue
        for ticker, latest in inserted.items():
            listing_id = listing_by_ticker[ticker]
            current = last[request.adjust].get(listing_id)
            if current is None or latest > current:
                last[request.adjust][listing_id] = latest

    for ticker, listing_id in listing_by_ticker.items():
        if all(
            (stored := last[adjust].get(listing_id)) is not None and stored >= trade_date
            for adjust in SERIES
        ):
            completeness.mark_collected(ticker)
        else:
            completeness.mark_missing(
                ticker,
                "no_data",
                f"no bar for {trade_date.isoformat()} (suspended or not trading)",
            )

    report.metadata = {
        "provider": IFIND_PROVIDER,
        "scope": payload.scope,
        "universe_code": payload.universe_code,
        "trade_date": trade_date.isoformat(),
        "series": list(SERIES),
        "history_requests": len(requests),
        "proxy_calls": caller.calls,
        "rows_inserted": dict(collector.rows_inserted),
        "total_shares": {
            "rule": "fetched via /stock/info and stored on the trade-date raw row only",
            "fetched": sorted(shares),
            "errors": share_errors,
        },
    }
    return report


async def _fetch_total_shares(
    collector: _Collector, tickers: Sequence[str], trade_date: date
) -> tuple[dict[str, Decimal], dict[str, str]]:
    shares: dict[str, Decimal] = {}
    errors: dict[str, str] = {}
    for index, batch in enumerate(chunked(tickers), start=1):
        request = {"ticker": ",".join(batch), "request_time": trade_date.isoformat()}
        try:
            result = await collector.caller.call(INFO_PATH, request)
        except Exception as exc:
            # Missing shares only degrade the cross-check; bars still count.
            reason = error_reason(exc)
            log.warning("snapshot.market.info_failed", tickers=batch, error=reason)
            errors.update(dict.fromkeys(batch, reason))
            continue
        collector.artifacts.write(
            f"info-{trade_date.isoformat()}-b{index:03d}",
            {"path": INFO_PATH, "request": request, "body": result.body},
        )
        grouped = rows_by_ticker(result.rows)
        for ticker in batch:
            rows = grouped.get(ticker, [])
            value = _decimal(rows[0].get(TOTAL_SHARES_FIELD)) if rows else None
            if value is None or value <= 0:
                errors[ticker] = "no total shares in /stock/info response"
                continue
            shares[ticker] = value
    return shares, errors


async def _store_bars(
    collector: _Collector,
    *,
    request: HistoryRequest,
    result: IfindProxyResult,
    ref: str,
    listing_by_ticker: dict[str, int],
    shares: dict[str, Decimal],
    trade_date: date,
) -> dict[str, date]:
    grouped = rows_by_ticker(result.rows)
    parsed: dict[str, list[Bar]] = {}
    for ticker in request.tickers:
        by_day = {
            parsed_bar.trade_date: parsed_bar
            for row in grouped.get(ticker, [])
            if (parsed_bar := parse_bar(row)) is not None
            and request.start <= parsed_bar.trade_date <= request.end
        }
        parsed[ticker] = [by_day[day] for day in sorted(by_day)]

    captured_at = collector.now()
    latest: dict[str, date] = {}
    async with session_scope() as session:
        await fence_writes(session, collector.lease)
        repository = MarketSnapshotRepository(session)
        for ticker, bars in parsed.items():
            for bar in bars:
                total_shares = (
                    shares.get(ticker)
                    if request.adjust == "none" and bar.trade_date == trade_date
                    else None
                )
                await repository.insert(
                    listing_id=listing_by_ticker[ticker],
                    trade_date=bar.trade_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    adjust=request.adjust,
                    total_shares=total_shares,
                    provider=IFIND_PROVIDER,
                    captured_at=captured_at,
                    raw_ref=ref,
                )
                collector.rows_inserted[request.adjust] += 1
            if bars:
                latest[ticker] = bars[-1].trade_date
    return latest

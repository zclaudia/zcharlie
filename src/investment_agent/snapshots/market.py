"""``market_snapshot``: incremental daily bars and total shares for a universe.

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

Total shares rule: ``/stock/info`` is called (three tickers per call) only for
listings whose latest known ``total_shares`` was fetched more than
:data:`TOTAL_SHARES_REFRESH_DAYS` days before ``trade_date`` (or never). The
fetched value is written onto that trade date's raw row; every other raw row
carries the latest known value. The value's provenance - the info response
file and the date it was fetched for - is appended to the row's ``raw_ref``
as ``#total_shares=<ref>@<YYYY-MM-DD>``, which is also how the next run finds
the source date. Rows dated before the first known fetch get ``None``.
"""

from __future__ import annotations

import re
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
    SnapshotPreconditionError,
    chunked,
    error_reason,
    fence_writes,
    rows_by_ticker,
)
from investment_agent.snapshots.jobs import MarketSnapshotPayload
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease
from investment_agent.storage.repositories.universe import UniverseRepository

log = structlog.get_logger(__name__)

HISTORY_PATH = "/stock/history"
INFO_PATH = "/stock/info"
SERIES: tuple[str, ...] = ("none", "backward")
MAX_WINDOW_YEARS = 3
TOTAL_SHARES_REFRESH_DAYS = 7
TOTAL_SHARES_FIELD = "ths_total_shares_stock"

_TOTAL_SHARES_MARK = "#total_shares="
_TOTAL_SHARES_REF = re.compile(r"#total_shares=(?P<ref>.*)@(?P<date>\d{4}-\d{2}-\d{2})$")


@dataclass(frozen=True)
class TotalSharesSource:
    value: Decimal
    ref: str | None
    as_of: date | None


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


def format_raw_ref(history_ref: str, source: TotalSharesSource | None) -> str:
    if source is None or source.ref is None or source.as_of is None:
        return history_ref
    return f"{history_ref}{_TOTAL_SHARES_MARK}{source.ref}@{source.as_of.isoformat()}"


def parse_total_shares_source(raw_ref: str | None) -> tuple[str | None, date | None]:
    """Recover ``(info_ref, fetched_for)`` from a raw row's ``raw_ref``."""

    if not raw_ref:
        return None, None
    match = _TOTAL_SHARES_REF.search(raw_ref)
    if match is None:
        return None, None
    try:
        return match.group("ref"), date.fromisoformat(match.group("date"))
    except ValueError:
        return match.group("ref"), None


def total_shares_stale(source: TotalSharesSource | None, trade_date: date) -> bool:
    """Whether ``/stock/info`` must be called again for ``trade_date``."""

    if source is None or source.as_of is None:
        return True
    return (trade_date - source.as_of).days > TOTAL_SHARES_REFRESH_DAYS


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
    trade_date = payload.trade_date
    async with session_scope() as session:
        members = await UniverseRepository(session).members_as_of(
            payload.universe_code, trade_date
        )
        if not members:
            raise SnapshotPreconditionError(
                f"{payload.universe_code} has no members on {trade_date.isoformat()}; "
                "run universe_sync first"
            )
        listing_ids = [member.listing_id for member in members]
        repository = MarketSnapshotRepository(session)
        last = {
            adjust: await repository.latest_trade_dates(listing_ids, adjust=adjust)
            for adjust in SERIES
        }
        share_rows = await repository.latest_total_shares(listing_ids)

    listing_by_ticker = {member.ticker: member.listing_id for member in members}
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

    known: dict[str, TotalSharesSource] = {}
    for ticker, listing_id in listing_by_ticker.items():
        row = share_rows.get(listing_id)
        if row is not None and row.total_shares is not None:
            ref, fetched_for = parse_total_shares_source(row.raw_ref)
            known[ticker] = TotalSharesSource(row.total_shares, ref, fetched_for)

    stale = sorted(
        ticker
        for ticker in starts["none"]
        if total_shares_stale(known.get(ticker), trade_date)
    )
    fresh, share_errors = await _refresh_total_shares(collector, stale, trade_date)

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
                fresh=fresh,
                known=known,
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
        "universe_code": payload.universe_code,
        "trade_date": trade_date.isoformat(),
        "series": list(SERIES),
        "history_requests": len(requests),
        "proxy_calls": caller.calls,
        "rows_inserted": dict(collector.rows_inserted),
        "total_shares": {
            "rule": (
                f"refresh via /stock/info when older than {TOTAL_SHARES_REFRESH_DAYS} days; "
                "otherwise carry the latest known value"
            ),
            "refreshed": sorted(fresh),
            "errors": share_errors,
        },
    }
    return report


async def _refresh_total_shares(
    collector: _Collector, tickers: Sequence[str], trade_date: date
) -> tuple[dict[str, TotalSharesSource], dict[str, str]]:
    fresh: dict[str, TotalSharesSource] = {}
    errors: dict[str, str] = {}
    for index, batch in enumerate(chunked(tickers), start=1):
        request = {"ticker": ",".join(batch), "request_time": trade_date.isoformat()}
        try:
            result = await collector.caller.call(INFO_PATH, request)
        except Exception as exc:
            # Missing shares only degrade market-cap freshness; bars still count.
            reason = error_reason(exc)
            log.warning("snapshot.market.info_failed", tickers=batch, error=reason)
            errors.update(dict.fromkeys(batch, reason))
            continue
        ref = collector.artifacts.write(
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
            fresh[ticker] = TotalSharesSource(value, ref, trade_date)
    return fresh, errors


async def _store_bars(
    collector: _Collector,
    *,
    request: HistoryRequest,
    result: IfindProxyResult,
    ref: str,
    listing_by_ticker: dict[str, int],
    fresh: dict[str, TotalSharesSource],
    known: dict[str, TotalSharesSource],
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
                source: TotalSharesSource | None = None
                if request.adjust == "none":
                    source = _shares_for(ticker, bar.trade_date, fresh, known, trade_date)
                await repository.insert(
                    listing_id=listing_by_ticker[ticker],
                    trade_date=bar.trade_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    adjust=request.adjust,
                    total_shares=source.value if source is not None else None,
                    provider=IFIND_PROVIDER,
                    captured_at=captured_at,
                    raw_ref=format_raw_ref(ref, source),
                )
                collector.rows_inserted[request.adjust] += 1
            if bars:
                latest[ticker] = bars[-1].trade_date
    return latest


def _shares_for(
    ticker: str,
    day: date,
    fresh: dict[str, TotalSharesSource],
    known: dict[str, TotalSharesSource],
    trade_date: date,
) -> TotalSharesSource | None:
    refreshed = fresh.get(ticker)
    if refreshed is not None and day >= trade_date:
        return refreshed
    carried = known.get(ticker)
    if carried is not None and (carried.as_of is None or carried.as_of <= day):
        return carried
    return None

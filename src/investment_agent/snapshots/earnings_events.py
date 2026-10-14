"""``earnings_event`` collector (mrd-intake C1 / C2, decision D3).

Three parts, all from Eastmoney via AkShare
(:class:`~investment_agent.providers.akshare_eastmoney.AkshareEastmoneyClient`,
``provider="akshare"``):

``forecast``
    One whole-market ``stock_yjyg_em`` call for the payload's report period,
    filtered to the scope (target pool ∪ enabled watchlist ∪ open candidates
    ∪ holdings). A (listing, period, notice date) already stored is skipped;
    new notices are inserted into ``earnings_forecast`` and, when the listing
    is in one of the user's pools, listed in the run metadata as
    ``new_forecasts`` (the ``info`` notification of C2).
``schedule``
    One ``stock_yysj_em`` call for the period, filtered the same way. The
    appointment is appended to ``disclosure_schedule`` when it is the first
    capture or its scheduled / actual date changed. Appointments of the
    user's pools falling within the next :data:`UPCOMING_TRADING_DAYS`
    trading days are listed as ``upcoming_disclosures``; open
    ``report_overdue`` rows whose ``actual_date`` is now known are listed
    under ``report_overdue_reconcile`` (secondary evidence only: the baostock
    ``pubDate`` still resolves them, see :func:`reconcile_overdue`).
``segments``
    One ``stock_zygc_em`` call per listing of the user's pools (1 s apart);
    the latest :data:`MAX_SEGMENT_PERIODS` report periods are stored in
    ``business_segment`` unless the rows equal the latest stored capture of
    the period (``unchanged``).

Completeness: for the two whole-market parts every scope member counts as
collected once the answer arrived (most listings never issue a forecast);
a failed call marks all of them ``failed`` (the run is ``failed`` and
retried by the worker). The per-stock part isolates failures per listing:
:data:`~investment_agent.snapshots.eastmoney.MAX_CONSECUTIVE_FAILURES`
consecutive failures stop the run (``stopped``, ``partial``) and trip the
cross-run circuit for ``akshare`` (A1); an already open circuit
(:class:`ProviderUnavailableError`) stops before the first call.

Point in time: a notice / actual disclosure date after the capture date is
refused by the provider parser (:class:`LookaheadDataError`); appointment
dates are forward looking by nature.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Protocol

import structlog

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.providers.akshare_eastmoney import (
    PROVIDER as AKSHARE_PROVIDER,
)
from investment_agent.providers.akshare_eastmoney import (
    EarningsForecast,
    ForecastPeriod,
    SchedulePeriod,
    SegmentStatement,
)
from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.errors import ProviderUnavailableError
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    is_trading_day_or_weekday,
)
from investment_agent.snapshots.collect import (
    ArtifactWriter,
    CollectionReport,
    Completeness,
    error_reason,
    fence_writes,
)
from investment_agent.snapshots.disclosure import statutory_deadline
from investment_agent.snapshots.eastmoney import FailureBreaker
from investment_agent.snapshots.jobs import EarningsEventPayload, period_kind
from investment_agent.snapshots.scope import UserPools, scope_members, user_pools
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.earnings_events import DisclosureScheduleRecord
from investment_agent.storage.repositories.earnings_events import (
    EarningsEventRepository,
    SegmentRow,
    segment_row,
    stored_segment_row,
)
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository

log = structlog.get_logger(__name__)

UPCOMING_TRADING_DAYS = 3
"""Appointments within this many trading days ahead are ``upcoming_disclosures``."""
MAX_SEGMENT_PERIODS = 8
"""Report periods of one listing kept from a segments answer (newest first)."""
_MAX_LISTED = 50


class EarningsEventClient(Protocol):
    calls: int

    async def fetch_earnings_forecasts(self, report_period: date) -> ForecastPeriod: ...

    async def fetch_disclosure_schedule(self, report_period: date) -> SchedulePeriod: ...

    async def fetch_business_segments(self, ticker: str) -> SegmentStatement | None: ...


# --------------------------------------------------------------------------- #
# Scope and trading-day helpers
# --------------------------------------------------------------------------- #


async def earnings_scope(
    payload: EarningsEventPayload,
) -> tuple[dict[str, int], UserPools]:
    """(ticker -> listing id of the part's scope, the user's pools).

    ``forecast`` / ``schedule``: the payload scope (target pool for
    ``universe``) ∪ the user's pools; ``segments``: the user's pools only.
    """

    async with session_scope() as session:
        pools = await user_pools(session)
        if payload.part == "segments":
            return pools.members, pools
        base = await scope_members(session, payload, payload.as_of)
    return dict(sorted({**base, **pools.members}.items())), pools


def is_trading_day(calendar: TradingCalendar | None, day: date) -> bool:
    """Kept for this module's callers; the rule has one home in
    :func:`~investment_agent.scheduler.trading_calendar.is_trading_day_or_weekday`."""

    return is_trading_day_or_weekday(calendar, day)


def upcoming_trading_days(
    as_of: date, calendar: TradingCalendar | None, count: int = UPCOMING_TRADING_DAYS
) -> list[date]:
    """The next ``count`` trading days strictly after ``as_of``."""

    days: list[date] = []
    cursor = as_of
    while len(days) < count:
        cursor += timedelta(days=1)
        if is_trading_day(calendar, cursor):
            days.append(cursor)
    return days


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #


async def collect_earnings_events(
    payload: EarningsEventPayload,
    *,
    client: EarningsEventClient,
    now: Callable[[], datetime],
    lease: RunLease | None,
    artifacts: ArtifactWriter,
    circuit: CircuitBreaker | None = None,
    calendar: TradingCalendar | None = None,
    run_id: int | None = None,
) -> CollectionReport:
    if payload.provider != AKSHARE_PROVIDER:
        raise ValueError(f"earnings event collector got provider {payload.provider!r}")
    del now  # captured_at comes from the provider client (its clock)
    listing_by_ticker, pools = await earnings_scope(payload)
    if payload.part == "forecast":
        return await _collect_forecasts(
            payload, listing_by_ticker, pools, client=client, artifacts=artifacts, lease=lease
        )
    if payload.part == "schedule":
        return await _collect_schedule(
            payload,
            listing_by_ticker,
            pools,
            client=client,
            artifacts=artifacts,
            lease=lease,
            calendar=calendar,
            run_id=run_id,
        )
    return await _collect_segments(
        payload, listing_by_ticker, client=client, artifacts=artifacts, lease=lease, circuit=circuit
    )


def _base_metadata(
    payload: EarningsEventPayload, client: EarningsEventClient, pools: UserPools | None = None
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "provider": AKSHARE_PROVIDER,
        "part": payload.part,
        "scope": payload.scope,
        "universe_code": payload.universe_code,
        "report_period": payload.report_period.isoformat(),
        "period_kind": period_kind(payload.report_period),
        "as_of": payload.as_of.isoformat(),
        "akshare_calls": client.calls,
    }
    if pools is not None:
        metadata["pools"] = {
            "watchlist": len(pools.watchlist),
            "candidates": len(pools.candidates),
            "holdings": len(pools.holdings),
        }
    return metadata


def _whole_market_failure(
    report: CollectionReport,
    listing_by_ticker: Mapping[str, int],
    exc: Exception,
    *,
    part: str,
) -> None:
    reason = error_reason(exc)
    report.record_failure(exc)
    log.warning("snapshot.earnings_event.failed", part=part, error=reason)
    for ticker in listing_by_ticker:
        report.completeness.mark_missing(ticker, "failed", reason)


def forecast_item(
    forecast: EarningsForecast, *, pools: Sequence[str], raw_ref: str | None
) -> dict[str, object]:
    """One ``new_forecasts`` entry of the run metadata (read by the notification)."""

    return {
        "ticker": forecast.ticker,
        "name": forecast.name,
        "report_period": forecast.report_period.isoformat(),
        "notice_date": forecast.notice_date.isoformat(),
        "forecast_type": forecast.forecast_type,
        "indicator": forecast.indicator,
        "profit_low": forecast.profit_low,
        "profit_high": forecast.profit_high,
        "yoy_low": forecast.yoy_low,
        "yoy_high": forecast.yoy_high,
        "pools": list(pools),
        "raw_ref": raw_ref,
    }


async def _collect_forecasts(
    payload: EarningsEventPayload,
    listing_by_ticker: dict[str, int],
    pools: UserPools,
    *,
    client: EarningsEventClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> CollectionReport:
    period = payload.report_period
    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=AKSHARE_PROVIDER)
    try:
        answer = await client.fetch_earnings_forecasts(period)
    except LeaseLostError:
        raise
    except ProviderUnavailableError as exc:
        report.stop_for_provider(exc, sorted(listing_by_ticker), failures=exc.fail_count)
        report.metadata = _base_metadata(payload, client, pools)
        return report
    except Exception as exc:
        _whole_market_failure(report, listing_by_ticker, exc, part="forecast")
        report.metadata = _base_metadata(payload, client, pools)
        return report

    in_scope = {
        ticker: items for ticker, items in answer.forecasts.items() if ticker in listing_by_ticker
    }
    ref = artifacts.write(
        f"earnings-forecast-{period.isoformat()}",
        {
            "provider": AKSHARE_PROVIDER,
            "report_period": period.isoformat(),
            "captured_at": answer.captured_at.isoformat(),
            "rows": answer.raw_rows_for(sorted(in_scope)),
        },
    )
    listing_ids = [listing_by_ticker[ticker] for ticker in in_scope]
    async with session_scope() as session:
        existing = await EarningsEventRepository(session).forecast_keys(listing_ids, period)

    inserts: list[EarningsForecast] = []
    skipped = 0
    for ticker, items in sorted(in_scope.items()):
        for forecast in items:
            if (listing_by_ticker[ticker], forecast.notice_date) in existing:
                skipped += 1
            else:
                inserts.append(forecast)
    if inserts:
        async with session_scope() as session:
            await fence_writes(session, lease)
            repository = EarningsEventRepository(session)
            for forecast in inserts:
                await repository.insert_forecast(
                    listing_id=listing_by_ticker[forecast.ticker],
                    report_period=period,
                    notice_date=forecast.notice_date,
                    forecast_type=forecast.forecast_type,
                    indicator=forecast.indicator,
                    profit_low=forecast.profit_low,
                    profit_high=forecast.profit_high,
                    yoy_low=forecast.yoy_low,
                    yoy_high=forecast.yoy_high,
                    content=forecast.content,
                    provider=AKSHARE_PROVIDER,
                    captured_at=answer.captured_at,
                    raw_ref=ref,
                )
    for ticker in listing_by_ticker:
        completeness.mark_collected(ticker)
    new_in_pools = [
        forecast_item(forecast, pools=pools.pools_of(forecast.ticker), raw_ref=ref)
        for forecast in inserts
        if pools.pools_of(forecast.ticker)
    ]
    types: dict[str, int] = {}
    for forecast in inserts:
        types[forecast.forecast_type] = types.get(forecast.forecast_type, 0) + 1
    report.metadata = {
        **_base_metadata(payload, client, pools),
        "raw_ref": ref,
        "captured_at": answer.captured_at.isoformat(),
        "market_listings": len(answer.forecasts),
        "in_scope": len(in_scope),
        "stored": len(inserts),
        "existing": skipped,
        "stored_by_type": dict(sorted(types.items())),
        "stored_tickers": sorted({forecast.ticker for forecast in inserts})[:_MAX_LISTED],
        "new_forecasts": new_in_pools[:_MAX_LISTED],
        "new_forecast_count": len(new_in_pools),
        "skipped_indicators": answer.skipped_indicators,
        "deferred_count": len(answer.deferred),
        "deferred": [
            {"ticker": ticker, "notice_date": day.isoformat()}
            for ticker, day in answer.deferred[:_MAX_LISTED]
        ],
        "units": (
            "profit_low / profit_high in CNY (yuan); yoy_low / yoy_high fractions "
            "(ADD_AMP_LOWER / ADD_AMP_UPPER percent / 100); indicator names the net-profit line"
        ),
    }
    return report


def _schedule_changed(
    previous: DisclosureScheduleRecord | None, *, scheduled_date: date, actual_date: date | None
) -> bool:
    return (
        previous is None
        or previous.scheduled_date != scheduled_date
        or previous.actual_date != actual_date
    )


async def _collect_schedule(
    payload: EarningsEventPayload,
    listing_by_ticker: dict[str, int],
    pools: UserPools,
    *,
    client: EarningsEventClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    calendar: TradingCalendar | None,
    run_id: int | None,
) -> CollectionReport:
    period = payload.report_period
    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=AKSHARE_PROVIDER)
    try:
        answer = await client.fetch_disclosure_schedule(period)
    except LeaseLostError:
        raise
    except ProviderUnavailableError as exc:
        report.stop_for_provider(exc, sorted(listing_by_ticker), failures=exc.fail_count)
        report.metadata = _base_metadata(payload, client, pools)
        return report
    except Exception as exc:
        _whole_market_failure(report, listing_by_ticker, exc, part="schedule")
        report.metadata = _base_metadata(payload, client, pools)
        return report

    in_scope = {
        ticker: item for ticker, item in answer.appointments.items() if ticker in listing_by_ticker
    }
    ref = artifacts.write(
        f"earnings-schedule-{period.isoformat()}",
        {
            "provider": AKSHARE_PROVIDER,
            "report_period": period.isoformat(),
            "captured_at": answer.captured_at.isoformat(),
            "rows": answer.raw_rows_for(sorted(in_scope)),
        },
    )
    listing_ids = [listing_by_ticker[ticker] for ticker in in_scope]
    async with session_scope() as session:
        previous = await EarningsEventRepository(session).latest_schedules(listing_ids, period)

    appended: list[str] = []
    unchanged: list[str] = []
    published: list[str] = []
    for ticker, item in sorted(in_scope.items()):
        stored = previous.get(listing_by_ticker[ticker])
        if _schedule_changed(
            stored, scheduled_date=item.scheduled_date, actual_date=item.actual_date
        ):
            appended.append(ticker)
        else:
            unchanged.append(ticker)
        if item.actual_date is not None:
            published.append(ticker)
    if appended:
        async with session_scope() as session:
            await fence_writes(session, lease)
            repository = EarningsEventRepository(session)
            for ticker in appended:
                item = in_scope[ticker]
                await repository.insert_schedule(
                    listing_id=listing_by_ticker[ticker],
                    report_period=period,
                    first_date=item.first_date,
                    scheduled_date=item.scheduled_date,
                    actual_date=item.actual_date,
                    provider=AKSHARE_PROVIDER,
                    captured_at=answer.captured_at,
                    raw_ref=ref,
                )
    for ticker in listing_by_ticker:
        completeness.mark_collected(ticker)

    # Appointments after as_of and no later than the third trading day ahead
    # (a weekend appointment counts: companies do publish on Saturdays).
    window = upcoming_trading_days(payload.as_of, calendar)
    upcoming: list[dict[str, object]] = []
    for ticker, item in sorted(in_scope.items()):
        member_of = pools.pools_of(ticker)
        if (
            not member_of
            or item.actual_date is not None
            or not payload.as_of < item.scheduled_date <= window[-1]
        ):
            continue
        upcoming.append(
            {
                "ticker": ticker,
                "name": item.name,
                "report_period": period.isoformat(),
                "scheduled_date": item.scheduled_date.isoformat(),
                "first_date": item.first_date.isoformat(),
                "trading_days_ahead": sum(1 for day in window if day <= item.scheduled_date),
                "pools": member_of,
                "raw_ref": ref,
            }
        )
    reconcile = await reconcile_overdue(
        period=period,
        listing_by_ticker=listing_by_ticker,
        actual_dates={
            ticker: item.actual_date
            for ticker, item in in_scope.items()
            if item.actual_date is not None
        },
        lease=lease,
    )
    report.metadata = {
        **_base_metadata(payload, client, pools),
        "raw_ref": ref,
        "captured_at": answer.captured_at.isoformat(),
        "market_listings": len(answer.appointments),
        "in_scope": len(in_scope),
        "appended": len(appended),
        "appended_tickers": appended[:_MAX_LISTED],
        "unchanged": len(unchanged),
        "published": len(published),
        "not_scheduled": sorted(set(listing_by_ticker) - set(in_scope))[:_MAX_LISTED],
        "upcoming_window": [day.isoformat() for day in window],
        "upcoming_disclosures": upcoming[:_MAX_LISTED],
        "upcoming_count": len(upcoming),
        "report_overdue_reconcile": reconcile,
        "deferred_actual_count": len(answer.deferred_actual),
        "deferred_actual": [
            {"ticker": ticker, "actual_date": day.isoformat()}
            for ticker, day in answer.deferred_actual[:_MAX_LISTED]
        ],
        "run_id": run_id,
    }
    return report


async def reconcile_overdue(
    *,
    period: date,
    listing_by_ticker: Mapping[str, int],
    actual_dates: Mapping[str, date],
    lease: RunLease | None = None,
) -> dict[str, object]:
    """Compare open ``report_overdue`` rows of ``period`` with Eastmoney's actual dates.

    Secondary evidence only (C2): nothing is resolved here - the baostock
    ``pubDate`` seen by the financial job remains the primary signal. The
    differences are returned for the run metadata: open overdue rows whose
    Eastmoney ``actual_date`` is known (``published_per_eastmoney``) and
    listings Eastmoney says are published after the deadline but which were
    never recorded as overdue (``late_not_recorded``).
    """

    deadline = statutory_deadline(period)
    result: dict[str, object] = {
        "deadline": deadline.isoformat(),
        "checked": bool(listing_by_ticker),
        "published_per_eastmoney": [],
        "late_not_recorded": [],
    }
    if not listing_by_ticker:
        return result
    ticker_by_listing = {listing_id: ticker for ticker, listing_id in listing_by_ticker.items()}
    async with session_scope() as session:
        await fence_writes(session, lease)
        rows = await ReportOverdueRepository(session).for_period(period, list(ticker_by_listing))
    open_by_ticker = {
        ticker_by_listing[row.listing_id]: row for row in rows if row.resolved_on is None
    }
    recorded = {ticker_by_listing[row.listing_id] for row in rows}
    published = [
        {"ticker": ticker, "actual_date": actual.isoformat()}
        for ticker, actual in sorted(actual_dates.items())
        if ticker in open_by_ticker
    ]
    late = [
        {"ticker": ticker, "actual_date": actual.isoformat()}
        for ticker, actual in sorted(actual_dates.items())
        if actual > deadline and ticker not in recorded
    ]
    if published:
        log.info(
            "snapshot.report_overdue.eastmoney_actual_date",
            report_period=period.isoformat(),
            tickers=[item["ticker"] for item in published],
        )
    result.update(
        published_per_eastmoney=published[:_MAX_LISTED], late_not_recorded=late[:_MAX_LISTED]
    )
    return result


def _segment_rows(statement: SegmentStatement, period: date) -> list[SegmentRow]:
    return [
        segment_row(
            kind=item.kind,
            name=item.name,
            revenue=item.revenue,
            profit=item.profit,
            margin=item.margin,
            share=item.share,
        )
        for item in statement.periods[period]
    ]


async def _collect_segments(
    payload: EarningsEventPayload,
    listing_by_ticker: dict[str, int],
    *,
    client: EarningsEventClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    circuit: CircuitBreaker | None,
) -> CollectionReport:
    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=AKSHARE_PROVIDER)
    breaker = FailureBreaker(circuit=circuit)
    todo = sorted(listing_by_ticker)
    stored_rows = 0
    stored_periods = 0
    unchanged_periods = 0
    latest_periods: dict[str, str] = {}
    for position, ticker in enumerate(todo):
        listing_id = listing_by_ticker[ticker]
        try:
            statement = await client.fetch_business_segments(ticker)
        except LeaseLostError:
            raise
        except ProviderUnavailableError as exc:
            report.stop_for_provider(exc, todo[position:], failures=exc.fail_count)
            break
        except Exception as exc:
            reason = error_reason(exc)
            report.record_failure(exc)
            completeness.mark_missing(ticker, "failed", reason)
            log.warning("snapshot.earnings_segments.failed", ticker=ticker, error=reason)
            if breaker.failure(exc):
                report.stop_for_provider(exc, todo[position + 1 :], failures=breaker.consecutive)
                break
            continue
        breaker.success()
        if statement is None or not statement.periods:
            completeness.mark_missing(ticker, "no_data", "Eastmoney has no business segments")
            continue
        periods = list(statement.periods)[:MAX_SEGMENT_PERIODS]
        capture_day = statement.captured_at.astimezone(SHANGHAI_TZ).date()
        ref: str | None = None
        for period in periods:
            rows = _segment_rows(statement, period)
            if not rows:
                continue
            async with session_scope() as session:
                repository = EarningsEventRepository(session)
                previous = [
                    stored_segment_row(record)
                    for record in await repository.latest_segment_rows(listing_id, period)
                ]
                if previous == rows:
                    unchanged_periods += 1
                    continue
                if ref is None:
                    ref = artifacts.write(
                        f"earnings-segments-{ticker}",
                        {
                            "provider": AKSHARE_PROVIDER,
                            "ticker": ticker,
                            "captured_at": statement.captured_at.isoformat(),
                            "capture_date": capture_day.isoformat(),
                            "rows": statement.raw_rows_for(periods),
                        },
                    )
                await fence_writes(session, lease)
                stored_rows += await repository.insert_segments(
                    listing_id=listing_id,
                    report_period=period,
                    rows=rows,
                    provider=AKSHARE_PROVIDER,
                    captured_at=statement.captured_at,
                    raw_ref=ref,
                )
                stored_periods += 1
        completeness.mark_collected(ticker)
        latest_periods[ticker] = periods[0].isoformat()
    report.metadata = {
        **_base_metadata(payload, client),
        "members": len(listing_by_ticker),
        "stored_rows": stored_rows,
        "stored_periods": stored_periods,
        "unchanged_periods": unchanged_periods,
        "latest_periods": dict(sorted(latest_periods.items())[:_MAX_LISTED]),
        "max_periods_per_listing": MAX_SEGMENT_PERIODS,
        "units": "revenue / profit in CNY; share / margin fractions; kind product|industry|region",
    }
    return report


__all__ = [
    "MAX_SEGMENT_PERIODS",
    "UPCOMING_TRADING_DAYS",
    "EarningsEventClient",
    "collect_earnings_events",
    "earnings_scope",
    "forecast_item",
    "is_trading_day",
    "reconcile_overdue",
    "upcoming_trading_days",
]

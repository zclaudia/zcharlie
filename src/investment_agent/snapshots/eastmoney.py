"""AkShare / Eastmoney ``financial_snapshot`` collector (P1-A11, D6 rev 2).

Runs as the ``financial_snapshot`` job with ``provider="akshare"`` and a
``part``:

``summary``
    One whole-market call per endpoint (``stock_lrb_em`` / ``stock_xjll_em``
    / ``stock_zcfz_em``) for ``report_period``, filtered to the scope:
    ``net_profit_parent``, ``revenue_total``, ``operating_cash_flow``,
    ``debt_ratio`` (+ total assets / liabilities / equity).
``cash_flow``
    One ``stock_cash_flow_sheet_by_report_em`` call per listing (16-24 s);
    every requested period (``report_period`` + ``extra_periods``) of the
    answer is stored: ``operating_cash_flow``, ``capital_expenditure`` and
    ``free_cash_flow = NETCASH_OPERATE - CONSTRUCT_LONG_ASSET``. Listings
    that already have a cash-flow capture of every requested period are
    skipped (the slow call is only spent on gaps).

Scope: ``universe`` = point-in-time members on ``as_of`` **plus** enabled
watchlist A-shares (the summaries cost nothing extra); ``watchlist`` = the
watchlist only. Backfill runs (``backfill=True``) use every member of the
universe between the report period and ``as_of`` (survivorship set).

Point in time (decision D2): ``available_from`` = the baostock ``pubDate``
of the same listing and period when stored (``availability="actual"``),
else the Eastmoney notice date (``approx``; for the summaries this is the
latest notice touching the row, i.e. conservative). Eastmoney serves
restated figures, so rows carry ``restated``: cash-flow rows when
``UPDATE_DATE > NOTICE_DATE``; summary rows when the notice is later than the
baostock pubDate (or, without one, than the statutory deadline).

Versioning: every capture whose content differs from the latest stored
AkShare row of the same period and part is inserted as a new version;
identical re-captures are skipped (``unchanged``), nothing is overwritten.

Cross-check: after a summary capture the overlapping fields are compared
with the baostock row of the period - ``debt_ratio`` (a baostock value
failing the registry sanity rule is listed as suspect, not as a conflict)
and ``operating_cash_flow`` vs baostock ``CFOToNP x netProfit``. Revenue is
not comparable (``revenue_total`` vs ``revenue_main``). A difference beyond
the field's registry tolerance is stored on the row
(``fields.provider_conflicts``, read by the factor view as ``conflict``) and
listed in the run metadata (``provider_conflict``); key-field conflicts and
missing key fields go to :func:`enqueue_verification` (P1-A13 verification queue).

Throttle and failures: the client serializes calls (>= 1 s apart by default).
:data:`MAX_CONSECUTIVE_FAILURES` consecutive failed calls stop the job: the
remaining listings are ``stopped``, the run ends ``partial`` with
``stop_reason="provider_unavailable"`` and one ``action_required``
notification.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.factors.field_sources import (
    FieldSourceRegistry,
    default_field_sources,
    relative_difference,
)
from investment_agent.providers.akshare_eastmoney import (
    PROVIDER as AKSHARE_PROVIDER,
)
from investment_agent.providers.akshare_eastmoney import (
    SUMMARY_ENDPOINTS,
    SUMMARY_KEY_FIELDS,
    CashFlowPeriod,
    CashFlowStatement,
    PeriodSummaries,
    PeriodSummary,
)
from investment_agent.providers.baostock_reference import IndexCode
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
    FinancialSnapshotPayload,
    enqueue_snapshot_job,
    idempotency_key,
    period_kind,
)
from investment_agent.snapshots.scope import scope_members
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease
from investment_agent.storage.repositories.universe import UniverseRepository

log = structlog.get_logger(__name__)

BAOSTOCK_PROVIDER = "baostock"
MAX_CONSECUTIVE_FAILURES = 3
_MAX_LISTED = 50
# Fields compared between the period summaries and baostock.
CROSS_CHECK_FIELDS: tuple[str, ...] = ("debt_ratio", "operating_cash_flow")
# Content keys compared to decide whether a re-capture is a new version.
_SUMMARY_VALUE_KEYS = (
    "net_profit_parent",
    "revenue_total",
    "operating_cash_flow",
    "debt_ratio",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "notice_date",
)
_CASH_FLOW_VALUE_KEYS = (
    "operating_cash_flow",
    "capital_expenditure",
    "free_cash_flow",
    "notice_date",
    "update_date",
)

VerificationTrigger = Literal["conflict", "missing"]
# AkShare cross-check reason -> verification_request trigger (P1-A13)
_VERIFICATION_TRIGGERS: dict[str, str] = {"conflict": "conflict", "missing": "missing"}


class EastmoneyCollectorClient(Protocol):
    calls: int

    async def fetch_period_summaries(self, report_period: date) -> PeriodSummaries: ...

    async def fetch_cash_flow_statement(self, ticker: str) -> CashFlowStatement | None: ...


async def enqueue_verification(
    *,
    listing_id: int,
    ticker: str,
    report_period: date,
    fields: Sequence[str],
    trigger: VerificationTrigger,
    detail: Mapping[str, Any] | None = None,
    run_id: int | None = None,
) -> bool:
    """Ask for an iFinD verification of key ``fields`` (P1-A13).

    Delegates to :func:`investment_agent.verification.enqueue_verification`
    (trigger ``conflict`` -> ``conflict``, ``missing`` -> ``missing``; one open
    request per listing, trigger and period, merged on repeat). Returns whether
    a new request was queued. Never raises: a failed enqueue only logs, the
    collection result stands.
    """

    from investment_agent.verification import enqueue_verification as enqueue

    try:
        return await enqueue(
            listing_id=listing_id,
            ticker=ticker,
            trigger=_VERIFICATION_TRIGGERS[trigger],
            fields=list(fields),
            report_period=report_period,
            detail=detail,
            run_id=run_id,
        )
    except Exception:
        log.exception(
            "snapshot.akshare.verification_enqueue_failed",
            listing_id=listing_id,
            ticker=ticker,
            trigger=trigger,
            report_period=report_period.isoformat(),
        )
        return False


@dataclass
class FailureBreaker:
    """Stops the job after :data:`MAX_CONSECUTIVE_FAILURES` consecutive failed calls."""

    limit: int = MAX_CONSECUTIVE_FAILURES
    consecutive: int = 0
    last_error: BaseException | None = None

    def success(self) -> None:
        self.consecutive = 0

    def failure(self, exc: BaseException) -> bool:
        """Record a failure; ``True`` when the job must stop."""

        self.consecutive += 1
        self.last_error = exc
        return self.consecutive >= self.limit


@dataclass
class _CrossCheck:
    registry: FieldSourceRegistry
    conflicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    suspect: dict[str, float] = field(default_factory=dict)
    compared: dict[str, int] = field(default_factory=dict)
    verification_requests: int = 0

    def to_metadata(self) -> dict[str, object]:
        by_field: dict[str, int] = {}
        for items in self.conflicts.values():
            for name in items:
                by_field[name] = by_field.get(name, 0) + 1
        return {
            "count": len(self.conflicts),
            "by_field": dict(sorted(by_field.items())),
            "compared": dict(sorted(self.compared.items())),
            "items": dict(sorted(self.conflicts.items())[:_MAX_LISTED]),
            "baostock_suspect_debt_ratio": dict(sorted(self.suspect.items())[:_MAX_LISTED]),
            "baostock_suspect_count": len(self.suspect),
            "verification_requests": self.verification_requests,
        }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def baostock_operating_cash_flow(fields: Mapping[str, object]) -> float | None:
    """baostock ``CFOToNP`` x ``netProfit`` (approximate operating cash flow)."""

    ratio = _number(fields.get("cfo_to_np"))
    profit = _number(fields.get("net_profit"))
    if ratio is None or profit is None:
        return None
    return ratio * profit


def cross_check(
    ticker: str,
    akshare: Mapping[str, float | None],
    baostock_row: FinancialSnapshot | None,
    check: _CrossCheck,
) -> dict[str, dict[str, Any]]:
    """Overlapping fields beyond tolerance -> ``{field: detail}`` (also recorded in ``check``)."""

    if baostock_row is None:
        return {}
    stored = baostock_row.fields if isinstance(baostock_row.fields, dict) else {}
    baostock_values = {
        "debt_ratio": _number(stored.get("debt_ratio")),
        "operating_cash_flow": baostock_operating_cash_flow(stored),
    }
    conflicts: dict[str, dict[str, Any]] = {}
    for name in CROSS_CHECK_FIELDS:
        theirs = baostock_values[name]
        ours = akshare.get(name)
        if theirs is None or ours is None:
            continue
        if check.registry.sanity_problem(BAOSTOCK_PROVIDER, name, theirs) is not None:
            check.suspect[ticker] = theirs
            continue
        check.compared[name] = check.compared.get(name, 0) + 1
        tolerance = check.registry.tolerance(name)
        difference = relative_difference(ours, theirs)
        if difference > tolerance:
            conflicts[name] = {
                "akshare": ours,
                "baostock": theirs,
                "baostock_basis": "liabilityToAsset"
                if name == "debt_ratio"
                else "CFOToNP x netProfit (approx)",
                "relative_difference": round(difference, 6),
                "tolerance": tolerance,
                "baostock_ref": f"financial_snapshot:{baostock_row.id}",
                # The baostock operating cash flow is derived from ratios: a
                # difference is worth recording, not an iFinD verification.
                "baostock_approx": name != "debt_ratio",
            }
    if conflicts:
        check.conflicts[ticker] = conflicts
    return conflicts


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


async def akshare_scope(
    session: AsyncSession, payload: FinancialSnapshotPayload
) -> dict[str, int]:
    """Ticker -> listing id (see module doc)."""

    if payload.scope == "watchlist":
        return await scope_members(session, payload, payload.as_of)
    assert payload.universe_code is not None
    if payload.backfill:
        start = min(payload.periods)
        members = await UniverseRepository(session).members_between(
            payload.universe_code, start, payload.as_of
        )
        if not members:
            raise SnapshotPreconditionError(
                f"{payload.universe_code} has no members between {start.isoformat()} and "
                f"{payload.as_of.isoformat()}; run universe_sync / the membership backfill first"
            )
        return {member.ticker: member.listing_id for member in sorted(members, key=_ticker)}
    resolved = await scope_members(session, payload, payload.as_of)
    try:
        watchlist = await scope_members(
            session,
            payload.model_copy(update={"scope": "watchlist", "universe_code": None}),
            payload.as_of,
        )
    except SnapshotPreconditionError:
        watchlist = {}
    return dict(sorted({**watchlist, **resolved}.items()))


def _ticker(member: Any) -> str:
    return str(member.ticker)


# --------------------------------------------------------------------------- #
# Row construction (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Availability:
    available_from: date | None
    availability: str
    basis: str


def availability_for(
    baostock_row: FinancialSnapshot | None, notice_date: date | None
) -> Availability:
    if baostock_row is not None and baostock_row.available_from is not None:
        return Availability(baostock_row.available_from, "actual", "baostock_pub_date")
    if notice_date is not None:
        return Availability(notice_date, "approx", "eastmoney_notice_date")
    return Availability(None, "unknown", "none")


def summary_restated(
    summary: PeriodSummary, baostock_row: FinancialSnapshot | None
) -> tuple[bool, str]:
    """Whether the summary row reflects a later restatement (and on what basis)."""

    notice = summary.notice_date
    if notice is None:
        return False, "no_notice_date"
    if baostock_row is not None and baostock_row.available_from is not None:
        return notice > baostock_row.available_from, "notice_after_baostock_pub_date"
    return notice > statutory_deadline(summary.report_period), "notice_after_statutory_deadline"


def summary_fields(
    summary: PeriodSummary,
    *,
    baostock_row: FinancialSnapshot | None,
    availability: Availability,
    conflicts: Mapping[str, Any],
) -> dict[str, object]:
    restated, basis = summary_restated(summary, baostock_row)
    fields: dict[str, object] = {
        "part": "summary",
        **summary.fields,
        "missing_fields": summary.missing_fields,
        "source_values": dict(summary.source_values),
        "notice_date": summary.notice_date.isoformat() if summary.notice_date else None,
        "restated": restated,
        "restated_basis": basis,
        "availability_basis": availability.basis,
        "endpoints": list(summary.endpoints),
    }
    if conflicts:
        fields["provider_conflicts"] = dict(conflicts)
    return fields


def cash_flow_fields(period: CashFlowPeriod, availability: Availability) -> dict[str, object]:
    values = period.fields()
    return {
        "part": "cash_flow",
        **values,
        "missing_fields": sorted(name for name, value in values.items() if value is None),
        "source_values": {
            "NETCASH_OPERATE": period.operating_cash_flow,
            "CONSTRUCT_LONG_ASSET": period.capital_expenditure,
            "CURRENCY": period.currency,
        },
        "notice_date": period.notice_date.isoformat() if period.notice_date else None,
        "update_date": period.update_date.isoformat() if period.update_date else None,
        "restated": period.restated,
        "restated_basis": "update_date_after_notice_date",
        "availability_basis": availability.basis,
    }


def _content(
    fields: Mapping[str, object], availability: Availability, keys: Sequence[str]
) -> tuple[object, ...]:
    return (
        *(fields.get(key) for key in keys),
        availability.available_from,
        availability.availability,
    )


def unchanged(
    previous: FinancialSnapshot | None,
    fields: Mapping[str, object],
    availability: Availability,
    keys: Sequence[str],
) -> bool:
    if previous is None:
        return False
    previous_availability = Availability(previous.available_from, previous.availability, "")
    return _content(previous.fields, previous_availability, keys) == _content(
        fields, availability, keys
    )


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #


async def collect_akshare_financial(
    payload: FinancialSnapshotPayload,
    *,
    client: EastmoneyCollectorClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    now: Callable[[], datetime],
    run_id: int | None = None,
    registry: FieldSourceRegistry | None = None,
) -> CollectionReport:
    if payload.provider != AKSHARE_PROVIDER:
        raise ValueError(f"AkShare financial collector got provider {payload.provider!r}")
    del now  # captured_at comes from the provider client (its clock)
    async with session_scope() as session:
        listing_by_ticker = await akshare_scope(session, payload)
    if payload.part == "cash_flow":
        return await _collect_cash_flow(
            payload, listing_by_ticker, client=client, artifacts=artifacts, lease=lease,
            run_id=run_id,
        )
    return await _collect_summary(
        payload,
        listing_by_ticker,
        client=client,
        artifacts=artifacts,
        lease=lease,
        run_id=run_id,
        registry=registry or default_field_sources(),
    )


def _base_metadata(payload: FinancialSnapshotPayload, client: EastmoneyCollectorClient) -> dict[str, object]:
    return {
        "provider": AKSHARE_PROVIDER,
        "part": payload.part,
        "scope": payload.scope,
        "universe_code": payload.universe_code,
        "report_period": payload.report_period.isoformat(),
        "period_kind": period_kind(payload.report_period),
        "as_of": payload.as_of.isoformat(),
        "backfill": payload.backfill,
        "akshare_calls": client.calls,
    }


async def _collect_summary(
    payload: FinancialSnapshotPayload,
    listing_by_ticker: dict[str, int],
    *,
    client: EastmoneyCollectorClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    run_id: int | None,
    registry: FieldSourceRegistry,
) -> CollectionReport:
    period = payload.report_period
    kind = period_kind(period)
    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=AKSHARE_PROVIDER)
    check = _CrossCheck(registry)
    try:
        summaries = await client.fetch_period_summaries(period)
    except LeaseLostError:
        raise
    except Exception as exc:
        reason = error_reason(exc)
        report.record_failure(exc)
        log.warning("snapshot.akshare_summary.failed", report_period=period.isoformat(), error=reason)
        for ticker in listing_by_ticker:
            completeness.mark_missing(ticker, "failed", reason)
        report.metadata = {**_base_metadata(payload, client), "endpoints": list(SUMMARY_ENDPOINTS)}
        return report

    in_scope = {
        ticker: summary
        for ticker, summary in summaries.summaries.items()
        if ticker in listing_by_ticker
    }
    ref = artifacts.write(
        f"akshare-summary-{period.isoformat()}",
        {
            "provider": AKSHARE_PROVIDER,
            "report_period": period.isoformat(),
            "captured_at": summaries.captured_at.isoformat(),
            "empty_endpoints": list(summaries.empty_endpoints),
            "rows": summaries.raw_rows_for(sorted(in_scope)),
        },
    )
    listing_ids = [listing_by_ticker[ticker] for ticker in in_scope]
    async with session_scope() as session:
        repository = FinancialSnapshotRepository(session)
        baostock_rows = await repository.latest_versions(
            listing_ids, [period], provider=BAOSTOCK_PROVIDER
        )
        previous_rows = await repository.latest_versions(
            listing_ids, [period], provider=AKSHARE_PROVIDER
        )

    stored: list[str] = []
    same: list[str] = []
    restated: list[str] = []
    approx: list[str] = []
    missing_key: dict[str, list[str]] = {}
    inserts: list[tuple[str, dict[str, object], Availability]] = []
    for ticker, summary in sorted(in_scope.items()):
        listing_id = listing_by_ticker[ticker]
        baostock_row = baostock_rows.get((listing_id, period, ""))
        availability = availability_for(baostock_row, summary.notice_date)
        conflicts = cross_check(ticker, summary.fields, baostock_row, check)
        fields = summary_fields(
            summary, baostock_row=baostock_row, availability=availability, conflicts=conflicts
        )
        gaps = [name for name in SUMMARY_KEY_FIELDS if summary.fields.get(name) is None]
        if gaps:
            missing_key[ticker] = gaps
        previous = previous_rows.get((listing_id, period, "summary"))
        if unchanged(previous, fields, availability, _SUMMARY_VALUE_KEYS):
            same.append(ticker)
            completeness.mark_collected(ticker)
            continue
        inserts.append((ticker, fields, availability))

    if inserts:
        async with session_scope() as session:
            await fence_writes(session, lease)
            repository = FinancialSnapshotRepository(session)
            for ticker, fields, availability in inserts:
                await repository.insert(
                    listing_id=listing_by_ticker[ticker],
                    report_period=period,
                    period_kind=kind,
                    fields=fields,
                    provider=AKSHARE_PROVIDER,
                    captured_at=summaries.captured_at,
                    availability=availability.availability,
                    available_from=availability.available_from,
                    raw_ref=ref,
                    is_backfill=payload.backfill,
                )
    for ticker, fields, availability in inserts:
        stored.append(ticker)
        completeness.mark_collected(ticker)
        if fields.get("restated"):
            restated.append(ticker)
        if availability.availability != "actual":
            approx.append(ticker)

    # iFinD verification is for current decisions: history backfills only
    # record conflicts and gaps (provider_conflicts / run metadata), and a
    # difference against an approximate baostock value never asks iFinD.
    verify = not payload.backfill
    for ticker, conflicts in check.conflicts.items() if verify else ():
        key_fields = [
            name
            for name, detail in conflicts.items()
            if (source := registry.get(name)) and source.key and not detail.get("baostock_approx")
        ]
        if key_fields and await enqueue_verification(
            listing_id=listing_by_ticker[ticker],
            ticker=ticker,
            report_period=period,
            fields=key_fields,
            trigger="conflict",
            detail=conflicts,
            run_id=run_id,
        ):
            check.verification_requests += 1
    for ticker, gaps in missing_key.items() if verify else ():
        if await enqueue_verification(
            listing_id=listing_by_ticker[ticker],
            ticker=ticker,
            report_period=period,
            fields=gaps,
            trigger="missing",
            run_id=run_id,
        ):
            check.verification_requests += 1

    not_disclosed = (
        f"{period.isoformat()} not yet in the Eastmoney summaries"
        if not summaries.empty_endpoints
        else f"{period.isoformat()} not yet disclosed (empty: {', '.join(summaries.empty_endpoints)})"
    )
    for ticker in listing_by_ticker:
        if ticker not in in_scope:
            completeness.mark_missing(ticker, "pending", not_disclosed)

    report.metadata = {
        **_base_metadata(payload, client),
        "endpoints": list(SUMMARY_ENDPOINTS),
        "empty_endpoints": list(summaries.empty_endpoints),
        "market_listings": len(summaries.summaries),
        "in_scope": len(in_scope),
        "stored": len(stored),
        "unchanged": len(same),
        "restated": len(restated),
        "restated_tickers": sorted(restated)[:_MAX_LISTED],
        "approx_availability": len(approx),
        "approx_tickers": sorted(approx)[:_MAX_LISTED],
        "missing_key_fields": dict(sorted(missing_key.items())[:_MAX_LISTED]),
        "provider_conflict": check.to_metadata(),
        "availability": (
            "actual: available_from = baostock pubDate when stored; "
            "approx: Eastmoney 公告日期 (latest notice touching the row)"
        ),
    }
    if check.conflicts:
        log.warning(
            "snapshot.akshare.provider_conflict",
            report_period=period.isoformat(),
            count=len(check.conflicts),
        )
    return report


async def _collect_cash_flow(
    payload: FinancialSnapshotPayload,
    listing_by_ticker: dict[str, int],
    *,
    client: EastmoneyCollectorClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    run_id: int | None,
) -> CollectionReport:
    del run_id
    periods = payload.periods
    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=AKSHARE_PROVIDER)
    listing_ids = list(listing_by_ticker.values())
    async with session_scope() as session:
        repository = FinancialSnapshotRepository(session)
        baostock_rows = await repository.latest_versions(
            listing_ids, list(periods), provider=BAOSTOCK_PROVIDER
        )
        previous_rows = await repository.latest_versions(
            listing_ids, list(periods), provider=AKSHARE_PROVIDER
        )

    todo: list[str] = []
    for ticker, listing_id in listing_by_ticker.items():
        if all((listing_id, period, "cash_flow") in previous_rows for period in periods):
            completeness.mark_collected(ticker)
        else:
            todo.append(ticker)

    breaker = FailureBreaker()
    stored = unchanged_count = restated = 0
    partial_periods: dict[str, list[str]] = {}
    for position, ticker in enumerate(todo):
        listing_id = listing_by_ticker[ticker]
        try:
            statement = await client.fetch_cash_flow_statement(ticker)
        except LeaseLostError:
            raise
        except Exception as exc:
            reason = error_reason(exc)
            report.record_failure(exc)
            completeness.mark_missing(ticker, "failed", reason)
            log.warning("snapshot.akshare_cash_flow.failed", ticker=ticker, error=reason)
            if breaker.failure(exc):
                report.stop_for_provider(exc, todo[position + 1 :], failures=breaker.consecutive)
                break
            continue
        breaker.success()
        if statement is None:
            completeness.mark_missing(ticker, "no_data", "Eastmoney has no cash-flow statement")
            continue
        found = [period for period in periods if period in statement.periods]
        if not found:
            completeness.mark_missing(
                ticker, "pending", f"{payload.report_period.isoformat()} not yet disclosed"
            )
            continue
        ref = artifacts.write(
            f"akshare-cash-flow-{ticker}",
            {
                "provider": AKSHARE_PROVIDER,
                "ticker": ticker,
                "captured_at": statement.captured_at.isoformat(),
                "rows": statement.raw_rows_for(found),
            },
        )
        rows: list[tuple[date, dict[str, object], Availability]] = []
        for period in found:
            item = statement.periods[period]
            availability = availability_for(
                baostock_rows.get((listing_id, period, "")), item.notice_date
            )
            fields = cash_flow_fields(item, availability)
            if unchanged(
                previous_rows.get((listing_id, period, "cash_flow")),
                fields,
                availability,
                _CASH_FLOW_VALUE_KEYS,
            ):
                unchanged_count += 1
                continue
            rows.append((period, fields, availability))
        if rows:
            async with session_scope() as session:
                await fence_writes(session, lease)
                repository = FinancialSnapshotRepository(session)
                for period, fields, availability in rows:
                    await repository.insert(
                        listing_id=listing_id,
                        report_period=period,
                        period_kind=period_kind(period),
                        fields=fields,
                        provider=AKSHARE_PROVIDER,
                        captured_at=statement.captured_at,
                        availability=availability.availability,
                        available_from=availability.available_from,
                        raw_ref=ref,
                        is_backfill=payload.backfill,
                    )
            stored += len(rows)
            restated += sum(1 for _, fields, _ in rows if fields.get("restated"))
        missing_periods = [period.isoformat() for period in periods if period not in found]
        if missing_periods:
            partial_periods[ticker] = missing_periods
        completeness.mark_collected(ticker)

    report.metadata = {
        **_base_metadata(payload, client),
        "endpoints": ["stock_cash_flow_sheet_by_report_em"],
        "periods": [period.isoformat() for period in periods],
        "requested": len(todo),
        "rows_stored": stored,
        "rows_unchanged": unchanged_count,
        "rows_restated": restated,
        "periods_not_disclosed": dict(sorted(partial_periods.items())[:_MAX_LISTED]),
        "aborted": report.stop_error,
        "availability": (
            "actual: available_from = baostock pubDate when stored; "
            "approx: Eastmoney NOTICE_DATE (original disclosure)"
        ),
    }
    return report


# --------------------------------------------------------------------------- #
# History backfill (``ia snapshot backfill --provider akshare``)
# --------------------------------------------------------------------------- #

SECONDS_SUMMARY_CALL = 8.0
SECONDS_CASH_FLOW_CALL = 20.0


def akshare_backfill_payloads(
    universe_code: IndexCode,
    as_of: date,
    *,
    annual_years: int,
    quarters: int,
    with_cash_flow: bool = False,
) -> list[FinancialSnapshotPayload]:
    """Period summaries for every D5 period (3 calls each), plus - only with
    ``with_cash_flow`` - one cash-flow job storing all those periods (slow:
    one ~20 s call per listing of the survivorship set)."""

    from investment_agent.snapshots.backfill import report_periods

    periods = report_periods(as_of, annual_years=annual_years, quarters=quarters)
    payloads = [
        FinancialSnapshotPayload(
            provider="akshare",
            part="summary",
            scope="universe",
            universe_code=universe_code,
            report_period=period,
            as_of=as_of,
            backfill=True,
        )
        for period in periods
    ]
    if with_cash_flow and periods:
        payloads.append(
            FinancialSnapshotPayload(
                provider="akshare",
                part="cash_flow",
                scope="universe",
                universe_code=universe_code,
                report_period=periods[0],
                extra_periods=tuple(periods[1:]),
                as_of=as_of,
                backfill=True,
            )
        )
    return payloads


@dataclass(frozen=True)
class AkshareBackfillAction:
    run_id: int
    key: str
    part: str
    report_period: date
    status: str
    created: bool


async def plan_akshare_backfill(
    session: AsyncSession,
    payloads: Sequence[FinancialSnapshotPayload],
    *,
    triggered_by: str,
) -> list[AkshareBackfillAction]:
    """Queue each payload once (idempotency key). Re-running resumes: queued,
    running and finished (success / partial) runs are kept; a failed run is
    re-queued under ``<key>:r<n>``."""

    actions: list[AkshareBackfillAction] = []
    for payload in payloads:
        key = idempotency_key(payload)
        runs = (
            (
                await session.execute(
                    select(PipelineRun)
                    .where(
                        or_(
                            PipelineRun.idempotency_key == key,
                            PipelineRun.idempotency_key.like(f"{key}:r%"),
                        )
                    )
                    .order_by(PipelineRun.id)
                )
            )
            .scalars()
            .all()
        )
        latest = runs[-1] if runs else None
        part = str(payload.part)
        if latest is not None and latest.status != "failed":
            actions.append(
                AkshareBackfillAction(
                    latest.id,
                    latest.idempotency_key or key,
                    part,
                    payload.report_period,
                    latest.status,
                    False,
                )
            )
            continue
        retry_key = key if latest is None else f"{key}:r{len(runs) + 1}"
        run, created = await enqueue_snapshot_job(
            session, payload, triggered_by=triggered_by, key=retry_key
        )
        actions.append(
            AkshareBackfillAction(
                run.id, retry_key, part, payload.report_period, run.status, created
            )
        )
    return actions


def estimate_akshare_backfill(
    payloads: Sequence[FinancialSnapshotPayload], *, listings: int
) -> tuple[int, float]:
    """Upper-bound AkShare calls and seconds (cash flow: one call per listing)."""

    summaries = sum(1 for payload in payloads if payload.part == "summary")
    cash_flow = any(payload.part == "cash_flow" for payload in payloads)
    calls = summaries * len(SUMMARY_ENDPOINTS) + (listings if cash_flow else 0)
    seconds = summaries * len(SUMMARY_ENDPOINTS) * SECONDS_SUMMARY_CALL + (
        listings * SECONDS_CASH_FLOW_CALL if cash_flow else 0.0
    )
    return calls, seconds

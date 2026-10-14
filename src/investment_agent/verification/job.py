"""``ifind_verify`` queue job (P1-A13): work the verification queue within budget.

One run:

1. returns stale ``running`` requests to the queue, then picks up to
   ``max_requests`` due requests (``pending``, or ``deferred`` past
   ``not_before``) by priority, then request time;
2. resolves each request's report period (``None`` -> the latest annual period
   a free source published) and folds requests of the same listing and period
   into one work item: the highest-priority request keeps the united fields,
   the others become ``superseded``;
3. groups work items of the same period into units of up to three tickers
   (one iFinD call serves three tickers) and, per unit, plans the calls with
   the A3 batch collectors' request shapes - the ``/financial/...`` endpoints
   the requested fields need, ``/stock/info`` for ``total_shares``,
   ``/stock/history`` (raw) for ``close`` on the latest baostock trade date;
4. before each unit checks the rolling budget: when the unit's calls do not
   fit, that unit and every later one become ``deferred`` with ``not_before`` =
   the moment enough calls leave the window, and the run ends (the dispatcher
   picks them up then). iFinD quota exhaustion (A8) stops the run the same
   way (``not_before`` = now + the budget window) and raises one
   ``action_required`` notification;
5. stores the answers as iFinD snapshots and compares each field with the
   value the factor view would use from the free sources (registry tolerance):
   the new iFinD ``financial_snapshot`` version carries
   ``fields.verification = {field: "verified" | "conflict"}`` (merged with the
   previous iFinD version of the period), so the view's precedence picks
   verified values; conflicts land in ``request.result`` and run metadata, and
   key-field conflicts raise an ``action_required`` notification.

A unit failing for another reason re-queues its requests (``deferred``, +30
minutes) up to :data:`MAX_ATTEMPTS` attempts, then marks them ``failed``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.config import Settings
from investment_agent.factors.field_resolution import factor_fields
from investment_agent.factors.field_sources import FieldSourceRegistry, default_field_sources
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.providers.ifind_budget import (
    BudgetedProxyClient,
    BudgetExhaustedError,
    BudgetStatus,
    as_utc,
    utc_now,
)
from investment_agent.research.ifind_chain import IfindProxyClient
from investment_agent.research.retry_policy import is_quota_exhausted
from investment_agent.snapshots.collect import (
    MAX_TICKERS_PER_CALL,
    ArtifactWriter,
    ProxyBatchCaller,
    ProxyClient,
    error_reason,
    fence_writes,
    rows_by_ticker,
)
from investment_agent.snapshots.financial import FinancialEndpoint, canonical_financial_fields
from investment_agent.snapshots.jobs import period_kind
from investment_agent.snapshots.market import (
    HISTORY_PATH,
    INFO_PATH,
    TOTAL_SHARES_FIELD,
    Bar,
    HistoryRequest,
    parse_bar,
)
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.verification import VerificationRequest
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)
from investment_agent.storage.repositories.pit import to_utc
from investment_agent.verification.compare import (
    FINANCIAL_FIELD_ENDPOINTS,
    VERIFIABLE_FIELDS,
    FieldCheck,
    check_field,
    default_fields,
    endpoints_for,
    free_period,
    latest_annual_period,
    latest_free_bar,
    latest_free_total_shares,
    latest_ifind_row,
)
from investment_agent.verification.queue import due_requests, merge_fields, release_stale_running

log = structlog.get_logger(__name__)

IFIND_VERIFY = "ifind_verify"
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=30)
DISPATCH_SLOT_MINUTES = 30
_MAX_LISTED = 50

StopReason = Literal["budget", "quota"]


class IfindVerifyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: str
    """Dispatch slot (UTC, e.g. ``2026-10-07T09:30Z``) or a CLI stamp."""
    max_requests: int | None = Field(default=None, ge=1, le=500)


def dispatch_slot(now: datetime, minutes: int = DISPATCH_SLOT_MINUTES) -> str:
    """``now`` floored to its ``minutes`` slot, UTC (idempotency per slot)."""

    utc = as_utc(now)
    floored = utc.replace(minute=utc.minute - utc.minute % minutes, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%dT%H:%MZ")


def verify_idempotency_key(slot: str) -> str:
    return f"{IFIND_VERIFY}:{slot}"


async def enqueue_verify_run(
    session: AsyncSession,
    *,
    slot: str,
    triggered_by: str,
    max_requests: int | None = None,
    key: str | None = None,
) -> tuple[PipelineRun, bool]:
    payload = IfindVerifyPayload(slot=slot, max_requests=max_requests)
    return await PipelineRunRepository(session).enqueue(
        kind=IFIND_VERIFY,
        idempotency_key=key or verify_idempotency_key(slot),
        triggered_by=triggered_by,
        request_json=payload.model_dump(mode="json"),
        max_attempts=2,
    )


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #

ProxyFactory = Callable[[], AbstractAsyncContextManager[ProxyClient]]


@dataclass
class VerifyRuntime:
    settings: Settings
    proxy_factory: ProxyFactory
    limiter: AsyncRateLimiter
    clock: Callable[[], datetime] = utc_now
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    batch_backoff_seconds: float = 2.0
    notifier: FileNotifier | None = None
    registry: FieldSourceRegistry | None = None

    @property
    def budget_cap(self) -> int:
        return self.settings.ifind_budget_per_5h

    @property
    def budget_window(self) -> timedelta:
        return timedelta(hours=self.settings.ifind_budget_window_hours)

    def field_sources(self) -> FieldSourceRegistry:
        return self.registry or default_field_sources()

    def file_notifier(self) -> FileNotifier:
        return self.notifier or FileNotifier(self.settings.data_dir / "notifications")


def default_verify_runtime(settings: Settings) -> VerifyRuntime:
    def proxy_factory() -> AbstractAsyncContextManager[ProxyClient]:
        # No ledger hook: BudgetedProxyClient records every verification call.
        return IfindProxyClient(
            base_url=settings.ifind_proxy_url, api_key=settings.ifind_proxy_api_key
        )

    return VerifyRuntime(
        settings=settings,
        proxy_factory=proxy_factory,
        limiter=AsyncRateLimiter(settings.ifind_collect_rate_per_minute),
    )


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


@dataclass
class WorkItem:
    request_id: int
    listing_id: int
    ticker: str
    trigger: str
    priority: int
    report_period: date
    fields: list[str]
    attempts: int
    raw_fields: list[str] | None = None
    """The requests' own fields united (``None`` = all key fields)."""
    merged_ids: list[int] = field(default_factory=list)
    close_date: date | None = None
    free_close: float | None = None
    free_close_ref: str | None = None

    @property
    def financial_fields(self) -> list[str]:
        return [name for name in self.fields if name in FINANCIAL_FIELD_ENDPOINTS]

    def wants(self, name: str) -> bool:
        return name in self.fields


@dataclass(frozen=True)
class UnitPlan:
    items: tuple[WorkItem, ...]
    endpoints: tuple[FinancialEndpoint, ...]
    info_tickers: tuple[str, ...]
    history: tuple[HistoryRequest, ...]

    @property
    def report_period(self) -> date:
        return self.items[0].report_period

    @property
    def calls(self) -> int:
        return len(self.endpoints) + (1 if self.info_tickers else 0) + len(self.history)

    def endpoint_tickers(self, endpoint: FinancialEndpoint) -> list[str]:
        return [
            item.ticker
            for item in self.items
            if any(FINANCIAL_FIELD_ENDPOINTS[name] == endpoint.key for name in item.financial_fields)
        ]


def plan_units(items: list[WorkItem]) -> list[UnitPlan]:
    """Greedy: the next item in priority order plus up to two more of its period."""

    remaining = list(items)
    units: list[UnitPlan] = []
    while remaining:
        head = remaining.pop(0)
        unit = [head]
        for other in list(remaining):
            if len(unit) == MAX_TICKERS_PER_CALL:
                break
            if other.report_period == head.report_period:
                unit.append(other)
                remaining.remove(other)
        endpoints = endpoints_for({name for item in unit for name in item.financial_fields})
        info = tuple(item.ticker for item in unit if item.wants("total_shares"))
        by_day: dict[date, list[str]] = {}
        for item in unit:
            if item.wants("close") and item.close_date is not None:
                by_day.setdefault(item.close_date, []).append(item.ticker)
        history = tuple(
            HistoryRequest("none", day, day, tuple(tickers)) for day, tickers in sorted(by_day.items())
        )
        units.append(UnitPlan(tuple(unit), tuple(endpoints), info, history))
    return units


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


@dataclass
class UnitAnswers:
    financial: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    shares: dict[str, float] = field(default_factory=dict)
    bars: dict[str, Bar] = field(default_factory=dict)
    ref: str | None = None


def _number(value: object) -> float | None:
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text or text in {"--", "None", "null", "nan"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


async def fetch_unit(
    unit: UnitPlan,
    *,
    caller: ProxyBatchCaller,
    artifacts: ArtifactWriter,
    index: int,
    today: date,
) -> UnitAnswers:
    answers = UnitAnswers()
    bodies: dict[str, Any] = {}
    period = unit.report_period
    for endpoint in unit.endpoints:
        tickers = unit.endpoint_tickers(endpoint)
        payload = endpoint.payload(tickers, period)
        result = await caller.call(endpoint.path, payload)
        bodies[endpoint.key] = {"path": endpoint.path, "request": payload, "body": result.body}
        grouped = rows_by_ticker(result.rows)
        for ticker in tickers:
            if grouped.get(ticker):
                answers.financial.setdefault(ticker, {})[endpoint.key] = grouped[ticker][0]
    if unit.info_tickers:
        payload = {"ticker": ",".join(unit.info_tickers), "request_time": today.isoformat()}
        result = await caller.call(INFO_PATH, payload)
        bodies[INFO_PATH] = {"path": INFO_PATH, "request": payload, "body": result.body}
        grouped = rows_by_ticker(result.rows)
        for ticker in unit.info_tickers:
            rows = grouped.get(ticker, [])
            value = _number(rows[0].get(TOTAL_SHARES_FIELD)) if rows else None
            if value is not None and value > 0:
                answers.shares[ticker] = value
    for request in unit.history:
        payload = request.payload()
        result = await caller.call(HISTORY_PATH, payload)
        bodies[f"{HISTORY_PATH}:{request.start.isoformat()}"] = {
            "path": HISTORY_PATH,
            "request": payload,
            "body": result.body,
        }
        grouped = rows_by_ticker(result.rows)
        for ticker in request.tickers:
            for row in grouped.get(ticker, []):
                bar = parse_bar(row)
                if bar is not None and bar.trade_date == request.start:
                    answers.bars[ticker] = bar
    answers.ref = artifacts.write(f"verify-{period.isoformat()}-u{index:03d}", bodies)
    return answers


def _merge_ifind_fields(
    previous: dict[str, object] | None, fresh: dict[str, object]
) -> dict[str, object]:
    merged: dict[str, object] = dict(previous or {})
    for name, value in fresh.items():
        if name == "source_values" and isinstance(value, dict):
            old = merged.get(name)
            values = dict(old) if isinstance(old, dict) else {}
            values.update({k: v for k, v in value.items() if v is not None or k not in values})
            merged[name] = dict(sorted(values.items()))
        elif value is not None or name not in merged:
            merged[name] = value
    merged["missing_fields"] = sorted(
        name for name, value in merged.items() if value is None and name != "missing_fields"
    )
    return merged


@dataclass
class ItemOutcome:
    checks: list[FieldCheck]
    financial_snapshot_id: int | None = None
    market_snapshot_id: int | None = None
    note: str = ""

    def result(self, item: WorkItem, run_id: int) -> dict[str, Any]:
        by_verdict: dict[str, list[str]] = {"verified": [], "conflict": [], "unavailable": []}
        for check in self.checks:
            by_verdict[check.verdict].append(check.name)
        result: dict[str, Any] = {
            "run_id": run_id,
            "report_period": item.report_period.isoformat(),
            "fields": {check.name: check.describe() for check in self.checks},
            "verified": sorted(by_verdict["verified"]),
            "conflicts": sorted(by_verdict["conflict"]),
            "unavailable": sorted(by_verdict["unavailable"]),
            "filled": sorted(check.name for check in self.checks if check.filled),
            "financial_snapshot_id": self.financial_snapshot_id,
            "market_snapshot_id": self.market_snapshot_id,
        }
        if self.note:
            result["note"] = self.note
        return result


async def settle_item(
    session: AsyncSession,
    item: WorkItem,
    answers: UnitAnswers,
    *,
    registry: FieldSourceRegistry,
    now: datetime,
    today: date,
    run_id: int,
) -> ItemOutcome:
    """Compare and store one item's answers (inside the caller's transaction)."""

    checks: list[FieldCheck] = []
    outcome = ItemOutcome(checks)
    financial_names = item.financial_fields
    if financial_names:
        rows = answers.financial.get(item.ticker, {})
        canonical = canonical_financial_fields(rows) if rows else None
        if canonical is None:
            checks.extend(
                FieldCheck(name, "unavailable", reason="iFinD has no data for the period")
                for name in financial_names
            )
            outcome.note = f"iFinD returned no {item.report_period.isoformat()} data"
        else:
            values = factor_fields("ifind", canonical)
            free = await free_period(session, item.listing_id, item.report_period, registry)
            for name in financial_names:
                free_value, provider = free.value(name)
                checks.append(
                    check_field(
                        name,
                        values.get(name),
                        free_value,
                        tolerance=registry.tolerance(name),
                        free_provider=provider,
                    )
                )
            previous = await latest_ifind_row(session, item.listing_id, item.report_period)
            fields = _merge_ifind_fields(previous.fields if previous else None, canonical)
            old_verification = previous.fields.get("verification") if previous else None
            verification = dict(old_verification) if isinstance(old_verification, dict) else {}
            verification.update(
                {c.name: c.verdict for c in checks if c.verdict in ("verified", "conflict")}
            )
            fields["verification"] = dict(sorted(verification.items()))
            fields["verification_detail"] = {
                "request_id": item.request_id,
                "trigger": item.trigger,
                "run_id": run_id,
                "checked_at": now.isoformat(),
                "free_refs": list(free.refs),
                "checks": {c.name: c.describe() for c in checks},
            }
            row = await FinancialSnapshotRepository(session).insert(
                listing_id=item.listing_id,
                report_period=item.report_period,
                period_kind=period_kind(item.report_period),
                fields=fields,
                provider="ifind",
                captured_at=now,
                availability="approx",
                available_from=now.astimezone(SHANGHAI_TZ).date(),
                raw_ref=answers.ref,
            )
            outcome.financial_snapshot_id = row.id

    if item.wants("total_shares"):
        free_shares, provider = await latest_free_total_shares(session, item.listing_id, today)
        checks.append(
            check_field(
                "total_shares",
                answers.shares.get(item.ticker),
                free_shares,
                tolerance=registry.tolerance("total_shares"),
                free_provider="baostock" if provider else None,
            )
        )
    if item.wants("close"):
        if item.close_date is None:
            checks.append(
                FieldCheck("close", "unavailable", reason="no baostock bar to compare with")
            )
        else:
            bar = answers.bars.get(item.ticker)
            checks.append(
                check_field(
                    "close",
                    float(bar.close) if bar is not None else None,
                    item.free_close,
                    tolerance=registry.tolerance("close"),
                    free_provider="baostock",
                )
            )
            if bar is not None:
                shares = answers.shares.get(item.ticker)
                market_row = await MarketSnapshotRepository(session).insert(
                    listing_id=item.listing_id,
                    trade_date=bar.trade_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    adjust="none",
                    total_shares=Decimal(str(shares)) if shares is not None else None,
                    provider="ifind",
                    captured_at=now,
                    raw_ref=answers.ref,
                )
                outcome.market_snapshot_id = market_row.id
    for name in item.fields:
        if name not in VERIFIABLE_FIELDS:
            checks.append(
                FieldCheck(name, "unavailable", reason="no iFinD endpoint for this field")
            )
    return outcome


@dataclass
class VerifyReport:
    picked: int = 0
    superseded: int = 0
    done: int = 0
    deferred: int = 0
    failed: int = 0
    units: int = 0
    stop_reason: StopReason | None = None
    stop_error: str | None = None
    deferred_until: datetime | None = None
    conflicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    verified: dict[str, list[str]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    budget_before: dict[str, object] | None = None
    budget_after: dict[str, object] | None = None
    proxy_calls: int = 0

    @property
    def status(self) -> Literal["success", "partial"]:
        # quota stop or a failed unit (re-queued or failed for good); a budget
        # deferral is the normal flow and not an error
        return "partial" if self.stop_reason == "quota" or self.failures else "success"

    def summary(self) -> str:
        text = (
            f"picked {self.picked}: done {self.done}, deferred {self.deferred}, "
            f"failed {self.failed}, superseded {self.superseded}; "
            f"conflicts {len(self.conflicts)}; proxy calls {self.proxy_calls}"
        )
        if self.stop_reason == "quota":
            text += "; stopped: iFinD quota exhausted"
        elif self.stop_reason == "budget":
            text += f"; budget exhausted until {self.deferred_until.isoformat() if self.deferred_until else '?'}"
        return text

    def to_metadata(self, slot: str) -> dict[str, object]:
        metadata: dict[str, object] = {
            "slot": slot,
            "picked": self.picked,
            "superseded": self.superseded,
            "done": self.done,
            "deferred": self.deferred,
            "failed": self.failed,
            "units": self.units,
            "proxy_calls": self.proxy_calls,
            "budget_before": self.budget_before,
            "budget_after": self.budget_after,
            "conflict_count": len(self.conflicts),
            "conflicts": dict(sorted(self.conflicts.items())[:_MAX_LISTED]),
            "key_field_conflicts": sorted(
                ticker for ticker, item in self.conflicts.items() if item.get("key_fields")
            ),
            "verified": dict(sorted(self.verified.items())[:_MAX_LISTED]),
            "failures": dict(sorted(self.failures.items())[:_MAX_LISTED]),
        }
        if self.deferred_until is not None:
            metadata["deferred_until"] = self.deferred_until.isoformat()
        if self.stop_reason is not None:
            metadata["stop_reason"] = self.stop_reason
            metadata["stop_error"] = self.stop_error
        return metadata


async def _prepare(
    *,
    run_id: int,
    lease: RunLease | None,
    now: datetime,
    today: date,
    limit: int,
    registry: FieldSourceRegistry,
    report: VerifyReport,
) -> list[WorkItem]:
    """Pick due requests, fold duplicates, mark them running (one transaction)."""

    async with session_scope() as session:
        await fence_writes(session, lease)
        await release_stale_running(session, now=now)
        requests = await due_requests(session, now=now, limit=limit)
        report.picked = len(requests)
        items: dict[tuple[int, date], WorkItem] = {}
        order: list[tuple[int, date]] = []
        for request in requests:
            period = request.report_period or await latest_annual_period(
                session, request.listing_id, today
            )
            key = (request.listing_id, period)
            fields = request.fields
            current = items.get(key)
            if current is None:
                items[key] = WorkItem(
                    request_id=request.id,
                    listing_id=request.listing_id,
                    ticker=request.ticker,
                    trigger=request.trigger,
                    priority=request.priority,
                    report_period=period,
                    fields=list(fields) if fields is not None else default_fields(registry),
                    attempts=request.attempts,
                    raw_fields=list(fields) if fields is not None else None,
                )
                order.append(key)
                request.status = "running"
                request.run_id = run_id
                request.not_before = None
                continue
            # Same listing and period: the earlier (higher-priority) request absorbs it.
            united = merge_fields(current.fields, fields if fields is not None else default_fields(registry))
            current.fields = united or current.fields
            current.raw_fields = merge_fields(current.raw_fields, fields)
            current.merged_ids.append(request.id)
            request.status = "superseded"
            request.run_id = run_id
            request.finished_at = to_utc(now)
            request.result = {"superseded_by": current.request_id}
            report.superseded += 1
        for key in order:
            item = items[key]
            if item.merged_ids:
                primary = await session.get(VerificationRequest, item.request_id)
                assert primary is not None
                primary.fields = item.raw_fields
            item.fields = sorted(item.fields)
            if item.wants("close"):
                bar = await latest_free_bar(session, item.listing_id, today)
                if bar is not None:
                    item.close_date = bar.trade_date
                    item.free_close = float(bar.close)
                    item.free_close_ref = f"market_snapshot:{bar.id}"
        await session.flush()
    return [items[key] for key in order]


async def _update_requests(
    lease: RunLease | None,
    items: list[WorkItem],
    *,
    status: str,
    now: datetime,
    not_before: datetime | None = None,
    result: dict[str, Any] | None = None,
    count_attempt: bool = False,
) -> None:
    async with session_scope() as session:
        await fence_writes(session, lease)
        for item in items:
            request = await session.get(VerificationRequest, item.request_id)
            if request is None:
                continue
            request.status = status
            request.not_before = to_utc(not_before) if not_before is not None else None
            if count_attempt:
                request.attempts = (request.attempts or 0) + 1
            if result is not None:
                request.result = result
            if status in {"done", "failed"}:
                request.finished_at = to_utc(now)


async def execute_verification(
    payload: IfindVerifyPayload,
    *,
    run_id: int,
    attempt: int,
    lease: RunLease | None,
    runtime: VerifyRuntime,
) -> VerifyReport:
    settings = runtime.settings
    registry = runtime.field_sources()
    now = as_utc(runtime.clock())
    today = now.astimezone(SHANGHAI_TZ).date()
    report = VerifyReport()
    items = await _prepare(
        run_id=run_id,
        lease=lease,
        now=now,
        today=today,
        limit=payload.max_requests or settings.ifind_verify_max_requests,
        registry=registry,
        report=report,
    )
    if not items:
        return report
    units = plan_units(items)
    report.units = len(units)
    artifacts = ArtifactWriter(settings.data_dir, run_id=run_id, attempt=attempt)

    async with runtime.proxy_factory() as client:
        budgeted = BudgetedProxyClient(
            client,
            cap=runtime.budget_cap,
            window=runtime.budget_window,
            clock=runtime.clock,
            run_id=run_id,
        )
        caller = ProxyBatchCaller(
            budgeted,
            runtime.limiter,
            retries=settings.ifind_collect_batch_retries,
            backoff_seconds=runtime.batch_backoff_seconds,
            sleep=runtime.sleep,
        )
        before = await budgeted.status()
        report.budget_before = before.describe()
        for index, unit in enumerate(units, start=1):
            rest = [item for later in units[index - 1 :] for item in later.items]
            if unit.calls:
                status: BudgetStatus = await budgeted.status()
                if status.remaining < unit.calls:
                    await _defer(report, lease, rest, now, status.available_at(unit.calls),
                                 "budget", f"budget: {status.used}/{status.cap} calls used")
                    break
            budgeted.request_id = unit.items[0].request_id
            try:
                answers = (
                    await fetch_unit(
                        unit, caller=caller, artifacts=artifacts, index=index, today=today
                    )
                    if unit.calls
                    else UnitAnswers()
                )
            except LeaseLostError:
                raise
            except BudgetExhaustedError as exc:
                await _defer(report, lease, rest, now, exc.available_at, "budget", str(exc))
                break
            except Exception as exc:
                if is_quota_exhausted(exc):
                    await _defer(report, lease, rest, now, now + runtime.budget_window,
                                 "quota", error_reason(exc))
                    break
                await _fail_unit(report, lease, list(unit.items), now, exc)
                continue
            await _settle_unit(report, lease, unit, answers, registry, now, today, run_id)
        report.proxy_calls = budgeted.calls
        report.budget_after = (await budgeted.status()).describe()
    return report


async def _defer(
    report: VerifyReport,
    lease: RunLease | None,
    items: list[WorkItem],
    now: datetime,
    not_before: datetime,
    reason: StopReason,
    error: str,
) -> None:
    report.stop_reason = reason
    report.stop_error = error
    report.deferred_until = not_before
    report.deferred += len(items)
    await _update_requests(
        lease,
        items,
        status="deferred",
        now=now,
        not_before=not_before,
        result={"deferred": reason, "not_before": not_before.isoformat(), "detail": error},
    )
    log.warning(
        "verification.deferred",
        reason=reason,
        requests=[item.request_id for item in items],
        not_before=not_before.isoformat(),
        error=error,
    )


async def _fail_unit(
    report: VerifyReport,
    lease: RunLease | None,
    items: list[WorkItem],
    now: datetime,
    exc: BaseException,
) -> None:
    reason = error_reason(exc)
    log.warning("verification.unit_failed", tickers=[i.ticker for i in items], error=reason)
    retry = [item for item in items if item.attempts + 1 < MAX_ATTEMPTS]
    final = [item for item in items if item.attempts + 1 >= MAX_ATTEMPTS]
    if retry:
        report.deferred += len(retry)
        await _update_requests(
            lease, retry, status="deferred", now=now, not_before=now + RETRY_AFTER,
            result={"error": reason, "retry": True}, count_attempt=True,
        )
    if final:
        report.failed += len(final)
        await _update_requests(
            lease, final, status="failed", now=now, result={"error": reason},
            count_attempt=True,
        )
    for item in items:
        report.failures[item.ticker] = reason


async def _settle_unit(
    report: VerifyReport,
    lease: RunLease | None,
    unit: UnitPlan,
    answers: UnitAnswers,
    registry: FieldSourceRegistry,
    now: datetime,
    today: date,
    run_id: int,
) -> None:
    for item in unit.items:
        async with session_scope() as session:
            await fence_writes(session, lease)
            outcome = await settle_item(
                session, item, answers, registry=registry, now=now, today=today, run_id=run_id
            )
            result = outcome.result(item, run_id)
            request = await session.get(VerificationRequest, item.request_id)
            if request is not None:
                request.status = "done"
                request.result = result
                request.attempts = (request.attempts or 0) + 1
                request.finished_at = to_utc(now)
                request.not_before = None
        report.done += 1
        if result["verified"]:
            report.verified[item.ticker] = result["verified"]
        if result["conflicts"]:
            key_fields = [
                name
                for name in result["conflicts"]
                if (source := registry.get(name)) is not None and source.key
            ]
            report.conflicts[item.ticker] = {
                "request_id": item.request_id,
                "trigger": item.trigger,
                "report_period": item.report_period.isoformat(),
                "fields": {name: result["fields"][name] for name in result["conflicts"]},
                "key_fields": key_fields,
            }


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #


class IfindVerifyHandler(BaseJobHandler):
    kind = IFIND_VERIFY

    def __init__(self, settings: Settings, runtime: VerifyRuntime | None = None) -> None:
        self.runtime = runtime or default_verify_runtime(settings)

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        payload = IfindVerifyPayload.model_validate(job.request_json)

        async def work() -> VerifyReport:
            return await execute_verification(
                payload,
                run_id=job.run_id,
                attempt=job.attempt,
                lease=job.lease,
                runtime=self.runtime,
            )

        report = await ctx.run_with_heartbeat(job.lease, work)
        metadata = report.to_metadata(payload.slot)
        async with session_scope() as session:
            repository = PipelineRunRepository(session)
            if report.status == "success":
                await repository.mark_success(job.run_id, metadata, lease=job.lease)
            else:
                await repository.mark_partial(
                    job.run_id, report.summary(), metadata, lease=job.lease
                )
        log.info("verification.run.finished", run_id=job.run_id, summary=report.summary())
        return "completed" if report.status == "success" else "partial"

    async def finalize(self, job: ClaimedJob) -> None:
        from investment_agent.verification.notifications import notify_verification_run

        try:
            await notify_verification_run(job.run_id, notifier=self.runtime.file_notifier())
        except Exception:
            log.exception("verification.notification_failed", run_id=job.run_id)


async def requests_of_run(session: AsyncSession, run_id: int) -> list[VerificationRequest]:
    stmt = (
        select(VerificationRequest)
        .where(VerificationRequest.run_id == run_id)
        .order_by(VerificationRequest.id)
    )
    return list((await session.execute(stmt)).scalars())


__all__ = [
    "IFIND_VERIFY",
    "IfindVerifyHandler",
    "IfindVerifyPayload",
    "VerifyRuntime",
    "default_verify_runtime",
    "dispatch_slot",
    "enqueue_verify_run",
    "execute_verification",
    "plan_units",
]

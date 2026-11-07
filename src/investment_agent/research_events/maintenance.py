"""Dedicated bounded mechanical ticks with explicit composition and lease fences.

No research runner, model, transport, notification or unfinished inbox import is
used here. Each delivery/wakeup owns a short injected transaction; both fences
and its projections commit together. The queue worker owns claiming/recovery.
The tick budget covers its transactions and callbacks; queue claim/heartbeat/
failure settlement remain worker responsibilities. SQLite lock contention ends
the tick without claiming uncommitted progress. Final success settlement also
uses a short lock wait and propagates contention for the worker to retry.
"""

from __future__ import annotations

import asyncio
import math
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID
from weakref import WeakSet

from pydantic import field_validator
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import ConnectionPoolEntry, Pool, PoolResetState

from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.research_events.delivery import DeliveryService, OrderingResolver
from investment_agent.research_events.registry import EventRegistry, get_default_registry
from investment_agent.research_events.repository import DeliveryRepository, ReceiptRecord, _now
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)

MAINTENANCE_KIND = "research_events_maintenance"
SINGLETON_INTERVAL_SECONDS = 30
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
LeaseFence = Callable[[AsyncSession, RunLease], Awaitable[None]]
_BUSY_TIMEOUT_INFO = "research_events_original_busy_timeout"
_busy_reset_pools: WeakSet[Pool] = WeakSet()


def _restore_busy_timeout(dbapi_connection: Any, connection_record: ConnectionPoolEntry,
                          reset_state: PoolResetState) -> None:
    original = connection_record.info.pop(_BUSY_TIMEOUT_INFO, None)
    if original is None or reset_state.terminate_only:
        # Terminating/invalidated connections are discarded, never reused.
        return
    # Reset runs after the session's commit/rollback, before pool checkin. An
    # error here makes SQLAlchemy discard the connection rather than leak its
    # mechanical timeout into an ordinary research session.
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={original}")
    finally:
        cursor.close()


async def _bound_sqlite_wait(session: AsyncSession) -> None:
    connection = await session.connection()
    if connection.dialect.name != "sqlite":
        return
    sync_connection = connection.sync_connection
    assert sync_connection is not None  # session.connection() has started it.
    pool = sync_connection.engine.pool
    if pool not in _busy_reset_pools:
        event.listen(pool, "reset", _restore_busy_timeout)
        _busy_reset_pools.add(pool)
    info = sync_connection.info
    if _BUSY_TIMEOUT_INFO not in info:
        original = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one()
        info[_BUSY_TIMEOUT_INFO] = int(original)
    # Async cancellation cannot interrupt SQLite's worker-thread busy handler.
    # Keep lock acquisition (including COMMIT and rollback cleanup) short while
    # this connection is checked out; the pool reset restores its own policy.
    await connection.exec_driver_sql("PRAGMA busy_timeout=5")


def _sqlite_contention(error: OperationalError) -> bool:
    code = getattr(error.orig, "sqlite_errorcode", None)
    return (isinstance(error.orig, sqlite3.OperationalError) and isinstance(code, int)
            and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))


class WakeupSelector(Protocol):
    async def __call__(self, session: AsyncSession, *, now: datetime, limit: int) -> Sequence[UUID]: ...


class WakeupApplier(Protocol):
    async def __call__(self, session: AsyncSession, target: UUID, *, now: datetime) -> bool: ...


def _name(value: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_]*\.v[1-9][0-9]*", value) or len(value) > 80:
        raise ValueError("maintenance bindings require stable versioned names")


@dataclass(frozen=True)
class ConsumerBinding:
    name: str
    ordering_key: OrderingResolver
    projector_version: str = "events/1"

    def __post_init__(self) -> None:
        _name(self.name)
        if not self.projector_version or len(self.projector_version) > 80:
            raise ValueError("invalid projector version")


@dataclass(frozen=True)
class WakeupBinding:
    name: str
    select_due: WakeupSelector
    apply: WakeupApplier

    def __post_init__(self) -> None:
        _name(self.name)


@dataclass(frozen=True)
class MaintenanceBindings:
    registry: EventRegistry
    consumers: tuple[ConsumerBinding, ...] = ()
    wakeups: tuple[WakeupBinding, ...] = ()

    def __post_init__(self) -> None:
        names = tuple(binding.name for binding in self.consumers)
        wakeups = tuple(binding.name for binding in self.wakeups)
        if len(set(names)) != len(names) or len(set(wakeups)) != len(wakeups):
            raise ValueError("duplicate maintenance binding")
        if set(names) != set(self.registry.consumer_names):
            raise ValueError("every registered consumer requires an explicit mechanical binding")

    @property
    def empty(self) -> bool:
        return not self.consumers and not self.wakeups


_consumers: dict[str, ConsumerBinding] = {}
_wakeups: dict[str, WakeupBinding] = {}


def register_maintenance_consumer(binding: ConsumerBinding) -> None:
    """Trusted static app composition only; no request/script callback loader."""
    get_default_registry().consumer(binding.name)
    if binding.name in _consumers:
        raise ValueError(f"maintenance consumer already installed: {binding.name}")
    _consumers[binding.name] = binding


def register_maintenance_wakeup(binding: WakeupBinding) -> None:
    if binding.name in _wakeups:
        raise ValueError(f"maintenance wakeup already installed: {binding.name}")
    _wakeups[binding.name] = binding


def get_maintenance_bindings() -> MaintenanceBindings:
    return MaintenanceBindings(registry=get_default_registry(),
        consumers=tuple(_consumers[name] for name in sorted(_consumers)),
        wakeups=tuple(_wakeups[name] for name in sorted(_wakeups)))


@dataclass(frozen=True)
class TickLimits:
    new_events: int = 50
    retries: int = 20
    wakeups: int = 20
    time_budget_seconds: float = 2.0

    def __post_init__(self) -> None:
        for value, maximum in ((self.new_events, 50), (self.retries, 20), (self.wakeups, 20)):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("tick quotas are global: <=50 new/20 retry/20 wakeup")
        if (isinstance(self.time_budget_seconds, bool) or not math.isfinite(self.time_budget_seconds)
                or not 0 < self.time_budget_seconds <= 2):
            raise ValueError("total tick budget must be finite and in (0, 2] seconds")


DEFAULT_TICK_LIMITS = TickLimits()


class TickReport(Contract):
    status: Literal["completed", "budget_exhausted", "no_consumers"]
    consumers: tuple[str, ...]
    new_events: int = 0
    retries: int = 0
    wakeups: int = 0
    wakeup_applied: int = 0
    applied: int = 0
    ignored: int = 0
    retry_pending: int = 0
    blocked: int = 0
    quarantined: int = 0
    elapsed_ms: int = 0
    next_wakeup_index: int = 0


class MaintenanceRequest(Contract):
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version must be a strict integer")
        return value


async def fence_lease(session: AsyncSession, lease: RunLease) -> None:
    """Atomic same-transaction fence, including the dedicated queue kind."""
    result = await session.execute(update(PipelineRun).where(
        PipelineRun.id == lease.run_id, PipelineRun.kind == MAINTENANCE_KIND,
        PipelineRun.status == "running", PipelineRun.worker_id == lease.worker_id,
        PipelineRun.attempt_count == lease.attempt,
    ).values(heartbeat_at=datetime.now(UTC)).execution_options(synchronize_session=False))
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise LeaseLostError(lease.run_id, "mechanical tick no longer owns its dedicated run")


@asynccontextmanager
async def _unit(sessions: SessionFactory, lease: RunLease, fence: LeaseFence) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:
        await _bound_sqlite_wait(session)
        await fence(session, lease)
        yield session
        await fence(session, lease)  # Immediately before caller-owned commit.


async def enqueue_maintenance_tick(session: AsyncSession, *, now: datetime | None = None,
                                   bindings: MaintenanceBindings | None = None) -> tuple[PipelineRun | None, bool]:
    installed = bindings if bindings is not None else get_maintenance_bindings()
    if installed.empty:
        return None, False
    await DeliveryRepository(session, registry=installed.registry)._reserve_sqlite_outer()
    active = await session.scalar(select(PipelineRun).where(
        PipelineRun.kind == MAINTENANCE_KIND, PipelineRun.status.in_(("queued", "running")),
    ).order_by(PipelineRun.id).limit(1))
    if active is not None:
        return active, False
    at = utc_timestamp(now) if now is not None else _now()
    slot = int(at.timestamp()) // SINGLETON_INTERVAL_SECONDS
    return await PipelineRunRepository(session).enqueue(kind=MAINTENANCE_KIND,
        idempotency_key=f"research-events-maintenance:{slot}", triggered_by="scheduler",
        request_json={"schema_version": 1}, available_at=at, max_attempts=3)


@dataclass
class _TickProgress:
    started: float
    consumers: tuple[str, ...]
    used: dict[str, int] = field(default_factory=lambda: {"new": 0, "retry": 0, "wakeup": 0})
    states: dict[str, int] = field(default_factory=lambda: {
        "applied": 0, "ignored": 0, "retry": 0, "blocked_predecessor": 0, "quarantined": 0})
    wakeup_applied: int = 0
    next_wakeup_index: int = 0

    def report(self, status: Literal["completed", "budget_exhausted", "no_consumers"], elapsed: int) -> TickReport:
        return TickReport(status=status, consumers=self.consumers,
            new_events=self.used["new"], retries=self.used["retry"], wakeups=self.used["wakeup"],
            wakeup_applied=self.wakeup_applied, applied=self.states["applied"], ignored=self.states["ignored"],
            retry_pending=self.states["retry"], blocked=self.states["blocked_predecessor"],
            quarantined=self.states["quarantined"], elapsed_ms=elapsed, next_wakeup_index=self.next_wakeup_index)


async def run_maintenance_tick(*, session_factory: SessionFactory, bindings: MaintenanceBindings,
                               lease: RunLease, limits: TickLimits = DEFAULT_TICK_LIMITS,
                               clock: Callable[[], datetime] = _now,
                               monotonic: Callable[[], float] = time.monotonic,
                               lease_fence: LeaseFence = fence_lease) -> TickReport:
    wall_started = time.monotonic()
    progress = _TickProgress(started=monotonic(), consumers=tuple(item.name for item in bindings.consumers))
    timer = asyncio.timeout(limits.time_budget_seconds)
    try:
        # The deadline covers planning, callbacks, fences and all categories.
        # Cancellation unwinds the current session before reporting timeout;
        # only attempts whose transaction already exited enter progress counts.
        async with timer:
            return await _run_maintenance_tick(progress=progress, session_factory=session_factory,
                bindings=bindings, lease=lease, limits=limits, clock=clock,
                monotonic=monotonic, lease_fence=lease_fence)
    except TimeoutError:
        if not timer.expired():
            raise  # A callback's own TimeoutError is not our tick deadline.
        elapsed = max(monotonic() - progress.started, time.monotonic() - wall_started)
        return progress.report("budget_exhausted", max(0, int(elapsed * 1000)))
    except OperationalError as error:
        if not _sqlite_contention(error):
            raise
        # A held SQLite writer cannot make progress within this short unit.
        # The caller-owned transaction has rolled back before this report;
        # leave all uncommitted work for the next mechanical tick.
        elapsed = max(monotonic() - progress.started, time.monotonic() - wall_started)
        return progress.report("budget_exhausted", max(0, int(elapsed * 1000)))


async def _run_maintenance_tick(*, progress: _TickProgress, session_factory: SessionFactory, bindings: MaintenanceBindings,
                               lease: RunLease, limits: TickLimits = DEFAULT_TICK_LIMITS,
                               clock: Callable[[], datetime] = _now,
                               monotonic: Callable[[], float] = time.monotonic,
                               lease_fence: LeaseFence = fence_lease) -> TickReport:
    started = progress.started
    if bindings.empty:
        # The handler still fences/settles its queue job. This is not evidence
        # that any uninstalled projection/source/consumer was covered.
        return TickReport(status="no_consumers", consumers=())
    scores: dict[str, tuple[datetime, int, str]] = {}
    wakeup_index = 0
    async with _unit(session_factory, lease, lease_fence) as session:
        repo = DeliveryRepository(session, registry=bindings.registry, clock=clock)
        for binding in bindings.consumers:
            cursor = await repo.cursor(binding.name)
            scores[binding.name] = (cursor.updated_at, cursor.revision, binding.name)
        metadata = await session.scalar(select(PipelineRun.metadata_json).where(PipelineRun.id == lease.run_id))
        prior_progress = (metadata or {}).get("research_events_maintenance_progress", {})
        if not isinstance(prior_progress, dict) or not isinstance(prior_progress.get("next_wakeup_index"), int):
            previous = await session.scalar(select(PipelineRun.metadata_json).where(
                PipelineRun.kind == MAINTENANCE_KIND, PipelineRun.status == "success",
            ).order_by(PipelineRun.id.desc()).limit(1))
            prior_progress = (previous or {}).get("research_events_maintenance", {})
        if isinstance(prior_progress, dict) and type(prior_progress.get("next_wakeup_index")) is int:
            wakeup_index = max(0, prior_progress["next_wakeup_index"])
            progress.next_wakeup_index = wakeup_index
    consumers = sorted(bindings.consumers, key=lambda binding: scores[binding.name])
    positions = {"new": 0, "retry": 0}
    used = progress.used
    ceilings = {"new": limits.new_events, "retry": limits.retries, "wakeup": limits.wakeups}
    states = progress.states
    active = {category for category in ceilings if ceilings[category] and
              (bindings.wakeups if category == "wakeup" else consumers)}

    def expired() -> bool:
        return monotonic() - started >= limits.time_budget_seconds

    async def delivery(category: str) -> ReceiptRecord | None:
        for _ in consumers:
            index = positions[category] % len(consumers)
            positions[category] = (index + 1) % len(consumers)
            binding = consumers[index]
            async with _unit(session_factory, lease, lease_fence) as session:
                service = DeliveryService(session, registry=bindings.registry,
                    ordering_key=binding.ordering_key, projector_version=binding.projector_version, clock=clock)
                ready = await service.ready_work(binding.name,
                    new_limit=1 if category == "new" else 0, retry_limit=1 if category == "retry" else 0)
                items = ready.new_event_ids if category == "new" else ready.retry_event_ids
                if items:
                    return await service.deliver_one(binding.name, items[0])
            if expired():
                break
        return None

    async def wakeup() -> bool | None:
        nonlocal wakeup_index
        for _ in bindings.wakeups:
            index = wakeup_index % len(bindings.wakeups)
            next_index = (index + 1) % len(bindings.wakeups)
            binding = bindings.wakeups[index]
            async with _unit(session_factory, lease, lease_fence) as session:
                at = utc_timestamp(clock())
                targets = tuple(await binding.select_due(session, now=at, limit=1))
                if len(targets) > 1 or any(not isinstance(target, UUID) for target in targets):
                    raise ValueError("wakeup selector must respect the exact one-item bound")
                if targets:
                    changed = await binding.apply(session, targets[0], now=at)
                    if type(changed) is not bool:
                        raise ValueError("wakeup applier must return an explicit boolean")
                    await PipelineRunRepository(session).record_metadata(lease.run_id,
                        {"research_events_maintenance_progress": {"next_wakeup_index": next_index}}, lease=lease)
                    wakeup_index = next_index
                    return changed
            wakeup_index = next_index
            if expired():
                break
        return None

    # Interleave categories so a steady new stream cannot consume the whole
    # time budget before retries/wakeups. Consumer round-robin is inside each
    # global quota; older cursor activity wins the next tick's starting order.
    while active and not expired():
        for category in ("retry", "new", "wakeup"):
            if category not in active:
                continue
            if expired():
                break
            result = await wakeup() if category == "wakeup" else await delivery(category)
            if result is None:
                active.remove(category)
                continue
            used[category] += 1
            if isinstance(result, ReceiptRecord):
                states[result.state] += 1
            else:
                progress.wakeup_applied += int(result)
                progress.next_wakeup_index = wakeup_index
            if used[category] >= ceilings[category]:
                active.remove(category)
    elapsed = max(0, int((monotonic() - started) * 1000))
    progress.next_wakeup_index = wakeup_index
    return progress.report("budget_exhausted" if active and expired() else "completed", elapsed)


class ResearchEventMaintenanceHandler(BaseJobHandler):
    kind = MAINTENANCE_KIND

    def __init__(self, *, session_factory: SessionFactory,
                 bindings_factory: Callable[[], MaintenanceBindings], limits: TickLimits = DEFAULT_TICK_LIMITS,
                 clock: Callable[[], datetime] = _now, monotonic: Callable[[], float] = time.monotonic,
                 lease_fence: LeaseFence = fence_lease) -> None:
        self.session_factory, self.bindings_factory = session_factory, bindings_factory
        self.limits, self.clock, self.monotonic, self.lease_fence = limits, clock, monotonic, lease_fence

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        if job.kind != self.kind or job.run_id != job.lease.run_id or job.listing_id is not None:
            raise ValueError("maintenance handler accepts only its dedicated global run")
        MaintenanceRequest.model_validate(job.request_json)

        async def work() -> TickReport:
            report = await run_maintenance_tick(session_factory=self.session_factory,
                bindings=self.bindings_factory(), lease=job.lease, limits=self.limits,
                clock=self.clock, monotonic=self.monotonic, lease_fence=self.lease_fence)
            async with self.session_factory() as session:
                await _bound_sqlite_wait(session)
                await self.lease_fence(session, job.lease)
                # mark_success is itself lease-guarded in the same transaction.
                await PipelineRunRepository(session).mark_success(job.run_id,
                    {"research_events_maintenance": report.model_dump(mode="json")}, lease=job.lease)
            return report

        await ctx.run_with_heartbeat(job.lease, work)
        return "completed"

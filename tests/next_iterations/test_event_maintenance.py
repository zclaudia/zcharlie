"""EV-08 fake queue/context and real temporary SQLite fault boundaries."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.next_iterations.test_event_append import NOW
from tests.next_iterations.test_event_append import db as db
from tests.next_iterations.test_event_receipts import project, registry_for, seed_delivery_event

from investment_agent.jobs.base import ClaimedJob
from investment_agent.research_events import maintenance as module
from investment_agent.research_events.contracts import StoredEvent
from investment_agent.research_events.delivery import DeliveryWork
from investment_agent.research_events.maintenance import (
    MAINTENANCE_KIND,
    ConsumerBinding,
    MaintenanceBindings,
    ResearchEventMaintenanceHandler,
    SessionFactory,
    TickLimits,
    WakeupBinding,
    enqueue_maintenance_tick,
    fence_lease,
    get_maintenance_bindings,
    register_maintenance_consumer,
    register_maintenance_wakeup,
    run_maintenance_tick,
)
from investment_agent.research_events.registry import EventRegistry
from investment_agent.research_events.repository import (
    ProjectionOutcome,
    ReceiptRecord,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Company
from investment_agent.storage.models.research_events import (
    ConsumerReceipt,
    ProjectionCursor,
    ResearchDomainEvent,
)
from investment_agent.storage.models.review_panel import ReviewRoundRecord
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)


def scopes(factory: async_sessionmaker[AsyncSession]) -> SessionFactory:
    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
    return scope


async def round_chain(session: AsyncSession, raw: ResearchDomainEvent) -> str:
    row = await session.get(ReviewRoundRecord, raw.source_ref["id"], populate_existing=True)
    assert row is not None
    return f"local_owner:round:{row.id}"


def configured(handler: Any = project) -> MaintenanceBindings:
    return MaintenanceBindings(registry_for(handler),
                               consumers=(ConsumerBinding("watchlist_sync.v1", round_chain),))


async def claim(factory: async_sessionmaker[AsyncSession], bindings: MaintenanceBindings,
                *, now: datetime = NOW, worker: str = "mechanical") -> ClaimedJob:
    async with factory() as session, session.begin():
        row, created = await enqueue_maintenance_tick(session, now=now, bindings=bindings)
        assert row is not None
        row = await PipelineRunRepository(session).claim_next(kind=MAINTENANCE_KIND, run_id=row.id,
                                                              worker_id=worker, now=now)
        assert row is not None
        return ClaimedJob(run_id=row.id, kind=MAINTENANCE_KIND,
            lease=RunLease(row.id, worker, row.attempt_count), request_json=dict(row.request_json))


class FakeContext:
    worker_id = "mechanical"

    def __init__(self) -> None:
        self.calls = 0

    async def run_with_heartbeat[T](self, lease: RunLease, work: Callable[[], Awaitable[T]]) -> T:
        self.calls += 1
        return await work()


async def test_dispatch_is_empty_without_bindings_and_one_active_across_slots(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    bindings = configured()
    async with factory() as session, session.begin():
        assert await enqueue_maintenance_tick(session, bindings=MaintenanceBindings(EventRegistry()), now=NOW) == (None, False)
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 0
        first, created = await enqueue_maintenance_tick(session, bindings=bindings, now=NOW)
        assert first is not None and created
        same, again = await enqueue_maintenance_tick(session, bindings=bindings, now=NOW + timedelta(minutes=2))
        assert same is not None and same.id == first.id and not again
    async with factory() as session, session.begin():
        row = await PipelineRunRepository(session).claim_next(kind=MAINTENANCE_KIND, worker_id="mechanical", now=NOW)
        assert row is not None
        same, created = await enqueue_maintenance_tick(session, bindings=bindings, now=NOW + timedelta(minutes=2))
        assert same is not None and same.id == row.id and not created
        await PipelineRunRepository(session).mark_success(row.id, lease=RunLease(row.id, "mechanical", row.attempt_count))
    async with factory() as session, session.begin():
        same_slot, created = await enqueue_maintenance_tick(session, bindings=bindings, now=NOW)
        assert same_slot is not None and not created
        next_slot, created = await enqueue_maintenance_tick(session, bindings=bindings, now=NOW + timedelta(seconds=30))
        assert next_slot is not None and created and next_slot.id != same_slot.id


async def test_two_dispatch_sessions_share_one_global_singleton(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    bindings = configured()
    entered, waiting = asyncio.Event(), asyncio.Event()

    async def first() -> tuple[int, bool]:
        async with factory() as session, session.begin():
            row, created = await enqueue_maintenance_tick(session, bindings=bindings, now=NOW)
            assert row is not None
            entered.set()
            await waiting.wait()
            await asyncio.sleep(0.03)
            return row.id, created

    async def second() -> tuple[int, bool]:
        await entered.wait()
        waiting.set()
        async with factory() as session, session.begin():
            row, created = await enqueue_maintenance_tick(session, bindings=bindings, now=NOW + timedelta(seconds=30))
            assert row is not None
            return row.id, created

    first_result, second_result = await asyncio.wait_for(asyncio.gather(first(), second()), 5)
    assert first_result[0] == second_result[0]
    assert first_result[1] and not second_result[1]


def test_composition_is_static_explicit_and_duplicate_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = registry_for()
    monkeypatch.setattr(module, "get_default_registry", lambda: registry)
    monkeypatch.setattr(module, "_consumers", {})
    monkeypatch.setattr(module, "_wakeups", {})
    with pytest.raises(ValueError, match="explicit mechanical binding"):
        get_maintenance_bindings()
    binding = ConsumerBinding("watchlist_sync.v1", round_chain)
    register_maintenance_consumer(binding)
    assert get_maintenance_bindings().consumers == (binding,)
    with pytest.raises(ValueError, match="already installed"):
        register_maintenance_consumer(binding)
    with pytest.raises(Exception, match="Consumer is not installed"):
        register_maintenance_consumer(ConsumerBinding("review_admission.v1", round_chain))

    async def select_due(session: AsyncSession, *, now: datetime, limit: int) -> Sequence[UUID]:
        return ()

    async def apply(session: AsyncSession, target: UUID, *, now: datetime) -> bool:
        return False

    wakeup = WakeupBinding("inbox_clock.v1", select_due, apply)
    register_maintenance_wakeup(wakeup)
    with pytest.raises(ValueError, match="already installed"):
        register_maintenance_wakeup(wakeup)


@pytest.mark.parametrize("kwargs", [{"new_events": 51}, {"retries": 21}, {"wakeups": 21},
    {"new_events": True}, {"retries": -1}, {"time_budget_seconds": 0},
    {"time_budget_seconds": 2.1}, {"time_budget_seconds": float("nan")}])
def test_tick_limits_are_global_and_bounded(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        TickLimits(**kwargs)


async def test_tick_uses_short_per_event_sessions_and_settles_queue_without_research(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_agent.investor import notifications
    from investment_agent.investor.monitoring import MonitoringService

    _, factory = db
    used_sessions: list[AsyncSession] = []

    async def handler(session: AsyncSession, event: StoredEvent) -> object:
        used_sessions.append(session)
        return await project(session, event)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("mechanical tick must not run research maintenance or notifications")

    monkeypatch.setattr(MonitoringService, "maintain", forbidden)
    monkeypatch.setattr(notifications, "deliver_one", forbidden)
    bindings = configured(handler)
    async with factory() as session, session.begin():
        for number in range(1, 4):
            await seed_delivery_event(session, number)
    job = await claim(factory, bindings)
    ctx = FakeContext()
    machine = ResearchEventMaintenanceHandler(session_factory=scopes(factory), bindings_factory=lambda: bindings,
        clock=lambda: NOW, monotonic=lambda: 0)
    assert await machine.execute(job, ctx) == "completed"
    assert ctx.calls == 1 and len(used_sessions) == 3 and len({id(item) for item in used_sessions}) == 3
    async with factory() as session:
        row = await session.get(PipelineRun, job.run_id)
        assert row is not None and row.status == "success"
        report = row.metadata_json["research_events_maintenance"]
        assert report["new_events"] == 3 and report["applied"] == 3  # type: ignore[index]
        assert await session.scalar(select(func.count()).select_from(ConsumerReceipt)) == 3


async def test_no_bindings_after_dispatch_is_an_honest_no_op(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    job = await claim(factory, configured())
    handler = ResearchEventMaintenanceHandler(session_factory=scopes(factory),
        bindings_factory=lambda: MaintenanceBindings(EventRegistry()))
    assert await handler.execute(job, FakeContext()) == "completed"
    async with factory() as session:
        row = await session.get(PipelineRun, job.run_id)
        assert row is not None and row.metadata_json["research_events_maintenance"]["status"] == "no_consumers"  # type: ignore[index]
        assert await session.scalar(select(func.count()).select_from(ConsumerReceipt)) == 0
        assert await session.scalar(select(func.count()).select_from(ProjectionCursor)) == 0


async def test_precommit_lease_loss_rolls_back_current_projection(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    bindings = configured()
    async with factory() as session, session.begin():
        await seed_delivery_event(session)
    job = await claim(factory, bindings)

    async def lose_at_commit(session: AsyncSession, lease: RunLease) -> None:
        await fence_lease(session, lease)
        if await session.get(Company, 1001) is not None:
            raise LeaseLostError(lease.run_id, "injected precommit ownership loss")

    handler = ResearchEventMaintenanceHandler(session_factory=scopes(factory), bindings_factory=lambda: bindings,
        clock=lambda: NOW, lease_fence=lose_at_commit)
    with pytest.raises(LeaseLostError):
        await handler.execute(job, FakeContext())
    async with factory() as session:
        assert await session.get(Company, 1001) is None
        assert await session.scalar(select(ConsumerReceipt)) is None
        cursor = await session.get(ProjectionCursor, "watchlist_sync.v1")
        assert cursor is not None and cursor.scanned_sequence == 0
        queue = await session.get(PipelineRun, job.run_id)
        assert queue is not None and queue.status == "running"


async def test_reclaimed_lease_stops_next_unit_and_successor_resumes_committed_prefix(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    bindings = configured()
    async with factory() as session, session.begin():
        first = await seed_delivery_event(session)
        second = await seed_delivery_event(session, 2)
    job = await claim(factory, bindings)
    reclaimed = [False]

    @asynccontextmanager
    async def reclaiming_scope() -> AsyncIterator[AsyncSession]:
        async with scopes(factory)() as session:
            yield session
        if not reclaimed[0]:
            async with factory() as probe:
                applied = await probe.get(Company, 1001)
            if applied is not None:
                async with factory() as replacement, replacement.begin():
                    await replacement.execute(update(PipelineRun).where(PipelineRun.id == job.run_id).values(
                        worker_id="successor", attempt_count=2))
                reclaimed[0] = True

    handler = ResearchEventMaintenanceHandler(session_factory=reclaiming_scope, bindings_factory=lambda: bindings,
        clock=lambda: NOW, monotonic=lambda: 0)
    with pytest.raises(LeaseLostError):
        await handler.execute(job, FakeContext())
    async with factory() as session:
        assert await session.get(Company, 1001) is not None and await session.get(Company, 1002) is None
        assert (await session.scalar(select(ConsumerReceipt).where(ConsumerReceipt.event_id == str(first)))).state == "applied"  # type: ignore[union-attr]
        assert await session.scalar(select(ConsumerReceipt).where(ConsumerReceipt.event_id == str(second))) is None
    successor = ClaimedJob(run_id=job.run_id, kind=MAINTENANCE_KIND,
        lease=RunLease(job.run_id, "successor", 2), request_json={"schema_version": 1})
    handler = ResearchEventMaintenanceHandler(session_factory=scopes(factory), bindings_factory=lambda: bindings,
        clock=lambda: NOW, monotonic=lambda: 0)
    await handler.execute(successor, FakeContext())
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ConsumerReceipt)) == 2
        assert await session.get(Company, 1002) is not None


async def test_real_async_timeout_cancels_callback_and_rolls_back_only_current_unit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    cancelled = asyncio.Event()
    slow = [True]

    async def callback(session: AsyncSession, event: StoredEvent) -> object:
        outcome = await project(session, event)
        if event.sequence == 2 and slow[0]:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return outcome

    bindings = configured(callback)
    async with factory() as session, session.begin():
        await seed_delivery_event(session)
        await seed_delivery_event(session, 2)
    job = await claim(factory, bindings)
    handler = ResearchEventMaintenanceHandler(session_factory=scopes(factory), bindings_factory=lambda: bindings,
        # Allow cold SQLite setup to reach the callback even under suite load.
        limits=TickLimits(time_budget_seconds=0.6), clock=lambda: NOW)
    await asyncio.wait_for(handler.execute(job, FakeContext()), 2)
    assert cancelled.is_set()
    async with factory() as session:
        assert await session.get(Company, 1001) is not None
        assert await session.get(Company, 1002) is None
        assert await session.scalar(select(func.count()).select_from(ConsumerReceipt)) == 1
        run = await session.get(PipelineRun, job.run_id)
        assert run is not None
        report = run.metadata_json["research_events_maintenance"]
        assert report["status"] == "budget_exhausted" and report["new_events"] == 1  # type: ignore[index]
    slow[0] = False
    next_job = await claim(factory, bindings, now=NOW + timedelta(seconds=30))
    await handler.execute(next_job, FakeContext())
    async with factory() as session:
        assert await session.get(Company, 1002) is not None
        assert await session.scalar(select(func.count()).select_from(ConsumerReceipt)) == 2


async def test_slow_fake_research_does_not_block_independent_mechanical_tick(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    release = asyncio.Event()

    async def slow_research() -> None:
        await release.wait()

    research = asyncio.create_task(slow_research())
    bindings = configured()
    try:
        async with factory() as session, session.begin():
            await seed_delivery_event(session)
        job = await claim(factory, bindings)
        handler = ResearchEventMaintenanceHandler(session_factory=scopes(factory), bindings_factory=lambda: bindings,
            clock=lambda: NOW)
        await asyncio.wait_for(handler.execute(job, FakeContext()), 1)
        assert not research.done()
    finally:
        release.set()
        await research


async def test_global_50_20_20_caps_do_not_multiply_by_consumers_or_wakeup_handlers(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = db
    consumers = ("review_admission.v1", "watchlist_sync.v1")
    delivered = {name: {"new": 0, "retry": 0} for name in consumers}
    wakes = {"clock_a.v1": 0, "clock_b.v1": 0}

    class FakeDelivery:
        """Only quota scheduling is faked; separate tests verify real effects."""

        def __init__(self, session: AsyncSession, **kwargs: Any) -> None:
            pass

        async def ready_work(self, consumer: str, *, new_limit: int, retry_limit: int) -> DeliveryWork:
            return DeliveryWork(
                new_event_ids=(UUID(int=delivered[consumer]["new"] + 1),) if new_limit else (),
                retry_event_ids=(UUID(int=10_000 + delivered[consumer]["retry"] + 1),) if retry_limit else ())

        async def deliver_one(self, consumer: str, identifier: UUID) -> ReceiptRecord:
            category = "retry" if identifier.int > 10_000 else "new"
            delivered[consumer][category] += 1
            return ReceiptRecord(consumer=consumer, event_id=identifier, state="applied", ordering_key="fake-quota",
                effect_manifest=None, effect_manifest_hash=None, effect_cursor=None, attempts=int(category == "retry"),
                next_retry_at=None, last_error_code=None, error_summary=None, processed_at=NOW,
                projector_version="events/1", effect_refs=())

    def wakeup_binding(name: str) -> WakeupBinding:
        async def select_due(session: AsyncSession, *, now: datetime, limit: int) -> Sequence[UUID]:
            assert limit == 1
            return (UUID(int=wakes[name] + 1),)

        async def apply(session: AsyncSession, target: UUID, *, now: datetime) -> bool:
            wakes[name] += 1
            return True

        return WakeupBinding(name, select_due, apply)

    registry = EventRegistry()
    for name in consumers:
        registry.register_consumer(name, project, event_types=("review_panel.finalized",))
    bindings = MaintenanceBindings(registry, tuple(ConsumerBinding(name, round_chain) for name in consumers),
                                   tuple(wakeup_binding(name) for name in wakes))
    monkeypatch.setattr(module, "DeliveryService", FakeDelivery)
    job = await claim(factory, bindings)
    report = await run_maintenance_tick(session_factory=scopes(factory), bindings=bindings, lease=job.lease,
                                        clock=lambda: NOW, monotonic=lambda: 0)
    assert (report.new_events, report.retries, report.wakeups) == (50, 20, 20)
    assert all(item == {"new": 25, "retry": 10} for item in delivered.values())
    assert set(wakes.values()) == {10}


async def test_next_tick_prioritizes_unserved_consumer_even_with_frozen_clock(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    registry = EventRegistry()

    def callback(prefix: int) -> Callable[[AsyncSession, StoredEvent], Awaitable[object]]:
        async def apply(session: AsyncSession, event: StoredEvent) -> object:
            session.add(Company(id=prefix + event.sequence, canonical_name="once per consumer"))
            await session.flush()
            return ProjectionOutcome(state="applied")
        return apply

    registry.register_consumer("review_admission.v1", callback(5000), event_types=("review_panel.finalized",))
    registry.register_consumer("watchlist_sync.v1", callback(6000), event_types=("review_panel.finalized",))
    bindings = MaintenanceBindings(registry, tuple(ConsumerBinding(name, round_chain) for name in registry.consumer_names))
    async with factory() as session, session.begin():
        await seed_delivery_event(session)
    job = await claim(factory, bindings)
    limits = TickLimits(new_events=1, retries=0, wakeups=0)
    for _ in range(2):
        report = await run_maintenance_tick(session_factory=scopes(factory), bindings=bindings,
            lease=job.lease, limits=limits, clock=lambda: NOW, monotonic=lambda: 0)
        assert report.new_events == 1
    async with factory() as session:
        names = set((await session.scalars(select(ConsumerReceipt.consumer))).all())
        assert names == set(registry.consumer_names)


def due_wakeup(name: str, label: str) -> WakeupBinding:
    async def select_due(session: AsyncSession, *, now: datetime, limit: int) -> Sequence[UUID]:
        rows = (await session.scalars(select(Company.id).where(Company.canonical_name == label).order_by(Company.id).limit(limit))).all()
        return tuple(UUID(int=identity) for identity in rows)

    async def apply(session: AsyncSession, target: UUID, *, now: datetime) -> bool:
        row = await session.get(Company, target.int)
        assert row is not None
        if row.canonical_name != label:
            return False
        row.canonical_name = "woken"
        await session.flush()
        return True

    return WakeupBinding(name, select_due, apply)


async def test_wakeup_round_robin_position_is_persisted_between_ticks(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    bindings = MaintenanceBindings(EventRegistry(), wakeups=(due_wakeup("clock_a.v1", "due:a"), due_wakeup("clock_b.v1", "due:b")))
    async with factory() as session, session.begin():
        session.add_all([Company(id=8001, canonical_name="due:a"), Company(id=8002, canonical_name="due:b"),
                         Company(id=8003, canonical_name="due:a")])
    job = await claim(factory, bindings)
    reports = []
    for _ in range(2):
        reports.append(await run_maintenance_tick(session_factory=scopes(factory), bindings=bindings,
            lease=job.lease, limits=TickLimits(new_events=0, retries=0, wakeups=1), clock=lambda: NOW, monotonic=lambda: 0))
    assert [value.next_wakeup_index for value in reports] == [1, 0]
    async with factory() as session:
        assert (await session.get(Company, 8001)).canonical_name == "woken"  # type: ignore[union-attr]
        assert (await session.get(Company, 8002)).canonical_name == "woken"  # type: ignore[union-attr]
        assert (await session.get(Company, 8003)).canonical_name == "due:a"  # type: ignore[union-attr]


async def test_real_wakeup_timeout_rolls_back_current_clock_effect(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    cancelled = asyncio.Event()
    base = due_wakeup("clock_a.v1", "due:a")

    async def slow_apply(session: AsyncSession, target: UUID, *, now: datetime) -> bool:
        await base.apply(session, target, now=now)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return True

    bindings = MaintenanceBindings(EventRegistry(), wakeups=(WakeupBinding(base.name, base.select_due, slow_apply),))
    async with factory() as session, session.begin():
        session.add(Company(id=8001, canonical_name="due:a"))
    job = await claim(factory, bindings)
    handler = ResearchEventMaintenanceHandler(session_factory=scopes(factory), bindings_factory=lambda: bindings,
        # This tests callback cancellation, not cold-start scheduling latency.
        limits=TickLimits(time_budget_seconds=0.6), clock=lambda: NOW)
    await asyncio.wait_for(handler.execute(job, FakeContext()), 2)
    assert cancelled.is_set()
    async with factory() as session:
        row = await session.get(Company, 8001)
        assert row is not None and row.canonical_name == "due:a"
        run = await session.get(PipelineRun, job.run_id)
        assert run is not None and run.metadata_json["research_events_maintenance"]["wakeups"] == 0  # type: ignore[index]


async def test_non_contention_database_error_propagates_and_restores_connection_policy(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    bindings = configured()
    job = await claim(factory, bindings)

    async def invalid_fence(session: AsyncSession, lease: RunLease) -> None:
        await session.execute(text("SELECT ev08_table_does_not_exist FROM ev08_table_does_not_exist"))

    with pytest.raises(OperationalError, match="no such table"):
        await run_maintenance_tick(session_factory=scopes(factory), bindings=bindings,
            lease=job.lease, lease_fence=invalid_fence)
    async with factory() as session:
        assert await session.scalar(text("PRAGMA busy_timeout")) == 15000


async def test_final_success_contention_is_short_and_propagates_for_worker_retry(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    job = await claim(factory, configured())
    # Uninstalled after enqueue: no unit writes, so the locked operation is
    # specifically the handler's final fenced queue settlement.
    handler = ResearchEventMaintenanceHandler(session_factory=scopes(factory),
        bindings_factory=lambda: MaintenanceBindings(EventRegistry()))
    async with factory() as writer:
        await writer.execute(update(PipelineRun).where(PipelineRun.id == job.run_id).values(heartbeat_at=NOW))

        async def release() -> None:
            await asyncio.sleep(0.5)
            await writer.rollback()

        releasing = asyncio.create_task(release())
        started = time.monotonic()
        try:
            with pytest.raises(OperationalError) as failure:
                await handler.execute(job, FakeContext())
            elapsed = time.monotonic() - started
        finally:
            await releasing
            await writer.rollback()
    assert module._sqlite_contention(failure.value)
    assert elapsed < 0.25
    async with factory() as first, factory() as second:
        for session in (first, second):
            assert await session.scalar(text("PRAGMA busy_timeout")) == 15000
        run = await first.get(PipelineRun, job.run_id)
        assert run is not None and run.status == "running"
        assert "research_events_maintenance" not in (run.metadata_json or {})


async def test_oversized_wakeup_selector_is_rejected_before_any_apply(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db

    async def invalid_selector(session: AsyncSession, *, now: datetime, limit: int) -> Sequence[UUID]:
        session.add(Company(id=9001, canonical_name="selector writes must roll back"))
        await session.flush()
        return (UUID(int=1), UUID(int=2))

    async def never_apply(session: AsyncSession, target: UUID, *, now: datetime) -> bool:
        raise AssertionError("selector did not respect bound")

    bindings = MaintenanceBindings(EventRegistry(), wakeups=(WakeupBinding("clock_a.v1", invalid_selector, never_apply),))
    job = await claim(factory, bindings)
    with pytest.raises(ValueError, match="one-item bound"):
        await run_maintenance_tick(session_factory=scopes(factory), bindings=bindings, lease=job.lease, clock=lambda: NOW)
    async with factory() as session:
        assert await session.get(Company, 9001) is None


@pytest.mark.parametrize("kind,listing_id,request_body", [("ifind_research", None, {}),
    (MAINTENANCE_KIND, 18, {}), (MAINTENANCE_KIND, None, {"schema_version": True}),
    (MAINTENANCE_KIND, None, {"script": "not an accepted request"})])
async def test_handler_rejects_other_jobs_and_arbitrary_request_configuration(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], kind: str, listing_id: int | None, request_body: dict[str, Any],
) -> None:
    _, factory = db
    bindings = configured()
    original = await claim(factory, bindings)
    invalid = ClaimedJob(original.run_id, kind, original.lease, listing_id=listing_id, request_json=request_body)
    ctx = FakeContext()
    handler = ResearchEventMaintenanceHandler(session_factory=scopes(factory), bindings_factory=lambda: bindings)
    with pytest.raises(ValueError):
        await handler.execute(invalid, ctx)
    assert ctx.calls == 0

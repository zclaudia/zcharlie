"""Independent EV-11 real SQLite contention against the tick deadline."""

import asyncio
import time

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.next_iterations.test_event_append import NOW
from tests.next_iterations.test_event_append import db as db
from tests.next_iterations.test_event_maintenance import claim, configured, scopes
from tests.next_iterations.test_event_receipts import seed_delivery_event

from investment_agent.research_events.maintenance import (
    TickLimits,
    fence_lease,
    run_maintenance_tick,
)
from investment_agent.storage.database import SQLITE_BUSY_TIMEOUT_MS
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Company
from investment_agent.storage.models.research_events import ConsumerReceipt, ProjectionCursor
from investment_agent.storage.repositories.pipeline_run import RunLease


async def test_sqlite_writer_contention_does_not_outlive_tick_deadline(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    bindings = configured()
    async with factory() as session, session.begin():
        await seed_delivery_event(session)
    job = await claim(factory, bindings)
    attempted_fence = asyncio.Event()

    async def observed_fence(session: AsyncSession, lease: RunLease) -> None:
        attempted_fence.set()
        await fence_lease(session, lease)

    async with factory() as writer:
        await writer.execute(update(PipelineRun).where(PipelineRun.id == job.run_id).values(heartbeat_at=NOW))

        async def release() -> None:
            await asyncio.sleep(0.5)
            await writer.rollback()

        release_task = asyncio.create_task(release())
        started = time.monotonic()
        try:
            report = await run_maintenance_tick(session_factory=scopes(factory), bindings=bindings,
                lease=job.lease, limits=TickLimits(time_budget_seconds=0.05), lease_fence=observed_fence)
            elapsed = time.monotonic() - started
        finally:
            await release_task
            await writer.rollback()
    assert report.status == "budget_exhausted"
    assert attempted_fence.is_set(), "the contention probe must actually reach the blocked lease UPDATE"
    assert report.new_events == report.retries == report.wakeups == 0
    # Check out both connections concurrently, including the one used by the
    # cancelled tick: ordinary sessions must retain their original pool policy.
    async with factory() as first, factory() as second:
        for session in (first, second):
            assert await session.scalar(text("PRAGMA busy_timeout")) == SQLITE_BUSY_TIMEOUT_MS
        assert await first.scalar(select(func.count()).select_from(ConsumerReceipt)) == 0
        assert await first.scalar(select(func.count()).select_from(ProjectionCursor)) == 0
        assert await first.get(Company, 1001) is None
    # A generous fivefold scheduling allowance still distinguishes cancellation
    # at the deadline from waiting for the other writer's 500ms transaction.
    assert elapsed < 0.25, f"50ms tick waited {elapsed:.3f}s for a SQLite writer"

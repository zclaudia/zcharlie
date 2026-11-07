"""EV-10 independent real competing-session projection CAS acceptance."""

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.next_iterations.test_event_append import NOW
from tests.next_iterations.test_event_append import db as db
from tests.next_iterations.test_event_receipts import event_from, registry_for, seed_delivery_event

from investment_agent.research_events.contracts import StoredEvent
from investment_agent.research_events.repository import (
    DeliveryRepository,
    ProjectionCASConflictError,
    ProjectionOutcome,
)
from investment_agent.storage.models.reference import Company
from investment_agent.storage.models.research_events import ConsumerReceipt


async def test_real_two_sessions_with_same_expected_cursor_only_one_commits(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    entered, contender_started = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    async def project(session: AsyncSession, event: StoredEvent) -> ProjectionOutcome:
        calls.append(event.sequence)
        session.add(Company(id=8001, canonical_name="one CAS winner"))
        await session.flush()
        entered.set()
        await contender_started.wait()
        await asyncio.sleep(0.03)
        return ProjectionOutcome(state="applied")

    registry = registry_for(project)
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        repo = DeliveryRepository(session, registry=registry, clock=lambda: NOW)
        expected = (await repo.cursor("watchlist_sync.v1")).revision

    async def attempt(*, contender: bool) -> str:
        if contender:
            await entered.wait()
            contender_started.set()
        try:
            async with factory() as session, session.begin():
                repo = DeliveryRepository(session, registry=registry, clock=lambda: NOW)
                await repo.apply_projection("watchlist_sync.v1", await event_from(repo, identifier),
                    ordering_key="listing:18", expected_cursor_revision=expected, projector_version="acceptance/1")
            return "committed"
        except ProjectionCASConflictError:
            return "stale"

    results = await asyncio.wait_for(asyncio.gather(attempt(contender=False), attempt(contender=True)), 5)
    assert tuple(results) == ("committed", "stale") and calls == [1]
    async with factory() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(Company).where(Company.id == 8001)) == 1
        assert await session.scalar(select(func.count()).select_from(ConsumerReceipt)) == 1
        repo = DeliveryRepository(session, registry=registry, clock=lambda: NOW)
        cursor = await repo.cursor("watchlist_sync.v1")
        assert cursor.revision == expected + 1 and cursor.scanned_sequence == 1
        replay = await repo.apply_projection("watchlist_sync.v1", await event_from(repo, identifier),
            ordering_key="listing:18", expected_cursor_revision=cursor.revision, projector_version="acceptance/1")
        assert replay.state == "applied" and calls == [1]

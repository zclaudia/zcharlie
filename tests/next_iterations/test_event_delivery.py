"""EV-10/11 independent persisted-prefix and unknown-chain acceptance."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.next_iterations.test_event_append import NOW
from tests.next_iterations.test_event_append import db as db
from tests.next_iterations.test_event_delivery_unit import trusted_listing_chain
from tests.next_iterations.test_event_receipts import registry_for, seed_delivery_event

from investment_agent.research_events.contracts import StoredEvent
from investment_agent.research_events.delivery import DeliveryService
from investment_agent.research_events.repository import (
    CursorUpdate,
    DeliveryRepository,
    EffectManifest,
    ProjectionOutcome,
)
from investment_agent.storage.models.reference import Company
from investment_agent.storage.models.research_events import ResearchDomainEvent


async def test_committed_fifty_target_prefix_survives_failed_last_slice_and_restart(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    generation = UUID(int=8500)
    starts: list[int] = []
    fail_last = [True]
    clock = [NOW]

    async def project(session: AsyncSession, event: StoredEvent) -> ProjectionOutcome:
        repo = DeliveryRepository(session, registry=registry, clock=lambda: clock[0])
        receipt = await repo.receipt("research_inbox.v1", event.event_id)
        assert receipt is not None
        start = receipt.effect_cursor or 0
        starts.append(start)
        manifest = receipt.effect_manifest or EffectManifest(generation=generation,
            targets=tuple(UUID(int=i) for i in range(1, 52)), source_ref=event.source_ref,
            source_revision=event.source_revision, source_payload_hash=event.payload_hash)
        end = min(start + 50, len(manifest.targets))
        for target in manifest.targets[start:end]:
            session.add(Company(id=9000 + target.int, canonical_name="committed target"))
        await session.flush()
        if start == 50 and fail_last[0]:
            raise RuntimeError("last slice failed after its effect write")
        return ProjectionOutcome(state="applied" if end == 51 else "retry", reason_code="effects_pending",
            effect_manifest=manifest, effect_cursor=end)

    registry = registry_for(project, "research_inbox.v1")
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        repo = DeliveryRepository(session, registry=registry, clock=lambda: clock[0])
        cursor = await repo.cursor("research_inbox.v1")
        await repo.compare_and_set_cursor("research_inbox.v1", cursor.revision, CursorUpdate(active_generation=generation))

    async def deliver() -> tuple[str, int | None]:
        # Each invocation is a new session, as after a worker restart.
        async with factory() as session, session.begin():
            result = await DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain,
                clock=lambda: clock[0]).deliver_one("research_inbox.v1", identifier)
            return result.state, result.effect_cursor

    assert await deliver() == ("retry", 50)
    assert await deliver() == ("retry", 50)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Company).where(Company.id > 9000)) == 50
        assert await session.get(Company, 9051) is None
    fail_last[0] = False
    clock[0] += timedelta(seconds=5)
    assert await deliver() == ("applied", 51)
    assert await deliver() == ("applied", 51)
    assert starts == [0, 50, 50]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Company).where(Company.id > 9000)) == 51
        receipt = await DeliveryRepository(session, registry=registry).receipt("research_inbox.v1", identifier)
        assert receipt is not None and receipt.effect_manifest is not None
        assert receipt.effect_manifest.targets == tuple(UUID(int=i) for i in range(1, 52))
        assert receipt.attempts == 1 and receipt.state == "applied"


async def test_unknown_chain_blocks_other_listing_for_same_consumer_only(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    calls: list[UUID] = []

    async def project(session: AsyncSession, event: StoredEvent) -> ProjectionOutcome:
        calls.append(event.event_id)
        return ProjectionOutcome(state="applied")

    registry = registry_for(project)
    registry.register_consumer("review_admission.v1", project, event_types=("review_panel.finalized",))
    async with factory() as session, session.begin():
        unknown = await seed_delivery_event(session)
        other_listing = await seed_delivery_event(session, 2, listing_id=19)

    async def unavailable(session: AsyncSession, raw: ResearchDomainEvent) -> str:
        if raw.event_id == str(unknown):
            raise ValueError("cannot establish a trustworthy chain")
        return await trusted_listing_chain(session, raw)

    async with factory() as session, session.begin():
        service = DeliveryService(session, registry=registry, ordering_key=unavailable, clock=lambda: NOW)
        first = await service.deliver_one("watchlist_sync.v1", unknown)
        blocked = await service.deliver_one("watchlist_sync.v1", other_listing)
        assert first.state == "quarantined" and first.last_error_code == "ordering_key_unavailable"
        assert blocked.state == "blocked_predecessor" and blocked.attempts == 0
        assert calls == []
    async with factory() as session, session.begin():
        independent = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
        assert (await independent.deliver_one("review_admission.v1", unknown)).state == "applied"
        assert (await independent.deliver_one("review_admission.v1", other_listing)).state == "applied"
        assert calls == [unknown, other_listing]

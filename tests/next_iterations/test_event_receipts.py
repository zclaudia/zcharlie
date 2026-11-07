"""EV-06 actual projection/receipt/cursor atomicity and bounded frozen progress."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.next_iterations.test_event_append import NOW, request, seed_round
from tests.next_iterations.test_event_append import db as db

from investment_agent.investor.foundations import InvestorError
from investment_agent.research_events import canonical
from investment_agent.research_events.contracts import (
    SOURCE_REF_ADAPTER,
    ReviewPanelPayload,
    StoredEvent,
)
from investment_agent.research_events.registry import EventRegistry
from investment_agent.research_events.repository import (
    CursorUpdate,
    DeliveryRepository,
    EffectManifest,
    EventRepository,
    ProjectionCASConflictError,
    ProjectionOutcome,
    stored_event,
)
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.research_events import ConsumerReceipt, ResearchDomainEvent
from investment_agent.storage.models.review_panel import ReviewRoundRecord


async def seed_delivery_event(session: AsyncSession, number: int = 1, *, listing_id: int = 18,
                              schema_version: int = 1, malformed_hash: bool = False) -> UUID:
    round_id, comparison_id = 380 + number, 451 + number
    await seed_round(session, round_id=round_id, comparison_id=comparison_id)
    if listing_id != 18:
        if await session.get(Listing, listing_id) is None:
            session.add(Company(id=listing_id, canonical_name="other synthetic issuer"))
            session.add(Listing(id=listing_id, company_id=listing_id, ticker="600000", market_code="SSE"))
        source = await session.get(ReviewRoundRecord, round_id)
        assert source is not None
        source.listing_id = listing_id
    body = request()
    body["source_ref"]["id"] = round_id
    body["payload"]["comparison_ref"]["id"] = comparison_id
    body["payload"]["subject"]["listing_id"] = listing_id
    if schema_version == 1 and not malformed_hash:
        return (await EventRepository(session, clock=lambda: NOW).append_event(**body)).event_id
    payload = ReviewPanelPayload.model_validate(body["payload"])
    ref = SOURCE_REF_ADAPTER.validate_python(body["source_ref"])
    identifier = canonical.event_id("local_owner", "review_panel.finalized", ref, 1)
    session.add(ResearchDomainEvent(event_id=str(identifier), owner_id="local_owner",
        event_type="review_panel.finalized", source_identity=canonical.source_identity(ref),
        source_ref=canonical.normalize(ref), source_revision=1, schema_version=schema_version,
        occurred_at=NOW, recorded_at=NOW, payload_hash="b" * 64 if malformed_hash else canonical.payload_hash(payload),
        payload=canonical.normalize(payload)))
    await session.flush()
    return identifier


async def project(session: AsyncSession, event: StoredEvent) -> object:
    session.add(Company(id=1000 + event.sequence, canonical_name="projected once"))
    await session.flush()
    return ProjectionOutcome(state="applied")


def registry_for(handler: Any = project, consumer: str = "watchlist_sync.v1") -> EventRegistry:
    registry = EventRegistry()
    registry.register_consumer(consumer, handler, event_types=("review_panel.finalized",))
    return registry


async def event_from(repo: DeliveryRepository, identifier: UUID) -> StoredEvent:
    return stored_event(await repo.event_row(identifier))


async def test_projection_receipt_cursor_all_rollback_together(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
    registry = registry_for()
    async with factory() as session:
        repo = DeliveryRepository(session, registry=registry, clock=lambda: NOW)
        cursor = await repo.cursor("watchlist_sync.v1")
        await repo.apply_projection("watchlist_sync.v1", await event_from(repo, identifier),
            ordering_key="local_owner:listing:18", expected_cursor_revision=cursor.revision, projector_version="fixture/1")
        await session.rollback()
    async with factory() as session:
        assert await session.get(Company, 1001) is None
        assert await session.scalar(select(ConsumerReceipt)) is None
        assert (await session.scalar(select(ResearchDomainEvent))).event_id == str(identifier)  # type: ignore[union-attr]


async def test_projection_idempotent_receipt_and_monotonic_scan(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        repo = DeliveryRepository(session, registry=registry_for(), clock=lambda: NOW)
        event = await event_from(repo, identifier)
        cursor = await repo.cursor("watchlist_sync.v1")
        first = await repo.apply_projection("watchlist_sync.v1", event, ordering_key="chain",
            expected_cursor_revision=cursor.revision, projector_version="fixture/1")
        current = await repo.cursor("watchlist_sync.v1")
        second = await repo.apply_projection("watchlist_sync.v1", event, ordering_key="chain",
            expected_cursor_revision=current.revision, projector_version="fixture/1")
        assert first == second and current.scanned_sequence == 1
        assert current.last_success_at == NOW
        assert (await repo.cursor("watchlist_sync.v1")).revision == current.revision
        with pytest.raises(InvestorError, match="backwards"):
            await repo.compare_and_set_cursor("watchlist_sync.v1", current.revision, CursorUpdate(scanned_sequence=0))
        assert len((await session.scalars(select(Company).where(Company.id >= 1000))).all()) == 1


async def test_callback_and_cursor_changes_rollback_if_cas_lost(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    registry = registry_for()
    async with factory() as session:
        identifier = await seed_delivery_event(session)
        repo = DeliveryRepository(session, registry=registry, clock=lambda: NOW)
        cursor = await repo.cursor("watchlist_sync.v1")
        # Simulate a stale expected revision without letting the callback run.
        with pytest.raises(ProjectionCASConflictError):
            await repo.apply_projection("watchlist_sync.v1", await event_from(repo, identifier),
                ordering_key="chain", expected_cursor_revision=cursor.revision + 1, projector_version="fixture/1")
        assert await session.get(Company, 1001) is None
        assert await repo.receipt("watchlist_sync.v1", identifier) is None

        async def lose_cas(same_session: AsyncSession, event: StoredEvent) -> object:
            await project(same_session, event)
            await same_session.execute(update(type_cursor()).values(revision=2))
            return ProjectionOutcome(state="applied")

        nested_registry = registry_for(lose_cas)
        nested_repo = DeliveryRepository(session, registry=nested_registry, clock=lambda: NOW)
        with pytest.raises(ProjectionCASConflictError):
            await nested_repo.apply_projection("watchlist_sync.v1", await event_from(repo, identifier),
                ordering_key="chain", expected_cursor_revision=cursor.revision, projector_version="fixture/1")
        assert await session.get(Company, 1001) is None
        assert await repo.receipt("watchlist_sync.v1", identifier) is None
        assert (await repo.cursor("watchlist_sync.v1")).revision == 1


def type_cursor() -> Any:
    from investment_agent.storage.models.research_events import ProjectionCursor
    return ProjectionCursor


async def test_cursor_cannot_skip_events_or_accept_unknown_consumer(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session:
        await seed_delivery_event(session)
        repo = DeliveryRepository(session, registry=registry_for(), clock=lambda: NOW)
        cursor = await repo.cursor("watchlist_sync.v1")
        with pytest.raises(InvestorError, match="skip unrecorded"):
            await repo.compare_and_set_cursor("watchlist_sync.v1", cursor.revision, CursorUpdate(scanned_sequence=1))
        with pytest.raises(InvestorError, match="Consumer is not installed"):
            await repo.cursor("unregistered.v1")
        with pytest.raises(InvestorError, match="Only the inbox"):
            await repo.compare_and_set_cursor("watchlist_sync.v1", cursor.revision, CursorUpdate(active_generation=UUID(int=500)))


async def test_manifest_freezes_progress_and_blocks_head_switch(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    generation = UUID(int=500)
    progress = [50]
    manifest_box: list[EffectManifest] = []

    async def fragment(session: AsyncSession, event: StoredEvent) -> object:
        manifest = manifest_box[0]
        return ProjectionOutcome(state="retry" if progress[0] < 51 else "applied", reason_code="effects_pending",
                                 effect_manifest=manifest, effect_cursor=progress[0])

    registry = registry_for(fragment, "research_inbox.v1")
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        repo = DeliveryRepository(session, registry=registry, clock=lambda: NOW)
        cursor = await repo.cursor("research_inbox.v1")
        cursor = await repo.compare_and_set_cursor("research_inbox.v1", cursor.revision, CursorUpdate(active_generation=generation))
        event = await event_from(repo, identifier)
        manifest_box.append(EffectManifest(generation=generation, targets=tuple(UUID(int=i) for i in range(1, 52)),
            source_ref=event.source_ref, source_revision=event.source_revision, source_payload_hash=event.payload_hash))
        await repo.apply_projection("research_inbox.v1", event, ordering_key="chain",
            expected_cursor_revision=cursor.revision, projector_version="fixture/1")
    async with factory() as session, session.begin():
        repo = DeliveryRepository(session, registry=registry, clock=lambda: NOW + timedelta(seconds=1))
        cursor = await repo.cursor("research_inbox.v1")
        with pytest.raises(InvestorError, match="Unfinished effect manifests"):
            await repo.compare_and_set_cursor("research_inbox.v1", cursor.revision, CursorUpdate(active_generation=UUID(int=501)))
        original = await repo.receipt("research_inbox.v1", identifier)
        assert original is not None and original.effect_cursor == 50
        for changed_manifest, changed_progress in [(manifest_box[0].model_copy(update={"targets": manifest_box[0].targets[:-1]}), 50), (manifest_box[0], 49)]:
            manifest_box[0] = changed_manifest
            progress[0] = changed_progress
            with pytest.raises(InvestorError):
                await repo.apply_projection("research_inbox.v1", await event_from(repo, identifier), ordering_key="chain",
                    expected_cursor_revision=cursor.revision, projector_version="fixture/1")
            assert await repo.receipt("research_inbox.v1", identifier) == original
        assert original.effect_manifest is not None
        manifest_box[0], progress[0] = original.effect_manifest, 51
        completed = await repo.apply_projection("research_inbox.v1", await event_from(repo, identifier), ordering_key="chain",
            expected_cursor_revision=cursor.revision, projector_version="fixture/1")
        assert completed.state == "applied" and completed.effect_cursor == 51
        current = await repo.cursor("research_inbox.v1")
        switched = await repo.compare_and_set_cursor("research_inbox.v1", current.revision, CursorUpdate(active_generation=UUID(int=501)))
        assert switched.active_generation == UUID(int=501)


async def test_progress_more_than_fifty_rolls_back_projection(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session:
        identifier = await seed_delivery_event(session)
        repo = DeliveryRepository(session, registry=registry_for(), clock=lambda: NOW)
        event = await event_from(repo, identifier)
        cursor = await repo.cursor("watchlist_sync.v1")
        manifest = EffectManifest(generation=UUID(int=500), targets=tuple(UUID(int=i) for i in range(1, 53)),
            source_ref=event.source_ref, source_revision=1, source_payload_hash=event.payload_hash)
        with pytest.raises(InvestorError, match="at most 50"):
            await repo.apply_projection("watchlist_sync.v1", event, ordering_key="chain",
                expected_cursor_revision=cursor.revision, projector_version="fixture/1",
                outcome=ProjectionOutcome(state="retry", reason_code="effects_pending", effect_manifest=manifest, effect_cursor=51))
        assert await repo.receipt("watchlist_sync.v1", identifier) is None


async def test_rebuild_state_is_closed_and_only_moves_forward_under_same_cas(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    from investment_agent.research_events.contracts import InboxRebuildState

    _, factory = db
    async with factory() as session:
        repo = DeliveryRepository(session, registry=registry_for(consumer="research_inbox.v1"), clock=lambda: NOW)
        cursor = await repo.cursor("research_inbox.v1")
        state = InboxRebuildState(candidate_generation=UUID(int=501), domain_watermark=0,
            journal_watermark=80, last_replayed_sequence=75, state="catching_up")
        current = await repo.compare_and_set_cursor("research_inbox.v1", cursor.revision,
            CursorUpdate(active_generation=UUID(int=500), rebuild_state=state))
        assert current.rebuild_state == state
        with pytest.raises(InvestorError, match="backwards"):
            await repo.compare_and_set_cursor("research_inbox.v1", current.revision,
                CursorUpdate(rebuild_state=state.model_copy(update={"last_replayed_sequence": 74})))
        with pytest.raises(ProjectionCASConflictError):
            await repo.compare_and_set_cursor("research_inbox.v1", cursor.revision, CursorUpdate(rebuild_state=None))
        assert (await repo.cursor("research_inbox.v1")).rebuild_state == state

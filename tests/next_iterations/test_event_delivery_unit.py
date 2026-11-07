"""EV-07 delivery tests run against real SQLite with fake clocks/local callbacks."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.next_iterations.test_event_append import NOW
from tests.next_iterations.test_event_append import db as db
from tests.next_iterations.test_event_receipts import project, registry_for, seed_delivery_event

from investment_agent.research_events.delivery import DeliveryService
from investment_agent.research_events.health import consumer_health
from investment_agent.research_events.repository import CursorUpdate, ProjectionOutcome
from investment_agent.storage.models.reference import Company
from investment_agent.storage.models.research_events import ProjectionCursor, ResearchDomainEvent
from investment_agent.storage.models.review_panel import ReviewRoundRecord


async def trusted_listing_chain(session: AsyncSession, raw: ResearchDomainEvent) -> str:
    # This controlled source identity is resolved from the DB, including future
    # envelope schema versions. No routing fields are read from raw.payload.
    source_id = raw.source_ref["id"]
    row = await session.get(ReviewRoundRecord, source_id, populate_existing=True)
    assert row is not None
    return f"local_owner:listing:{row.listing_id}"


async def test_future_schema_quarantines_then_blocks_same_chain_but_not_independent(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    calls: list[UUID] = []

    async def handler(session: AsyncSession, event: Any) -> object:
        calls.append(event.event_id)
        return await project(session, event)

    registry = registry_for(handler)
    async with factory() as session, session.begin():
        future = await seed_delivery_event(session, schema_version=2)
        same = await seed_delivery_event(session, 2)
        independent = await seed_delivery_event(session, 3, listing_id=19)
    async with factory() as session, session.begin():
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
        assert (await service.deliver_one("watchlist_sync.v1", future)).state == "quarantined"
        blocked = await service.deliver_one("watchlist_sync.v1", same)
        assert blocked.state == "blocked_predecessor" and blocked.attempts == 0
        assert (await service.deliver_one("watchlist_sync.v1", independent)).state == "applied"
        assert calls == [independent]
        health = await consumer_health(session, registry=registry, consumer="watchlist_sync.v1", now=NOW)
        assert health.status == "partial" and health.scanned_sequence == 3
        assert (health.quarantined, health.blocked, health.applied, health.unscanned) == (1, 1, 1, 0)
        assert health.last_success_at == NOW and health.oldest_unsettled_at == NOW


async def test_failure_rolls_back_callback_writes_and_retries_then_isolates(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    clock = [NOW]
    count = [0]

    async def failing(session: AsyncSession, event: Any) -> object:
        count[0] += 1
        await project(session, event)
        raise RuntimeError("secret-token=must-not-leak")

    registry = registry_for(failing)
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
    for attempt, delay in enumerate([5, 30, 120, 600, None], 1):
        async with factory() as session, session.begin():
            service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: clock[0])
            receipt = await service.deliver_one("watchlist_sync.v1", identifier)
            assert receipt.attempts == attempt
            assert receipt.state == ("retry" if delay is not None else "quarantined")
            assert "must-not-leak" not in (receipt.error_summary or "")
            assert await session.get(Company, 1001) is None
            if delay is not None:
                assert receipt.next_retry_at == clock[0] + timedelta(seconds=delay)
                assert (await service.ready_work("watchlist_sync.v1")).retry_event_ids == ()
                clock[0] = receipt.next_retry_at
            else:
                assert receipt.next_retry_at is None
    async with factory() as session, session.begin():
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: clock[0])
        await service.deliver_one("watchlist_sync.v1", identifier)
        assert count[0] == 5  # Quarantine never automatically re-runs the handler.
        assert (await service.ready_work("watchlist_sync.v1")).retry_event_ids == ()


async def test_recovered_predecessor_unblocks_following_event_without_repeating_effects(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    fail = [True]

    async def transient(session: AsyncSession, event: Any) -> object:
        if event.sequence == 1 and fail[0]:
            raise RuntimeError("temporary")
        return await project(session, event)

    registry = registry_for(transient)
    async with factory() as session, session.begin():
        first = await seed_delivery_event(session)
        second = await seed_delivery_event(session, 2)
    async with factory() as session, session.begin():
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
        assert (await service.deliver_one("watchlist_sync.v1", first)).state == "retry"
        assert (await service.deliver_one("watchlist_sync.v1", second)).state == "blocked_predecessor"
    fail[0] = False
    async with factory() as session, session.begin():
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW + timedelta(seconds=5))
        work = await service.ready_work("watchlist_sync.v1")
        assert work.retry_event_ids == (first,)
        assert (await service.deliver_one("watchlist_sync.v1", first)).state == "applied"
    async with factory() as session, session.begin():
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW + timedelta(seconds=5))
        assert (await service.ready_work("watchlist_sync.v1")).retry_event_ids == (second,)
        assert (await service.deliver_one("watchlist_sync.v1", second)).state == "applied"
        await service.deliver_one("watchlist_sync.v1", first)
        assert len((await session.scalars(select(Company).where(Company.id >= 1000))).all()) == 2


async def test_invalid_handler_and_cancelled_handler_do_not_leave_projection_writes(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db

    async def wrong_result(session: AsyncSession, event: Any) -> object:
        await project(session, event)
        return None

    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        service = DeliveryService(session, registry=registry_for(wrong_result), ordering_key=trusted_listing_chain, clock=lambda: NOW)
        assert (await service.deliver_one("watchlist_sync.v1", identifier)).state == "quarantined"
        assert await session.get(Company, 1001) is None

    async def cancelled(session: AsyncSession, event: Any) -> object:
        await project(session, event)
        raise asyncio.CancelledError()

    async with factory() as session:
        other = await seed_delivery_event(session, 2, listing_id=19)
        service = DeliveryService(session, registry=registry_for(cancelled), ordering_key=trusted_listing_chain, clock=lambda: NOW)
        with pytest.raises(asyncio.CancelledError):
            await service.deliver_one("watchlist_sync.v1", other)
        assert await session.get(Company, 1002) is None
        assert await service.repository.receipt("watchlist_sync.v1", other) is None
        await session.rollback()


async def test_ordering_failure_is_traceable_and_cannot_create_a_bypass_chain(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    failed = [True]

    async def lookup(session: AsyncSession, row: ResearchDomainEvent) -> str:
        if failed[0]:
            raise LookupError("private source detail")
        return await trusted_listing_chain(session, row)

    registry = registry_for()
    async with factory() as session, session.begin():
        first = await seed_delivery_event(session)
        second = await seed_delivery_event(session, 2)
        service = DeliveryService(session, registry=registry, ordering_key=lookup, clock=lambda: NOW)
        receipt = await service.deliver_one("watchlist_sync.v1", first)
        assert receipt.state == "quarantined" and receipt.last_error_code == "ordering_key_unavailable"
        assert receipt.ordering_key.startswith("unresolved:local_owner:")
        assert "private source detail" not in (receipt.error_summary or "")
        failed[0] = False
        assert (await service.deliver_one("watchlist_sync.v1", second)).state == "blocked_predecessor"


async def test_health_read_only_and_ready_work_bounds(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    registry = registry_for()
    async with factory() as session, session.begin():
        await seed_delivery_event(session)
        assert (await consumer_health(session, registry=registry, consumer="watchlist_sync.v1", now=NOW)).status == "not_started"
        assert await session.get(ProjectionCursor, "watchlist_sync.v1") is None
        assert (await consumer_health(session, registry=registry, consumer="research_inbox.v1", now=NOW)).status == "not_installed"
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
        for args in ({"new_limit": 51}, {"retry_limit": 21}, {"new_limit": True}, {"retry_limit": -1}):
            with pytest.raises(ValueError):
                await service.ready_work("watchlist_sync.v1", **args)
        work = await service.ready_work("watchlist_sync.v1", new_limit=1, retry_limit=0)
        assert len(work.new_event_ids) == 1 and work.retry_event_ids == ()


async def test_not_due_retry_does_not_run_handler_or_increment_attempts(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    calls = [0]

    async def transient(session: AsyncSession, event: Any) -> object:
        calls[0] += 1
        raise RuntimeError("temporary")

    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        service = DeliveryService(session, registry=registry_for(transient), ordering_key=trusted_listing_chain, clock=lambda: NOW)
        first = await service.deliver_one("watchlist_sync.v1", identifier)
        second = await service.deliver_one("watchlist_sync.v1", identifier)
        assert first == second and calls[0] == 1 and second.attempts == 1


async def test_two_sessions_do_not_repeat_same_consumer_projection(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    first_ran, second_started = asyncio.Event(), asyncio.Event()
    calls = [0]

    async def slow_once(session: AsyncSession, event: Any) -> object:
        calls[0] += 1
        result = await project(session, event)
        first_ran.set()
        await second_started.wait()
        await asyncio.sleep(0.05)
        return result

    registry = registry_for(slow_once)
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)

    async def first() -> object:
        async with factory() as session, session.begin():
            service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
            return await service.deliver_one("watchlist_sync.v1", identifier)

    async def second() -> object:
        await first_ran.wait()
        second_started.set()
        async with factory() as session, session.begin():
            service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
            return await service.deliver_one("watchlist_sync.v1", identifier)

    left, right = await asyncio.wait_for(asyncio.gather(first(), second()), 5)
    assert left == right and calls[0] == 1
    async with factory() as session:
        assert len((await session.scalars(select(Company).where(Company.id >= 1000))).all()) == 1


async def test_callback_failure_keeps_caller_outer_fact_but_caller_rollback_removes_receipt(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db

    async def fail_after_write(session: AsyncSession, event: Any) -> object:
        await project(session, event)
        raise RuntimeError("temporary")

    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
    async with factory() as session:
        session.add(Company(id=9000, canonical_name="caller outer fact"))
        service = DeliveryService(session, registry=registry_for(fail_after_write), ordering_key=trusted_listing_chain, clock=lambda: NOW)
        assert (await service.deliver_one("watchlist_sync.v1", identifier)).state == "retry"
        assert await session.get(Company, 9000) is not None
        assert await session.get(Company, 1001) is None
        await session.rollback()
    async with factory() as session:
        assert await session.get(Company, 9000) is None
        service = DeliveryService(session, registry=registry_for(fail_after_write), ordering_key=trusted_listing_chain, clock=lambda: NOW)
        assert await service.repository.receipt("watchlist_sync.v1", identifier) is None


async def test_unsubscribed_event_gets_explicit_ignored_receipt_without_callback(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    from investment_agent.research_events.registry import EventRegistry

    _, factory = db
    registry = EventRegistry()
    registry.register_consumer("watchlist_sync.v1", project, event_types=("research_change.recorded",))
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
        receipt = await service.deliver_one("watchlist_sync.v1", identifier)
        assert receipt.state == "ignored" and receipt.last_error_code == "unsupported_event_type"
        assert await session.get(Company, 1001) is None


async def test_blocked_chain_does_not_exhaust_independent_retry_budget(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db

    async def fail_independent(session: AsyncSession, event: Any) -> object:
        raise RuntimeError("temporary independent retry")

    registry = registry_for(fail_independent)
    async with factory() as session, session.begin():
        ids = [await seed_delivery_event(session, 1, schema_version=2)]
        ids.extend([await seed_delivery_event(session, number) for number in (2, 3)])
        independent = await seed_delivery_event(session, 4, listing_id=19)
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
        for identifier in ids:
            await service.deliver_one("watchlist_sync.v1", identifier)
        assert (await service.deliver_one("watchlist_sync.v1", independent)).state == "retry"
    async with factory() as session:
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW + timedelta(seconds=5))
        assert (await service.ready_work("watchlist_sync.v1", retry_limit=1)).retry_event_ids == (independent,)


async def test_failed_ordering_callback_writes_are_rolled_back_before_quarantine(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db

    async def bad_lookup(session: AsyncSession, raw: ResearchDomainEvent) -> str:
        session.add(Company(id=9001, canonical_name="must roll back"))
        await session.flush()
        raise LookupError("private detail")

    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        service = DeliveryService(session, registry=registry_for(), ordering_key=bad_lookup, clock=lambda: NOW)
        result = await service.deliver_one("watchlist_sync.v1", identifier)
        assert result.state == "quarantined"
        assert await session.get(Company, 9001) is None


async def test_handler_that_touches_immutable_event_still_records_failure_safely(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db

    async def bad_handler(session: AsyncSession, event: Any) -> object:
        raw = await session.scalar(select(ResearchDomainEvent).where(ResearchDomainEvent.event_id == str(event.event_id)))
        assert raw is not None
        raw.payload_hash = "b" * 64
        await session.flush()
        return ProjectionOutcome(state="applied")

    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        service = DeliveryService(session, registry=registry_for(bad_handler), ordering_key=trusted_listing_chain, clock=lambda: NOW)
        result = await service.deliver_one("watchlist_sync.v1", identifier)
        assert result.state == "quarantined" and result.last_error_code == "invalid_projection_result"
        assert (await service.repository.event_row(identifier)).payload_hash != "b" * 64


async def test_bad_persisted_hash_is_immediately_quarantined_without_handler(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session, malformed_hash=True)
        service = DeliveryService(session, registry=registry_for(), ordering_key=trusted_listing_chain, clock=lambda: NOW)
        result = await service.deliver_one("watchlist_sync.v1", identifier)
        assert result.state == "quarantined" and result.attempts == 1
        assert result.last_error_code == "invalid_event_contract" and result.next_retry_at is None
        assert await session.get(Company, 1001) is None


async def test_inbox_waits_for_persistent_generation_then_recovers(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    registry = registry_for(consumer="research_inbox.v1")
    async with factory() as session, session.begin():
        identifier = await seed_delivery_event(session)
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW)
        pending = await service.deliver_one("research_inbox.v1", identifier)
        assert pending.state == "retry" and pending.last_error_code == "projection_unavailable"
        assert await session.get(Company, 1001) is None
        cursor = await service.repository.cursor("research_inbox.v1")
        await service.repository.compare_and_set_cursor("research_inbox.v1", cursor.revision,
            CursorUpdate(active_generation=UUID(int=500)))
    async with factory() as session, session.begin():
        service = DeliveryService(session, registry=registry, ordering_key=trusted_listing_chain, clock=lambda: NOW + timedelta(seconds=5))
        assert (await service.deliver_one("research_inbox.v1", identifier)).state == "applied"
        assert await session.get(Company, 1001) is not None


async def test_distinct_case_episodes_do_not_share_a_blocker_merely_by_source_id(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    from tests.next_iterations.test_event_append import seed_round
    from tests.next_iterations.test_event_resolvers import event_request, seed_case, uid

    from investment_agent.investor.common import ObjectRef
    from investment_agent.research_events import canonical
    from investment_agent.research_events.contracts import SOURCE_REF_ADAPTER, CaseControlPayload
    from investment_agent.research_events.registry import EventRegistry
    from investment_agent.research_events.resolvers import read_case_ref
    from investment_agent.storage.models.investor import CaseRevision, InvestorCase

    _, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
        case = await seed_case(session)
        next_case = case.model_copy(update={"revision": 5, "episode": 2})
        head = await session.get(InvestorCase, uid(102))
        assert head is not None
        head.revision, head.payload = 5, next_case.model_dump(mode="json")
        session.add(CaseRevision(case_id=uid(102), revision=5, payload=next_case.model_dump(mode="json")))
        ids = []
        # Raw future-publisher rows exercise delivery routing independently of
        # EV-05 admission; the ordering proof comes from real CaseRevision rows.
        for revision, episode, schema in ((4, 1, 2), (5, 2, 1)):
            body = event_request("case.control_changed")
            body["source_ref"]["revision"] = revision
            body["payload"]["case_ref"]["revision"] = revision
            body["payload"]["case_episode"] = episode
            source = SOURCE_REF_ADAPTER.validate_python(body["source_ref"])
            payload = CaseControlPayload.model_validate(body["payload"])
            identifier = canonical.event_id("local_owner", "case.control_changed", source, revision)
            ids.append(identifier)
            session.add(ResearchDomainEvent(event_id=str(identifier), owner_id="local_owner", event_type="case.control_changed",
                source_identity=canonical.source_identity(source), source_ref=canonical.normalize(source),
                source_revision=revision, schema_version=schema, occurred_at=NOW, recorded_at=NOW,
                payload_hash=canonical.payload_hash(payload), payload=canonical.normalize(payload)))
        await session.flush()

        async def published_episode(session: AsyncSession, raw: ResearchDomainEvent) -> str:
            ref = ObjectRef.model_validate(raw.source_ref)
            trusted_case = await read_case_ref(session, "local_owner", ref)
            return f"local_owner:case:{trusted_case.id}:episode:{trusted_case.episode}"

        registry = EventRegistry()
        registry.register_consumer("watchlist_sync.v1", project, event_types=("case.control_changed",))
        service = DeliveryService(session, registry=registry, ordering_key=published_episode, clock=lambda: NOW)
        assert (await service.deliver_one("watchlist_sync.v1", ids[0])).state == "quarantined"
        assert (await service.deliver_one("watchlist_sync.v1", ids[1])).state == "applied"


async def test_controlled_ordering_failure_blocks_only_its_proven_chain(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    from investment_agent.research_events.delivery import OrderingKeyResolutionError

    _, factory = db

    async def lookup(session: AsyncSession, raw: ResearchDomainEvent) -> str:
        chain = await trusted_listing_chain(session, raw)
        if raw.sequence == 1:
            raise OrderingKeyResolutionError(chain)
        return chain

    async with factory() as session, session.begin():
        failed = await seed_delivery_event(session)
        independent = await seed_delivery_event(session, 2, listing_id=19)
        same = await seed_delivery_event(session, 3)
        service = DeliveryService(session, registry=registry_for(), ordering_key=lookup, clock=lambda: NOW)
        isolated = await service.deliver_one("watchlist_sync.v1", failed)
        assert isolated.state == "quarantined" and isolated.last_error_code == "ordering_key_failed"
        assert (await service.deliver_one("watchlist_sync.v1", independent)).state == "applied"
        assert (await service.deliver_one("watchlist_sync.v1", same)).state == "blocked_predecessor"

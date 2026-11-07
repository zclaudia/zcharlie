"""Real same-session event append, using persisted sources and file SQLite."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Table, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from investment_agent.investor.foundations import InvestorError
from investment_agent.research_events.repository import EventRepository, append_event
from investment_agent.storage.database import configure_sqlite_engine
from investment_agent.storage.models.analysis import (
    PipelineRun,
    ResearchChangeSet,
    ResearchSnapshot,
)
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.investor import (
    CaseRevision,
    InvestorCase,
    UserObject,
    UserObjectRevision,
)
from investment_agent.storage.models.monitoring import ReviewCycle
from investment_agent.storage.models.publication import PublicationArtifact, PublicationEvent
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.research_events import (
    ConsumerReceipt,
    ProjectionCursor,
    ResearchDomainEvent,
)
from investment_agent.storage.models.review_panel import ReviewComparisonRecord, ReviewRoundRecord

NOW = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/next_iterations/contracts"
ROUND_PAYLOAD: dict[str, Any] = json.loads((FIXTURES / "events.valid.json").read_text())["events"][1]["payload"]
MODELS = (Market, Company, Listing, PipelineRun, ResearchSnapshot, ResearchChangeSet,
          ReviewRoundRecord, ReviewComparisonRecord, InvestorCase, CaseRevision, UserObject,
          UserObjectRevision, PublicationArtifact, PublicationEvent, ReviewCycle, SavedEvidence,
          SavedCalculation, ResearchDomainEvent, ConsumerReceipt, ProjectionCursor)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'append.db'}")
    configure_sqlite_engine(engine)
    async with engine.begin() as connection:
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[cast(Table, model.__table__) for model in MODELS]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, factory
    await engine.dispose()


async def seed_round(session: AsyncSession, *, round_id: int = 381, comparison_id: int = 452) -> None:
    if await session.get(Listing, 18) is None:
        session.add_all([
            Market(code="SSE", name="synthetic", timezone="Asia/Shanghai", currency="CNY"),
            Company(id=18, canonical_name="synthetic"),
            Listing(id=18, company_id=18, market_code="SSE", ticker="600036"),
        ])
    session.add(ReviewRoundRecord(
        id=round_id, listing_id=18, trigger="manual", trigger_ref=None, panel_id="fixture",
        panel_snapshot={}, as_of=date(2026, 11, 7), evidence_cutoff=NOW,
        base_pack={}, base_pack_hash="a" * 64, status="completed", reserved_tokens=0,
        reserved_requests=0, settled_usage={}, created_at=NOW - timedelta(minutes=1), completed_at=NOW,
    ))
    session.add(ReviewComparisonRecord(id=comparison_id, round_id=round_id, algorithm_version="v1",
                                      result_json={"notify_reasons": ["high_divergence"]}, created_at=NOW))
    await session.flush()


def request(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"owner_id": "local_owner", "event_type": "review_panel.finalized",
                             "source_ref": {"kind": "review_round", "id": 381, "revision": 1},
                             "source_revision": 1, "occurred_at": NOW,
                             "payload": json.loads(json.dumps(ROUND_PAYLOAD))}
    value.update(changes)
    return value


async def test_append_same_transaction_and_replay_keeps_original_timestamp(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        first = await EventRepository(session, clock=lambda: NOW).append_event(**request())
        second = await EventRepository(session, clock=lambda: NOW + timedelta(days=1)).append_event(**request())
        assert first == second and second.recorded_at == NOW
        await session.commit()
    async with factory() as session:
        assert len((await session.scalars(select(ResearchDomainEvent))).all()) == 1
        assert await session.get(ReviewRoundRecord, 381) is not None


async def test_caller_rollback_removes_source_and_event(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        await append_event(session, **request())
        await session.rollback()
    async with factory() as session:
        assert await session.get(ReviewRoundRecord, 381) is None
        assert await session.scalar(select(ResearchDomainEvent)) is None


async def test_conflict_does_not_rollback_outer_pending_facts(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
        original = await append_event(session, **request())
        session.add(Company(id=99, canonical_name="outer transaction fact"))
        changed = request()
        changed["payload"]["notify_reasons"] = ["stance_flip"]
        with pytest.raises(InvestorError) as failure:
            await append_event(session, **changed)
        assert failure.value.detail.code == "source_conflict"
        assert await session.get(Company, 99) is not None
        saved = await session.scalar(select(ResearchDomainEvent))
        assert saved is not None and saved.event_id == str(original.event_id)
    async with factory() as session:
        assert await session.get(Company, 99) is not None
        assert len((await session.scalars(select(ResearchDomainEvent))).all()) == 1


async def test_insert_failure_rolls_back_only_savepoint_when_caller_handles_it(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
        await session.execute(text("CREATE TRIGGER test_reject_event BEFORE INSERT ON research_domain_event BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END"))
        with pytest.raises(IntegrityError):
            await append_event(session, **request())
        assert await session.get(ReviewRoundRecord, 381) is not None
        assert await session.scalar(select(ResearchDomainEvent)) is None
        session.add(Company(id=99, canonical_name="outer still usable"))
    async with factory() as session:
        assert await session.get(Company, 99) is not None
        assert await session.get(ReviewRoundRecord, 381) is not None


async def test_propagated_append_failure_rolls_back_whole_business_transaction(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    with pytest.raises(InvestorError):
        async with factory() as session, session.begin():
            await seed_round(session)
            await append_event(session, **request(occurred_at=NOW + timedelta(seconds=1)))
    async with factory() as session:
        assert await session.scalar(select(ResearchDomainEvent)) is None
        assert await session.get(ReviewRoundRecord, 381) is None


@pytest.mark.parametrize("configured", [True, False])
async def test_savepoint_cannot_commit_independently_on_read_only_outer(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], configured: bool,
) -> None:
    engine, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
    raw_engine: AsyncEngine | None = None
    if not configured:
        raw_engine = create_async_engine(engine.url)
        factory = async_sessionmaker(raw_engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.get(ReviewRoundRecord, 381)  # No preceding DML.
            await append_event(session, **request())
            await session.rollback()
        async with factory() as session:
            assert await session.scalar(select(ResearchDomainEvent)) is None
            assert await session.get(ReviewRoundRecord, 381) is not None
    finally:
        if raw_engine is not None:
            await raw_engine.dispose()


@pytest.mark.parametrize("different_payload", [False, True])
async def test_two_file_sqlite_writers_deduplicate_or_report_conflict(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], different_payload: bool,
) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
    first_inserted = asyncio.Event()
    second_started = asyncio.Event()

    async def first_writer() -> object:
        async with factory() as session, session.begin():
            result = await append_event(session, **request())
            first_inserted.set()
            await second_started.wait()
            # A real second connection will wait for this transaction's lock.
            await asyncio.sleep(0.05)
            return result

    async def second_writer() -> object:
        await first_inserted.wait()
        second_started.set()
        async with factory() as session, session.begin():
            value = request()
            if different_payload:
                value["payload"]["backfill"] = True
                value["payload"]["manifest_hash"] = "b" * 64
            try:
                return await append_event(session, **value)
            except InvestorError as error:
                return error.detail.code

    first, second = await asyncio.wait_for(asyncio.gather(first_writer(), second_writer()), 5)
    if different_payload:
        assert second == "source_conflict"
    else:
        assert first == second
    async with factory() as session:
        assert len((await session.scalars(select(ResearchDomainEvent))).all()) == 1


async def test_append_never_calls_commit_or_rollback_on_session(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)

        async def forbidden() -> None:
            raise AssertionError("repository cannot settle the caller transaction")

        with monkeypatch.context() as patch:
            patch.setattr(session, "commit", forbidden)
            patch.setattr(session, "rollback", forbidden)
            await append_event(session, **request())
        await session.rollback()

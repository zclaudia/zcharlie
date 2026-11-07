"""BT-008 actual SQLite writers, idempotent receipts and caller-owned rollback."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Table, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.next_iterations.test_bank_topic_models import NOW
from tests.next_iterations.test_bank_topic_models import values as static_values

from investment_agent.storage.models.bank_topics import (
    TopicCase,
    TopicConditionHead,
    TopicConditionRevision,
    TopicFactRevision,
    TopicMutationReceipt,
    TopicRevision,
)
from investment_agent.storage.models.base import Base
from investment_agent.storage.repositories.bank_topics import (
    ApplyMutation,
    AuthorizeMutation,
    BankTopicMutationError,
    BankTopicReferenceError,
    BankTopicRepository,
    MutationResponse,
    StoredMutation,
)


@dataclass
class Harness:
    sessions: async_sessionmaker[AsyncSession]
    case_id: UUID
    condition_id: UUID


def access(case_id: UUID, owner: str = "local_owner") -> AuthorizeMutation:
    async def authorize(session: AsyncSession) -> None:
        if await BankTopicRepository(session).get_case(owner, case_id) is None:
            raise BankTopicReferenceError("not_found")
    return authorize


def append(case_id: UUID, expected: int = 1) -> ApplyMutation:
    async def apply(session: AsyncSession) -> MutationResponse:
        repo = BankTopicRepository(session)
        await repo.cas_case_head("local_owner", case_id, expected_revision=expected)
        row = {**static_values(TopicRevision), "case_id": str(case_id), "revision": expected + 1}
        session.add(TopicRevision(**row))
        return MutationResponse(201, {"revision": expected + 1, "value": "0.0187"})
    return apply


async def mutate(
    harness: Harness, key: UUID, *, apply: ApplyMutation | None = None,
    authorize: AuthorizeMutation | None = None, request_hash: str = "a" * 64,
    route: str = "POST /bank-topics/revisions", object_identity: str | None = None,
) -> StoredMutation:
    async with harness.sessions() as session, session.begin():
        return await BankTopicRepository(session).run_mutation(
            owner_id="local_owner", mutation_id=key, route=route,
            object_identity=object_identity or str(harness.case_id), request_hash=request_hash,
            authorize=authorize or access(harness.case_id), apply=apply or append(harness.case_id),
            case_id=harness.case_id,
        )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    from typing import cast

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mutations.sqlite'}", connect_args={"timeout": 5})
    tables = [cast(Table, model.__table__) for model in (
        TopicCase, TopicRevision, TopicFactRevision, TopicMutationReceipt,
        TopicConditionRevision, TopicConditionHead,
    )]
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    case_id, condition_id = uuid4(), uuid4()
    async with sessions() as session, session.begin():
        session.add(TopicCase(id=str(case_id), owner_id="local_owner", listing_id=18,
                              template_key="bank_nii_sustainability_v1", base_period=NOW.date(),
                              head_revision=1, control_revision=1, state="ready", created_at=NOW))
        session.add(TopicRevision(**{**static_values(TopicRevision), "case_id": str(case_id)}))
        session.add(TopicConditionHead(condition_id=str(condition_id), owner_id="local_owner", case_id=str(case_id),
                                       head_revision=1, control_revision=1, state="draft", next_due_at=None,
                                       active_run_id=None, last_check_ref=None))
    yield Harness(sessions, case_id, condition_id)
    await engine.dispose()


async def test_same_receipt_replays_before_new_write_gates_or_cas(harness: Harness) -> None:
    key = uuid4()
    first = await mutate(harness, key)
    assert first.status == 201 and not first.replayed
    first.body["revision"] = 999  # A caller cannot mutate the saved JSON receipt.
    called = 0
    async def denied_new_work(_session: AsyncSession) -> MutationResponse:
        nonlocal called
        called += 1
        raise RuntimeError("new writes disabled; stale expected_revision would also fail")
    replay = await mutate(harness, key, apply=denied_new_work)
    assert replay.replayed and replay.status == 201
    assert replay.body == {"revision": 2, "value": "0.0187"}
    assert called == 0
    async with harness.sessions() as session:
        assert (await session.get(TopicCase, str(harness.case_id))).head_revision == 2  # type: ignore[union-attr]
        assert await session.scalar(select(func.count()).select_from(TopicMutationReceipt)) == 1


async def test_current_access_is_checked_before_receipt_lookup_on_replay(harness: Harness) -> None:
    key = uuid4()
    await mutate(harness, key)
    statements: list[str] = []
    async with harness.sessions() as session:
        connection = await session.connection()
        def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", record)
        async def revoked(_session: AsyncSession) -> None:
            raise BankTopicReferenceError("not_found")
        try:
            with pytest.raises(BankTopicReferenceError):
                await BankTopicRepository(session).run_mutation(
                    owner_id="local_owner", mutation_id=key, route="POST /bank-topics/revisions",
                    object_identity=str(harness.case_id), request_hash="a" * 64,
                    authorize=revoked, apply=append(harness.case_id), case_id=harness.case_id,
                )
            assert not any("FROM topic_mutation_receipt" in statement for statement in statements)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", record)
            await session.rollback()


@pytest.mark.parametrize("change", ["hash", "route", "object"])
async def test_mutation_key_cannot_change_canonical_request_identity(harness: Harness, change: str) -> None:
    key = uuid4()
    await mutate(harness, key)
    options: dict[str, Any] = {"request_hash": "b" * 64} if change == "hash" else {"route": "POST /different"} if change == "route" else {"object_identity": "another-object"}
    with pytest.raises(BankTopicMutationError) as caught:
        await mutate(harness, key, **options)
    assert caught.value.code == "idempotency_mismatch"


async def test_callback_failure_rolls_back_revision_head_and_receipt_even_if_outer_commits(harness: Harness) -> None:
    async def broken(session: AsyncSession) -> MutationResponse:
        await append(harness.case_id)(session)
        await session.flush()
        raise RuntimeError("injected after head and revision, before receipt")
    async with harness.sessions() as session, session.begin():
        with pytest.raises(RuntimeError, match="injected"):
            await BankTopicRepository(session).run_mutation(
                owner_id="local_owner", mutation_id=uuid4(), route="append", object_identity=str(harness.case_id),
                request_hash="a" * 64, authorize=access(harness.case_id), apply=broken, case_id=harness.case_id,
            )
    async with harness.sessions() as session:
        case = await session.get(TopicCase, str(harness.case_id))
        assert case is not None and case.head_revision == 1
        assert await session.scalar(select(func.count()).select_from(TopicRevision)) == 1
        assert await session.scalar(select(func.count()).select_from(TopicMutationReceipt)) == 0


async def test_success_never_commits_outside_callers_transaction(harness: Harness) -> None:
    async with harness.sessions() as session:
        result = await BankTopicRepository(session).run_mutation(
            owner_id="local_owner", mutation_id=uuid4(), route="append", object_identity=str(harness.case_id),
            request_hash="a" * 64, authorize=access(harness.case_id), apply=append(harness.case_id), case_id=harness.case_id,
        )
        assert result.status == 201
        await session.rollback()
    async with harness.sessions() as session:
        case = await session.get(TopicCase, str(harness.case_id))
        assert case is not None and case.head_revision == 1
        assert await session.scalar(select(func.count()).select_from(TopicRevision)) == 1
        assert await session.scalar(select(func.count()).select_from(TopicMutationReceipt)) == 0


async def test_real_two_session_same_key_returns_single_head_revision_and_receipt(harness: Harness) -> None:
    key = uuid4()
    results = await asyncio.gather(mutate(harness, key), mutate(harness, key))
    assert {result.replayed for result in results} == {False, True}
    assert results[0].body == results[1].body
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TopicRevision)) == 2
        assert await session.scalar(select(func.count()).select_from(TopicMutationReceipt)) == 1


async def test_real_two_session_competing_expected_revision_has_one_cas_winner(harness: Harness) -> None:
    results = await asyncio.gather(mutate(harness, uuid4()), mutate(harness, uuid4()), return_exceptions=True)
    assert sum(isinstance(result, StoredMutation) for result in results) == 1
    errors = [result for result in results if isinstance(result, BankTopicMutationError)]
    assert len(errors) == 1 and errors[0].code == "revision_conflict"
    async with harness.sessions() as session:
        case = await session.get(TopicCase, str(harness.case_id))
        assert case is not None and case.head_revision == 2
        assert await session.scalar(select(func.count()).select_from(TopicMutationReceipt)) == 1


async def test_head_and_control_versions_are_independent_and_owner_case_guarded(harness: Harness) -> None:
    async with harness.sessions() as session, session.begin():
        repo = BankTopicRepository(session)
        await repo.cas_case_control("local_owner", harness.case_id, expected_control_revision=1, state="archived")
        with pytest.raises(BankTopicMutationError):
            await repo.cas_case_head("local_owner", harness.case_id, expected_revision=1, expected_control_revision=1)
        await repo.cas_condition_head("local_owner", harness.case_id, harness.condition_id,
                                      expected_revision=1, expected_control_revision=1)
        await repo.cas_condition_control("local_owner", harness.case_id, harness.condition_id,
                                         expected_control_revision=1, state="paused", next_due_at=None)
        with pytest.raises(BankTopicMutationError):
            await repo.cas_condition_control("other_owner", harness.case_id, harness.condition_id,
                                             expected_control_revision=2, state="cancelled", next_due_at=None)
        with pytest.raises(BankTopicMutationError):
            await repo.cas_condition_head("local_owner", uuid4(), harness.condition_id,
                                          expected_revision=2, expected_control_revision=2)
    async with harness.sessions() as session:
        case = await session.get(TopicCase, str(harness.case_id))
        head = await session.get(TopicConditionHead, str(harness.condition_id))
        assert case is not None and (case.head_revision, case.control_revision, case.state) == (1, 2, "archived")
        assert head is not None and (head.head_revision, head.control_revision, head.state) == (2, 2, "paused")


async def test_creation_uses_the_same_atomic_receipt_without_an_existing_case(harness: Harness) -> None:
    created_id, key = uuid4(), uuid4()
    authorized = 0
    async def authorize_listing(_session: AsyncSession) -> None:
        # Trusted service seam representing current access to an existing listing.
        nonlocal authorized
        authorized += 1
    async def create(session: AsyncSession) -> MutationResponse:
        session.add(TopicCase(id=str(created_id), owner_id="local_owner", listing_id=19,
                              template_key="bank_nii_sustainability_v1", base_period=NOW.date(),
                              head_revision=1, control_revision=1, state="draft", created_at=NOW))
        session.add(TopicRevision(**{**static_values(TopicRevision), "case_id": str(created_id)}))
        return MutationResponse(201, {"id": str(created_id)})
    for expected_replay in (False, True):
        async with harness.sessions() as session, session.begin():
            result = await BankTopicRepository(session).run_mutation(
                owner_id="local_owner", mutation_id=key, route="create", object_identity="listing19/FY2025",
                request_hash="a" * 64, authorize=authorize_listing, apply=create,
            )
            assert result.replayed is expected_replay
    assert authorized == 2
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TopicCase)) == 2
        assert await session.scalar(select(func.count()).select_from(TopicMutationReceipt)) == 1


async def test_pending_orm_writes_are_not_implicitly_flushed_outside_mutation_savepoint(harness: Harness) -> None:
    async with harness.sessions() as session:
        row = TopicRevision(**{**static_values(TopicRevision), "case_id": str(harness.case_id), "revision": 2})
        session.add(row)
        with pytest.raises(BankTopicMutationError) as caught:
            await BankTopicRepository(session).run_mutation(
                owner_id="local_owner", mutation_id=uuid4(), route="append", object_identity=str(harness.case_id),
                request_hash="a" * 64, authorize=access(harness.case_id), apply=append(harness.case_id),
            )
        assert caught.value.code == "invalid_reference" and row in session.new
        await session.rollback()


async def test_saved_receipt_does_not_bypass_changed_database_ownership(harness: Harness) -> None:
    key = uuid4()
    await mutate(harness, key)
    async with harness.sessions() as session, session.begin():
        case = await session.get(TopicCase, str(harness.case_id))
        assert case is not None
        case.owner_id = "foreign_test_owner"
    with pytest.raises(BankTopicReferenceError) as caught:
        await mutate(harness, key)
    assert caught.value.code == "not_found"
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TopicMutationReceipt)) == 1


async def test_receipt_identity_is_owner_scoped(harness: Harness) -> None:
    key, other_case_id = uuid4(), uuid4()
    await mutate(harness, key)
    async with harness.sessions() as session, session.begin():
        session.add(TopicCase(id=str(other_case_id), owner_id="foreign_test_owner", listing_id=19,
                              template_key="bank_nii_sustainability_v1", base_period=NOW.date(),
                              head_revision=1, control_revision=1, state="draft", created_at=NOW))
    async def foreign_response(_session: AsyncSession) -> MutationResponse:
        return MutationResponse(200, {"owner_marker": "other synthetic scope"})
    async with harness.sessions() as session, session.begin():
        result = await BankTopicRepository(session).run_mutation(
            owner_id="foreign_test_owner", mutation_id=key, route="POST /bank-topics/revisions",
            object_identity=str(other_case_id), request_hash="b" * 64,
            authorize=access(other_case_id, "foreign_test_owner"), apply=foreign_response, case_id=other_case_id,
        )
        assert not result.replayed and result.body == {"owner_marker": "other synthetic scope"}
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TopicMutationReceipt)) == 2

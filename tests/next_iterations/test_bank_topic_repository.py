"""BT-006 exact history reads and raw-SQL protection in temporary SQLite."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Table, event, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.next_iterations.test_bank_condition_models import values as condition_values
from tests.next_iterations.test_bank_topic_models import NOW
from tests.next_iterations.test_bank_topic_models import values as static_values

from investment_agent.bank_topics.contracts import TopicRef
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.bank_topics import (
    TopicCase,
    TopicCheckRevision,
    TopicConditionHead,
    TopicConditionRevision,
    TopicFactRevision,
    TopicMutationReceipt,
    TopicRevision,
)
from investment_agent.storage.models.base import Base
from investment_agent.storage.repositories.bank_topics import (
    BankTopicReferenceError,
    BankTopicRepository,
)

MODELS = (TopicCase, TopicRevision, TopicFactRevision, TopicMutationReceipt,
          TopicConditionRevision, TopicConditionHead, TopicCheckRevision)


def local_binding(fact: dict[str, Any]) -> dict[str, Any]:
    return {"stable_fact_id": fact["stable_fact_id"], "metric": fact["metric"], "source": {
        "binding_kind": "topic_fact", "fact_ref": {"id": fact["id"], "revision": fact["revision"]}}}


@dataclass
class Seeded:
    session: AsyncSession
    repo: BankTopicRepository
    case_id: UUID
    revision_ids: tuple[UUID, UUID]
    fact_ids: tuple[UUID, UUID]
    condition_id: UUID
    check_id: UUID
    foreign_case_id: UUID


@pytest.fixture
async def seeded(tmp_path: Path) -> AsyncIterator[Seeded]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repository.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[cast(Table, model.__table__) for model in MODELS]))
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        case_id, foreign_case_id = uuid4(), uuid4()
        for identity, owner in ((case_id, "local_owner"), (foreign_case_id, "foreign_test_owner")):
            session.add(TopicCase(id=str(identity), owner_id=owner, listing_id=18,
                                  template_key="bank_nii_sustainability_v1", base_period=NOW.date(),
                                  head_revision=2, control_revision=1, state="ready", created_at=NOW))
        fact1 = {**static_values(TopicFactRevision), "case_id": str(case_id), "value": "100"}
        fact2 = {**fact1, "id": str(uuid4()), "revision": 2, "value": "200"}
        snapshot_ref = {"kind": "peer_annual_snapshot", "id": str(uuid4()), "revision": 1, "field_path": "fields.nim"}
        external = {"stable_fact_id": "nim_2025", "metric": "nim", "source": {
            "binding_kind": "existing_fact", "source_ref": snapshot_ref}}
        revisions = [
            {**static_values(TopicRevision), "case_id": str(case_id), "revision": number,
             "fact_bindings": [local_binding(fact), external]}
            for number, fact in ((1, fact1), (2, fact2))
        ]
        condition = {**condition_values(TopicConditionRevision), "case_id": str(case_id),
                     "topic_revision_ref": {"kind": "topic_revision", "id": revisions[0]["id"], "revision": 1}}
        check = {**condition_values(TopicCheckRevision), "condition_id": condition["condition_id"],
                 "condition_ref": {"kind": "topic_condition", "id": condition["condition_id"], "revision": 1}}
        session.add_all([TopicFactRevision(**fact1), TopicFactRevision(**fact2),
                         *(TopicRevision(**value) for value in revisions),
                         TopicConditionRevision(**condition), TopicCheckRevision(**check),
                         TopicConditionHead(condition_id=condition["condition_id"], owner_id="local_owner",
                                            case_id=str(case_id), head_revision=1, control_revision=1,
                                            state="armed", next_due_at=None, active_run_id=None, last_check_ref=None),
                         TopicMutationReceipt(**static_values(TopicMutationReceipt))])
        await session.commit()
        yield Seeded(session, BankTopicRepository(session), case_id,
                     (UUID(revisions[0]["id"]), UUID(revisions[1]["id"])),
                     (UUID(fact1["id"]), UUID(fact2["id"])), UUID(condition["condition_id"]),
                     UUID(check["id"]), foreign_case_id)
    await engine.dispose()


def ref(kind: str, identity: UUID, revision: int = 1, field: str | None = None) -> TopicRef:
    return TopicRef.model_validate({"kind": kind, "id": identity, "revision": revision, "field_path": field})


async def test_case_business_revision_and_immutable_revision_uuid_are_distinct(seeded: Seeded) -> None:
    old = await seeded.repo.resolve_reference("local_owner", ref("topic_case", seeded.case_id, 1))
    current = await seeded.repo.resolve_reference("local_owner", ref("topic_case", seeded.case_id, 2))
    assert old.case.head_revision == 2
    assert old.revision is not None and old.revision.id == str(seeded.revision_ids[0])
    assert current.revision is not None and current.revision.id == str(seeded.revision_ids[1])
    by_uuid = await seeded.repo.resolve_reference("local_owner", ref("topic_revision", seeded.revision_ids[1]))
    assert by_uuid.revision is not None and by_uuid.revision.revision == 2
    # Shared ObjectRef integration cannot bypass immutable ref.revision=1.
    bad = ObjectRef(kind="topic_revision", id=seeded.revision_ids[1], revision=2)
    with pytest.raises(BankTopicReferenceError, match="invalid"):
        await seeded.repo.resolve_reference("local_owner", bad)


async def test_old_parent_reads_exact_fact_row_not_latest_stable_id(seeded: Seeded) -> None:
    old = await seeded.repo.resolve_reference("local_owner", ref("topic_revision", seeded.revision_ids[0], field="facts.nii_2025"))
    current = await seeded.repo.resolve_reference("local_owner", ref("topic_revision", seeded.revision_ids[1], field="facts.nii_2025"))
    assert old.fact is not None and old.fact.local_fact is not None
    assert current.fact is not None and current.fact.local_fact is not None
    assert old.fact.local_fact.value == "100" and old.fact.local_fact.revision == 1
    assert current.fact.local_fact.value == "200" and current.fact.local_fact.revision == 2
    assert old.fact.resolution == "requires_proof_resolution"


async def test_external_reference_stays_unresolved_without_claiming_verified(seeded: Seeded) -> None:
    value = await seeded.repo.resolve_reference("local_owner", ref("topic_revision", seeded.revision_ids[0], field="facts.nim_2025"))
    assert value.fact is not None and value.fact.local_fact is None
    assert value.fact.external_ref is not None and value.fact.external_ref.kind == "peer_annual_snapshot"
    assert value.fact.external_ref.field_path == "fields.nim"
    assert value.fact.resolution == "requires_proof_resolution"


@pytest.mark.parametrize("kind", ["topic_case", "topic_revision", "topic_condition", "topic_check"])
async def test_owner_scope_rejects_every_reference_kind(seeded: Seeded, kind: str) -> None:
    identity = {"topic_case": seeded.case_id, "topic_revision": seeded.revision_ids[0],
                "topic_condition": seeded.condition_id, "topic_check": seeded.check_id}[kind]
    with pytest.raises(BankTopicReferenceError) as caught:
        await seeded.repo.resolve_reference("foreign_test_owner", ref(kind, identity))
    assert caught.value.code == "not_found"
    assert str(identity) not in str(caught.value)
    assert await seeded.repo.get_case("foreign_test_owner", seeded.case_id) is None
    assert await seeded.repo.history("foreign_test_owner", seeded.case_id) == []


async def test_listing_unknown_field_and_missing_parent_are_not_resolved(seeded: Seeded) -> None:
    with pytest.raises(BankTopicReferenceError):
        await seeded.repo.resolve_reference("local_owner", ref("topic_case", seeded.case_id), listing_id=19)
    with pytest.raises(BankTopicReferenceError) as caught:
        await seeded.repo.resolve_reference("local_owner", ref("topic_revision", seeded.revision_ids[0], field="facts.missing"))
    assert caught.value.code == "invalid_reference"
    with pytest.raises(BankTopicReferenceError):
        await seeded.repo.resolve_reference("local_owner", ref("topic_case", uuid4()))


@pytest.mark.parametrize("changed", ["owner", "case", "stable_id", "metric", "revision", "missing_uuid"])
async def test_local_binding_requires_exact_row_parent_identity(seeded: Seeded, changed: str) -> None:
    fact = {**static_values(TopicFactRevision), "case_id": str(seeded.case_id)}
    binding = local_binding(fact)
    if changed == "owner":
        fact["owner_id"] = "foreign_test_owner"
    elif changed == "case":
        fact["case_id"] = str(seeded.foreign_case_id)
    elif changed == "stable_id":
        fact["stable_fact_id"] = "other_fact"
    elif changed == "metric":
        fact["metric"] = "nim"
    elif changed == "revision":
        binding["source"]["fact_ref"]["revision"] = 2
    else:
        binding["source"]["fact_ref"]["id"] = str(uuid4())
    # Avoid the already-existing business identity in this synthetic fixture.
    fact["revision"] = 3
    if changed != "revision":
        binding["source"]["fact_ref"]["revision"] = 3
    parent = {**static_values(TopicRevision), "case_id": str(seeded.case_id), "revision": 3, "fact_bindings": [binding]}
    seeded.session.add_all([TopicFactRevision(**fact), TopicRevision(**parent)])
    case = await seeded.repo.get_case("local_owner", seeded.case_id)
    assert case is not None
    case.head_revision = 3
    await seeded.session.commit()
    with pytest.raises(BankTopicReferenceError) as caught:
        await seeded.repo.resolve_reference("local_owner", ref("topic_revision", UUID(parent["id"]), field="facts.nii_2025"))
    assert caught.value.code == "not_found"


async def test_condition_and_check_follow_exact_parent_condition_revision(seeded: Seeded) -> None:
    condition = await seeded.repo.resolve_reference("local_owner", ref("topic_condition", seeded.condition_id))
    check = await seeded.repo.resolve_reference("local_owner", ref("topic_check", seeded.check_id))
    assert condition.condition is not None and condition.condition.revision == 1
    assert check.condition is not None and check.check is not None
    assert check.check.condition_revision == check.condition.revision
    assert check.case.id == str(seeded.case_id)


async def test_unpublished_future_revision_and_invalid_history_window_are_rejected(seeded: Seeded) -> None:
    future = {**static_values(TopicRevision), "case_id": str(seeded.case_id), "revision": 3}
    seeded.session.add(TopicRevision(**future))
    await seeded.session.commit()
    assert await seeded.repo.get_revision("local_owner", seeded.case_id, 3) is None
    with pytest.raises(BankTopicReferenceError):
        await seeded.repo.resolve_reference("local_owner", ref("topic_revision", UUID(future["id"])))
    assert [row.revision for row in await seeded.repo.history("local_owner", seeded.case_id)] == [1, 2]
    assert [row.revision for row in await seeded.repo.history("local_owner", seeded.case_id, after_revision=1, limit=1)] == [2]
    for limit in (0, True, 101):
        with pytest.raises(BankTopicReferenceError):
            await seeded.repo.history("local_owner", seeded.case_id, limit=limit)


@pytest.mark.parametrize("table", ["topic_revision", "topic_fact_revision", "topic_condition_revision", "topic_check_revision", "topic_mutation_receipt"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
async def test_direct_sql_cannot_change_history(seeded: Seeded, table: str, operation: str) -> None:
    statement = f"DELETE FROM {table}" if operation == "DELETE" else f"UPDATE {table} SET owner_id='changed'" if table != "topic_revision" else "UPDATE topic_revision SET actor='changed'"
    with pytest.raises(IntegrityError, match="immutable"):
        await seeded.session.execute(text(statement))
    await seeded.session.rollback()


async def test_repository_reads_execute_no_mutations(seeded: Seeded) -> None:
    statements: list[str] = []
    def record(_connection: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)
    assert seeded.session.bind is not None
    engine = seeded.session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        await seeded.repo.get_case("local_owner", seeded.case_id)
        await seeded.repo.get_revision("local_owner", seeded.case_id, 1)
        await seeded.repo.history("local_owner", seeded.case_id)
        await seeded.repo.resolve_reference("local_owner", ref("topic_check", seeded.check_id))
        await seeded.repo.resolve_reference("local_owner", ref("topic_revision", seeded.revision_ids[0], field="facts.nii_2025"))
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements
    assert all(not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)


@pytest.mark.parametrize("model", [TopicRevision, TopicFactRevision, TopicMutationReceipt, TopicConditionRevision, TopicCheckRevision])
async def test_hidden_rowid_cannot_replace_history_with_all_new_declared_keys(seeded: Seeded, model: type[Base]) -> None:
    table = cast(Table, model.__table__)
    name = table.name
    definition = (await seeded.session.execute(text("SELECT sql FROM sqlite_master WHERE name=:name"), {"name": name})).scalar_one()
    assert "WITHOUT ROWID" in definition.upper()
    before = list((await seeded.session.execute(text(f'SELECT * FROM "{name}"'))).mappings())
    assert before
    substitutions: dict[str, Any] = {
        "id": str(uuid4()), "owner_id": "replacement_test_owner", "case_id": str(uuid4()),
        "condition_id": str(uuid4()), "mutation_id": str(uuid4()), "stable_fact_id": "replacement_fact",
        "revision": 99, "condition_revision": 99, "semantic_input_hash": "f" * 64,
        "evaluator_version": "replacement/99",
    }
    parameters = {key: value for key, value in substitutions.items() if key in table.c}
    columns = ",".join(f'"{column.name}"' for column in table.columns)
    selections = ",".join(f":{column.name}" if column.name in parameters else f'"{column.name}"' for column in table.columns)
    # The vulnerable form would reuse hidden rowid=1 while replacing every PK
    # and business unique identity. WITHOUT ROWID rejects it before any deletion.
    statement = f'INSERT OR REPLACE INTO "{name}" (rowid,{columns}) SELECT 1,{selections} FROM "{name}" LIMIT 1'
    with pytest.raises(OperationalError, match="rowid"):
        await seeded.session.execute(text(statement), parameters)
    await seeded.session.rollback()
    after = list((await seeded.session.execute(text(f'SELECT * FROM "{name}"'))).mappings())
    assert after == before


async def test_cross_case_condition_parent_revision_is_rejected(seeded: Seeded) -> None:
    other_case_id = uuid4()
    other_case = TopicCase(id=str(other_case_id), owner_id="local_owner", listing_id=19,
                           template_key="bank_nii_sustainability_v1", base_period=NOW.date(),
                           head_revision=1, control_revision=1, state="ready", created_at=NOW)
    foreign_parent = {**static_values(TopicRevision), "case_id": str(other_case_id)}
    invalid = {**condition_values(TopicConditionRevision), "case_id": str(seeded.case_id),
               "topic_revision_ref": {"kind": "topic_revision", "id": foreign_parent["id"], "revision": 1}}
    seeded.session.add_all([other_case, TopicRevision(**foreign_parent), TopicConditionRevision(**invalid),
        TopicConditionHead(condition_id=invalid["condition_id"], owner_id="local_owner", case_id=str(seeded.case_id),
                           head_revision=1, control_revision=1, state="paused", next_due_at=None, active_run_id=None,
                           last_check_ref=None)])
    await seeded.session.commit()
    with pytest.raises(BankTopicReferenceError):
        await seeded.repo.resolve_reference("local_owner", ref("topic_condition", UUID(invalid["condition_id"])))


async def test_reads_do_not_autoflush_unrelated_pending_writes(seeded: Seeded) -> None:
    pending = TopicMutationReceipt(**static_values(TopicMutationReceipt))
    seeded.session.add(pending)
    await seeded.repo.history("local_owner", seeded.case_id)
    assert pending in seeded.session.new


@pytest.mark.parametrize("malformed_id", [False, True])
async def test_check_cannot_substitute_or_malform_its_condition_identity(seeded: Seeded, malformed_id: bool) -> None:
    invalid = {**condition_values(TopicCheckRevision), "condition_id": "invalid-uuid" if malformed_id else str(seeded.condition_id),
               "semantic_input_hash": "c" * 64,
               "condition_ref": {"kind": "topic_condition", "id": str(uuid4()), "revision": 1}}
    seeded.session.add(TopicCheckRevision(**invalid))
    await seeded.session.commit()
    with pytest.raises(BankTopicReferenceError) as caught:
        await seeded.repo.resolve_reference("local_owner", ref("topic_check", UUID(invalid["id"])))
    assert caught.value.code == "invalid_reference"


@pytest.mark.parametrize("broken", ["missing_head", "other_owner", "other_case", "future_revision"])
async def test_condition_history_requires_published_same_owner_case_head(seeded: Seeded, broken: str) -> None:
    value = {**condition_values(TopicConditionRevision), "case_id": str(seeded.case_id),
             "topic_revision_ref": {"kind": "topic_revision", "id": str(seeded.revision_ids[0]), "revision": 1}}
    if broken == "future_revision":
        value["revision"] = 2
    seeded.session.add(TopicConditionRevision(**value))
    if broken != "missing_head":
        seeded.session.add(TopicConditionHead(
            condition_id=value["condition_id"], owner_id="foreign_test_owner" if broken == "other_owner" else "local_owner",
            case_id=str(seeded.foreign_case_id) if broken == "other_case" else str(seeded.case_id),
            head_revision=1, control_revision=1, state="cancelled", next_due_at=None,
            active_run_id=None, last_check_ref=None))
    await seeded.session.commit()
    with pytest.raises(BankTopicReferenceError) as caught:
        await seeded.repo.resolve_reference("local_owner", ref("topic_condition", UUID(value["condition_id"]), value["revision"]))
    assert caught.value.code == "not_found"


@pytest.mark.parametrize("state", ["paused", "cancelled"])
async def test_inactive_condition_head_keeps_authorized_history_readable(seeded: Seeded, state: str) -> None:
    head = await seeded.session.get(TopicConditionHead, str(seeded.condition_id))
    assert head is not None
    head.state = state
    await seeded.session.commit()
    value = await seeded.repo.resolve_reference("local_owner", ref("topic_condition", seeded.condition_id))
    assert value.condition is not None and value.condition.revision == 1

"""BT-004: conditions retain content history independently of mutable control."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, Table, create_engine, delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from investment_agent.storage.models.bank_topics import (
    BANK_TOPIC_IMMUTABLE_DDL,
    TopicCase,
    TopicCheckRevision,
    TopicConditionHead,
    TopicConditionRevision,
)
from investment_agent.storage.models.base import Base

MODELS = (TopicCase, TopicConditionRevision, TopicConditionHead, TopicCheckRevision)
HISTORY = (TopicConditionRevision, TopicCheckRevision)
NOW = datetime(2027, 4, 30, 16, tzinfo=UTC)


def values(model: type[Base]) -> dict[str, Any]:
    condition_id = str(uuid4())
    condition_ref = {"kind": "topic_condition", "id": condition_id, "revision": 1}
    if model is TopicConditionRevision:
        return {"condition_id": condition_id, "revision": 1, "schema_version": 1,
                "owner_id": "local_owner", "case_id": str(uuid4()),
                "topic_revision_ref": {"kind": "topic_revision", "id": str(uuid4()), "revision": 1},
                "dsl_version": "bank-condition/1", "predicate": {"kind": "all", "conditions": [
                    {"metric": "nii_yoy", "operator": "gt", "threshold": "0", "unit": "fraction"}]},
                "dependency_manifest": [], "target_selector": "next_comparable_annual",
                "review_due_at": NOW, "expires_at": NOW + timedelta(days=60), "timezone": "Asia/Shanghai",
                "activation_basis": None, "content_hash": "a" * 64, "created_at": NOW, "actor": "test-user"}
    if model is TopicCheckRevision:
        return {"id": str(uuid4()), "schema_version": 1, "owner_id": "local_owner",
                "condition_id": condition_id, "condition_revision": 1, "condition_ref": condition_ref,
                "evaluator_version": "bank-condition/1", "execution_manifest": {"as_of": NOW.isoformat(), "facts": []},
                "execution_manifest_hash": "b" * 64, "semantic_input_hash": "a" * 64,
                "as_of": NOW, "trigger": "due", "result": {"outcome": "insufficient_evidence", "coverage": "partial"},
                "unknown_reasons": [{"metric": "nii_yoy", "code": "no_new_comparable_report"}],
                "prior_check_ref": None, "supersedes_ref": None, "created_at": NOW}
    raise AssertionError(model)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(f"sqlite:///{tmp_path / 'bank-conditions.sqlite'}")
    Base.metadata.create_all(database, tables=[cast(Table, model.__table__) for model in MODELS])
    yield database
    database.dispose()


def test_condition_schema_exposes_due_index_and_exact_semantic_identity(engine: Engine) -> None:
    schema = inspect(engine)
    assert schema.get_pk_constraint("topic_condition_revision")["constrained_columns"] == ["condition_id", "revision"]
    assert ["owner_id", "state", "next_due_at"] in [index["column_names"] for index in schema.get_indexes("topic_condition_head")]
    assert ["owner_id", "condition_id", "condition_revision", "semantic_input_hash", "evaluator_version"] in [
        key["column_names"] for key in schema.get_unique_constraints("topic_check_revision")
    ]
    assert {"execution_manifest", "execution_manifest_hash", "semantic_input_hash", "evaluator_version", "condition_ref"} <= {
        column["name"] for column in schema.get_columns("topic_check_revision")
    }
    assert len(BANK_TOPIC_IMMUTABLE_DDL) == 15


@pytest.mark.parametrize("model", HISTORY, ids=lambda model: model.__tablename__)
@pytest.mark.parametrize("operation", ["orm_update", "orm_delete", "bulk_update", "bulk_delete", "replace_pk", "replace_unique"])
def test_condition_history_cannot_be_overwritten(engine: Engine, model: type[Base], operation: str) -> None:
    original = values(model)
    hash_column = "content_hash" if model is TopicConditionRevision else "execution_manifest_hash"
    table = cast(Table, model.__table__)
    with Session(engine) as session:
        session.add(model(**original))
        session.commit()
        with pytest.raises((ValueError, IntegrityError), match="immutable"):
            if operation.startswith("orm_"):
                row = session.scalars(select(model)).one()
                if operation == "orm_update":
                    setattr(row, hash_column, "c" * 64)
                else:
                    session.delete(row)
                session.commit()
            elif operation == "bulk_update":
                session.execute(update(model).values({hash_column: "c" * 64}))
            elif operation == "bulk_delete":
                session.execute(delete(model))
            else:
                replacement = {**original, hash_column: "c" * 64}
                if operation == "replace_unique" and model is TopicCheckRevision:
                    replacement["id"] = str(uuid4())
                session.execute(table.insert().prefix_with("OR REPLACE").values(**replacement))
        session.rollback()
        assert getattr(session.scalars(select(model)).one(), hash_column) == original[hash_column]


def test_semantic_key_deduplicates_scan_clock_but_separates_due_phase_and_evaluator(engine: Engine) -> None:
    original = values(TopicCheckRevision)
    with Session(engine) as session:
        session.add(TopicCheckRevision(**original))
        session.commit()
        # Later execution time / different execution hash alone cannot create a new check.
        repeat = {**original, "id": str(uuid4()), "as_of": NOW + timedelta(days=1), "execution_manifest_hash": "c" * 64}
        session.add(TopicCheckRevision(**repeat))
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()
        session.add_all([
            TopicCheckRevision(**{**repeat, "semantic_input_hash": "d" * 64}),
            TopicCheckRevision(**{**repeat, "id": str(uuid4()), "evaluator_version": "bank-condition/2"}),
            TopicCheckRevision(**{**repeat, "id": str(uuid4()), "condition_revision": 2}),
            TopicCheckRevision(**{**repeat, "id": str(uuid4()), "owner_id": "other_test_owner"}),
        ])
        session.commit()
        assert len(session.scalars(select(TopicCheckRevision)).all()) == 5


def test_control_head_changes_do_not_mutate_condition_content(engine: Engine) -> None:
    original = values(TopicConditionRevision)
    with Session(engine) as session:
        session.add(TopicConditionRevision(**original))
        head = TopicConditionHead(condition_id=original["condition_id"], owner_id="local_owner", case_id=original["case_id"],
                                  head_revision=1, control_revision=1, state="armed", next_due_at=NOW,
                                  active_run_id=None, last_check_ref=None)
        session.add(head)
        session.commit()
        head.control_revision = 2
        head.state = "paused"
        head.next_due_at = None
        session.commit()
        assert head.head_revision == 1 and head.control_revision == 2
        assert session.scalars(select(TopicConditionRevision)).one().content_hash == "a" * 64
        head.control_revision = 0
        with pytest.raises(IntegrityError, match="control_revision"):
            session.commit()
        session.rollback()


def test_condition_versions_append_and_window_constraints_apply(engine: Engine) -> None:
    original = values(TopicConditionRevision)
    with Session(engine) as session:
        session.add_all([TopicConditionRevision(**original), TopicConditionRevision(**{**original, "revision": 2, "content_hash": "c" * 64})])
        session.commit()
        invalid = {**original, "condition_id": str(uuid4()), "expires_at": original["review_due_at"]}
        session.add(TopicConditionRevision(**invalid))
        with pytest.raises(IntegrityError, match="window"):
            session.commit()
        session.rollback()
        assert len(session.scalars(select(TopicConditionRevision)).all()) == 2

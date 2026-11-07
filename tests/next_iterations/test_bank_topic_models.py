"""BT-003: real temporary SQLite history protection, without an application DB."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, Table, create_engine, delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from investment_agent.storage.models.bank_topics import (
    TopicCase,
    TopicFactRevision,
    TopicMutationReceipt,
    TopicRevision,
)
from investment_agent.storage.models.base import Base

STATIC_MODELS = (TopicCase, TopicRevision, TopicFactRevision, TopicMutationReceipt)
HISTORY = (TopicRevision, TopicFactRevision, TopicMutationReceipt)
NOW = datetime(2026, 11, 7, tzinfo=UTC)
PERIOD = {"start": "2025-01-01", "end": "2025-12-31", "basis": "period_to_date/annual"}


def values(model: type[Base]) -> dict[str, Any]:
    common = {"id": str(uuid4()), "case_id": str(uuid4()), "revision": 1,
              "created_at": NOW, "content_hash": "a" * 64}
    if model is TopicRevision:
        return {**common, "schema_version": 1, "question": "净息差下降时净利息收入为何仍增长？",
                "period": PERIOD, "baseline_period": {**PERIOD, "start": "2024-01-01", "end": "2024-12-31"},
                "accounting_standard": "CAS", "reporting_scope": "consolidated", "currency": "CNY",
                "as_of": NOW, "fact_bindings": [], "decomposition": None, "explanations": [],
                "condition_refs": [], "supersedes_ref": None, "actor": "test-reviewer"}
    if model is TopicFactRevision:
        return {**common, "owner_id": "local_owner", "schema_version": 1, "stable_fact_id": "nii_2025",
                "metric": "nii", "value": "215593000000", "unit": "CNY", "raw_value": "215,593",
                "raw_unit": "CNY_million", "period": PERIOD, "comparison_period": None,
                "accounting_standard": "CAS", "reporting_scope": "consolidated", "currency": "CNY",
                "source_refs": [], "proof": None, "verification": "candidate", "reason": None,
                "published_at": None, "published_date": date(2026, 3, 28), "published_precision": "date",
                "acquired_at": NOW, "checked_at": None, "review_actor": None}
    if model is TopicMutationReceipt:
        return {"owner_id": "local_owner", "mutation_id": str(uuid4()), "route": "POST /bank-topics",
                "object_identity": "600036.SH/FY2025", "request_hash": "a" * 64,
                "response": {"schema_version": 1, "state": "draft"}, "status": 201, "created_at": NOW}
    raise AssertionError(model)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    database = create_engine(f"sqlite:///{tmp_path / 'bank-models.sqlite'}")
    Base.metadata.create_all(database, tables=[cast(Table, model.__table__) for model in STATIC_MODELS])
    yield database
    database.dispose()


def test_static_schema_columns_indices_and_foreign_key_limit(engine: Engine) -> None:
    schema = inspect(engine)
    assert {"base_period", "owner_id", "listing_id", "head_revision", "control_revision"} <= {
        column["name"] for column in schema.get_columns("topic_case")
    }
    assert {"case_id", "stable_fact_id", "revision", "value", "published_date", "published_precision"} <= {
        column["name"] for column in schema.get_columns("topic_fact_revision")
    }
    assert ["owner_id", "listing_id", "template_key", "base_period"] in [
        key["column_names"] for key in schema.get_unique_constraints("topic_case")
    ]
    assert {"ix_topic_case_owner_state"} <= {index["name"] for index in schema.get_indexes("topic_case")}
    assert {"ix_topic_revision_case_created"} <= {index["name"] for index in schema.get_indexes("topic_revision")}
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
    # The database alone does not authorize this absent parent; service must reject it.
    with Session(engine) as session:
        session.add(TopicRevision(**values(TopicRevision)))
        session.commit()


@pytest.mark.parametrize("model", HISTORY, ids=lambda model: model.__tablename__)
@pytest.mark.parametrize("operation", ["orm_update", "orm_delete", "bulk_update", "bulk_delete", "replace_pk", "replace_unique"])
def test_static_history_rejects_orm_bulk_and_replace(
    engine: Engine, model: type[Base], operation: str,
) -> None:
    original = values(model)
    table: Table = model.__table__  # type: ignore[assignment]
    hash_column = "request_hash" if model is TopicMutationReceipt else "content_hash"
    with Session(engine) as session:
        session.add(model(**original))
        session.commit()
        with pytest.raises((ValueError, IntegrityError), match="immutable"):
            if operation.startswith("orm_"):
                row = session.scalars(select(model)).one()
                if operation == "orm_update":
                    setattr(row, hash_column, "b" * 64)
                else:
                    session.delete(row)
                session.commit()
            elif operation == "bulk_update":
                session.execute(update(model).values({hash_column: "b" * 64}))
            elif operation == "bulk_delete":
                session.execute(delete(model))
            else:
                replacement = {**original, hash_column: "b" * 64}
                if operation == "replace_unique" and "id" in replacement:
                    replacement["id"] = str(uuid4())
                session.execute(table.insert().prefix_with("OR REPLACE").values(**replacement))
        session.rollback()
        row = session.scalars(select(model)).one()
        assert getattr(row, hash_column) == "a" * 64


def test_case_head_is_mutable_but_identity_is_unique_and_revisions_positive(engine: Engine) -> None:
    data = {"id": str(uuid4()), "owner_id": "local_owner", "listing_id": 18,
            "template_key": "bank_nii_sustainability_v1", "base_period": date(2025, 12, 31),
            "head_revision": 1, "control_revision": 1, "state": "draft", "created_at": NOW}
    with Session(engine) as session:
        case = TopicCase(**data)
        session.add(case)
        session.commit()
        case.head_revision = 2
        case.control_revision = 3
        case.state = "archived"
        session.commit()
        assert case.head_revision == 2 and case.control_revision == 3
        session.add(TopicCase(**{**data, "id": str(uuid4())}))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        case.head_revision = 0
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_receipt_key_is_owner_scoped_and_fact_null_is_not_zero(engine: Engine) -> None:
    receipt = values(TopicMutationReceipt)
    with Session(engine) as session:
        session.add_all([TopicMutationReceipt(**receipt), TopicMutationReceipt(**{**receipt, "owner_id": "other_test_owner"})])
        fact = values(TopicFactRevision)
        fact["value"] = "0"
        session.add(TopicFactRevision(**fact))
        session.commit()
        assert session.scalars(select(TopicFactRevision)).one().value == "0"
        missing = {**fact, "id": str(uuid4()), "revision": 2, "value": None}
        session.add(TopicFactRevision(**missing))
        with pytest.raises(IntegrityError, match="unknown_reason"):
            session.commit()
        session.rollback()
        session.add(TopicFactRevision(**{**missing, "reason": "no_disclosure"}))
        session.commit()
        assert len(session.scalars(select(TopicFactRevision)).all()) == 2

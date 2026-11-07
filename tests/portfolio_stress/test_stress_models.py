"""PT-003 temporary SQLite checks for input/result and operation immutability."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine, Table, create_engine, delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from investment_agent.storage.models import Base
from investment_agent.storage.models.portfolio_stress import (
    PortfolioStressHead,
    PortfolioStressRevision,
    PortfolioStudyOperation,
)

NOW = datetime(2026, 11, 7, tzinfo=UTC)
STUDY, ACCOUNT, RESULT, OPERATION = (str(UUID(int=value)) for value in range(1, 5))
TABLES = [cast(Table, cls.__table__) for cls in
          (PortfolioStressHead, PortfolioStressRevision, PortfolioStudyOperation)]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'stress-models.db'}")
    Base.metadata.create_all(engine, tables=TABLES)
    yield engine
    engine.dispose()


def history() -> dict[str, Any]:
    return {"stress_id": STUDY, "revision": 1, "owner_id": "local_owner", "account_id": ACCOUNT,
            "input_hash": "a" * 64, "result_id": RESULT, "requested_at": NOW,
            "payload": {"frozen_inputs": {"marker": "original"}}}


def operation() -> dict[str, Any]:
    return {"owner_id": "local_owner", "operation_id": OPERATION, "action": "create_stress",
            "request_hash": "b" * 64, "response_payload": {"result_id": RESULT}, "created_at": NOW}


def test_control_and_configuration_versions_are_separate_and_history_remains(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(PortfolioStressHead(id=STUDY, owner_id="local_owner", account_id=ACCOUNT,
                                       enabled=True, created_at=NOW))
        session.add(PortfolioStressRevision(**history()))
    with Session(engine) as session, session.begin():
        result = session.execute(update(PortfolioStressHead).where(
            PortfolioStressHead.id == STUDY, PortfolioStressHead.control_revision == 1,
        ).values(control_revision=2, permission_epoch=2, enabled=False))
        assert result.rowcount == 1  # type: ignore[attr-defined]
    with Session(engine) as session:
        head = session.get(PortfolioStressHead, STUDY)
        saved = session.get(PortfolioStressRevision, (STUDY, 1))
        assert head is not None and saved is not None
        assert (head.revision, head.control_revision, head.permission_epoch, head.enabled) == (1, 2, 2, False)
        assert saved.result_id == RESULT and saved.payload == history()["payload"]
        stale = session.execute(update(PortfolioStressHead).where(
            PortfolioStressHead.id == STUDY, PortfolioStressHead.control_revision == 1,
        ).values(enabled=True))
        assert stale.rowcount == 0  # type: ignore[attr-defined]
    inspector = inspect(engine)
    assert ["owner_id", "result_id"] in [item["column_names"] for item in inspector.get_unique_constraints("portfolio_stress_revision")]
    assert inspector.get_pk_constraint("portfolio_study_operation")["constrained_columns"] == ["owner_id", "operation_id"]


@pytest.mark.parametrize("target", ("history", "operation"))
@pytest.mark.parametrize("mutation", ("orm_update", "orm_delete", "bulk_update", "bulk_delete", "replace"))
def test_history_and_original_receipt_cannot_be_changed(engine: Engine, target: str, mutation: str) -> None:
    model = PortfolioStressRevision if target == "history" else PortfolioStudyOperation
    values = history() if target == "history" else operation()
    with Session(engine) as session, session.begin():
        session.add(model(**values))
    with Session(engine) as session:
        with pytest.raises((ValueError, IntegrityError)):
            if mutation.startswith("orm"):
                row = session.scalar(select(model))
                assert row is not None
                if mutation == "orm_delete":
                    session.delete(row)
                elif isinstance(row, PortfolioStressRevision):
                    row.result_id = str(UUID(int=99))
                else:
                    assert isinstance(row, PortfolioStudyOperation)
                    row.request_hash = "c" * 64
            elif mutation == "bulk_update":
                session.execute(update(model).values(owner_id="changed"))
            elif mutation == "bulk_delete":
                session.execute(delete(model))
            else:
                session.execute(cast(Table, model.__table__).insert().prefix_with("OR REPLACE").values(**values))
            session.flush()
        session.rollback()
    with Session(engine) as session:
        saved = session.scalar(select(model))
        assert isinstance(saved, (PortfolioStressRevision, PortfolioStudyOperation))
        assert saved.owner_id == "local_owner"
        if isinstance(saved, PortfolioStressRevision):
            assert saved.result_id == RESULT
        else:
            assert isinstance(saved, PortfolioStudyOperation)
            assert saved.request_hash == "b" * 64


def test_result_id_cannot_be_rebound_to_another_study_by_replace(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(PortfolioStressRevision(**history()))
    values = {**history(), "stress_id": str(UUID(int=99)), "input_hash": "b" * 64}
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.execute(cast(Table, PortfolioStressRevision.__table__).insert().prefix_with("OR REPLACE").values(**values))
    with Session(engine) as session:
        original = session.get(PortfolioStressRevision, (STUDY, 1))
        assert original is not None and original.input_hash == "a" * 64


@pytest.mark.parametrize("target", ("history", "operation"))
def test_hidden_rowid_cannot_replace_an_unrelated_history_identity(engine: Engine, target: str) -> None:
    model = PortfolioStressRevision if target == "history" else PortfolioStudyOperation
    values = history() if target == "history" else operation()
    with Session(engine) as session, session.begin():
        session.add(model(**values))
    sql = (
        "INSERT OR REPLACE INTO portfolio_stress_revision "
        "(rowid,stress_id,revision,owner_id,account_id,input_hash,result_id,requested_at,payload) "
        "SELECT 1,:new_id,revision,owner_id,account_id,input_hash,:new_id,requested_at,payload "
        "FROM portfolio_stress_revision"
        if target == "history" else
        "INSERT OR REPLACE INTO portfolio_study_operation "
        "(rowid,owner_id,operation_id,action,request_hash,response_payload,created_at) "
        "SELECT 1,owner_id,:new_id,action,request_hash,response_payload,created_at "
        "FROM portfolio_study_operation"
    )
    with Session(engine) as session, pytest.raises(OperationalError, match="rowid"):
        session.execute(text(sql), {"new_id": str(UUID(int=99))})
    with Session(engine) as session:
        saved = session.scalar(select(model))
        assert saved is not None
        if isinstance(saved, PortfolioStressRevision):
            assert (saved.stress_id, saved.result_id) == (STUDY, RESULT)
        else:
            assert isinstance(saved, PortfolioStudyOperation)
            assert saved.operation_id == OPERATION


def test_revision_and_idempotency_receipt_rollback_together(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(PortfolioStressRevision(**history()))
        session.add(PortfolioStudyOperation(**operation()))
        session.flush()
        session.rollback()
    with Session(engine) as session:
        assert session.scalar(select(PortfolioStressRevision)) is None
        assert session.scalar(select(PortfolioStudyOperation)) is None


@pytest.mark.parametrize("field", ("revision", "control_revision", "permission_epoch"))
def test_zero_control_versions_are_rejected(engine: Engine, field: str) -> None:
    values = {"id": STUDY, "owner_id": "local_owner", "account_id": ACCOUNT,
              "enabled": True, "created_at": NOW, field: 0}
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(PortfolioStressHead(**values))
        session.flush()

"""EV-02 actual temporary SQLite constraints and immutable history protection."""

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine, Table, create_engine, delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from investment_agent.research_events import canonical
from investment_agent.research_events.contracts import EventEnvelope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.research_events import (
    ConsumerReceipt,
    ProjectionCursor,
    ResearchDomainEvent,
)

TABLES = [cast(Table, model.__table__) for model in (ResearchDomainEvent, ConsumerReceipt, ProjectionCursor)]
EVENT_TABLE = cast(Table, ResearchDomainEvent.__table__)
NOW = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine, tables=TABLES)
    yield engine
    engine.dispose()


def event_values() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "fixtures/next_iterations/contracts/events.valid.json"
    wire = json.loads(path.read_text())["events"][1]
    event = EventEnvelope.model_validate(wire)
    return {**wire, "occurred_at": event.occurred_at, "recorded_at": event.recorded_at,
            "source_identity": canonical.source_identity(event.source_ref)}


def receipt_values(consumer: str = "research_inbox.v1") -> dict[str, Any]:
    return {"consumer": consumer, "event_id": event_values()["event_id"], "state": "retry",
            "ordering_key": "local_owner:round:381", "projector_version": "inbox/1",
            "effect_refs": []}


def cursor_values(consumer: str = "research_inbox.v1") -> dict[str, Any]:
    return {"consumer": consumer, "updated_at": NOW}


def test_schema_has_required_uniqueness_indexes_and_no_extra_business_table(engine: Engine) -> None:
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {"research_domain_event", "consumer_receipt", "projection_cursor"}
    assert {"ix_research_domain_event_owner_sequence", "ix_research_domain_event_type_sequence"} <= {i["name"] for i in inspector.get_indexes("research_domain_event")}
    assert {"ix_consumer_receipt_retry", "ix_consumer_receipt_ordering"} <= {i["name"] for i in inspector.get_indexes("consumer_receipt")}
    assert ["owner_id", "event_type", "source_identity", "source_revision"] in [u["column_names"] for u in inspector.get_unique_constraints("research_domain_event")]
    assert inspector.get_pk_constraint("consumer_receipt")["constrained_columns"] == ["consumer", "event_id"]
    assert {"effect_manifest", "effect_manifest_hash", "effect_cursor"} <= {c["name"] for c in inspector.get_columns("consumer_receipt")}
    assert {"active_generation", "rebuild_state"} <= {c["name"] for c in inspector.get_columns("projection_cursor")}


def test_event_and_receipt_rollback_together(engine: Engine) -> None:
    with Session(engine) as session:
        session.add(ResearchDomainEvent(**event_values()))
        session.add(ConsumerReceipt(**receipt_values()))
        session.flush()
        session.rollback()
    with Session(engine) as session:
        assert session.scalar(select(ResearchDomainEvent)) is None
        assert session.scalar(select(ConsumerReceipt)) is None


def test_all_registered_consumer_names_and_future_scopes_fit_schema(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(ResearchDomainEvent(**event_values()))
        for name in ["research_inbox.v1", "watchlist_sync.v1", "review_admission.v1", "quality_source_validity.v1"]:
            session.add(ConsumerReceipt(**receipt_values(name)))
            session.add(ProjectionCursor(**cursor_values(name)))
    with Session(engine) as session:
        assert len(session.scalars(select(ConsumerReceipt)).all()) == 4
        for row in session.scalars(select(ProjectionCursor)):
            assert row.active_generation is None and row.rebuild_state is None


def test_receipt_same_consumer_event_unique_but_delivery_state_mutable(engine: Engine) -> None:
    values = receipt_values()
    with Session(engine) as session, session.begin():
        session.add(ResearchDomainEvent(**event_values()))
        session.add(ConsumerReceipt(**values))
    with Session(engine) as session, session.begin():
        row = session.get(ConsumerReceipt, (values["consumer"], values["event_id"]))
        assert row is not None
        row.state, row.attempts, row.processed_at = "applied", 1, NOW
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(ConsumerReceipt(**values))
        session.flush()


@pytest.mark.parametrize("mutation", ["orm_update", "orm_delete", "bulk_update", "bulk_delete", "replace_id", "replace_source", "replace_sequence"])
def test_immutable_history_cannot_be_modified_or_replaced(engine: Engine, mutation: str) -> None:
    values = event_values()
    with Session(engine) as session, session.begin():
        session.add(ResearchDomainEvent(**values))
    with Session(engine) as session:
        with pytest.raises((IntegrityError, ValueError)):
            if mutation.startswith("orm_"):
                row = session.scalar(select(ResearchDomainEvent))
                assert row is not None
                if mutation == "orm_update":
                    row.payload_hash = "b" * 64
                else:
                    session.delete(row)
            elif mutation == "bulk_update":
                session.execute(update(ResearchDomainEvent).values(payload_hash="b" * 64))
            elif mutation == "bulk_delete":
                session.execute(delete(ResearchDomainEvent))
            else:
                replace = copy.deepcopy(values)
                if mutation in {"replace_source", "replace_sequence"}:
                    replace["event_id"] = str(UUID(int=999))
                if mutation == "replace_sequence":
                    replace["sequence"] = 1
                    replace["source_identity"] = canonical.canonical_json({"kind": "review_round", "id": 999})
                session.execute(EVENT_TABLE.insert().prefix_with("OR REPLACE").values(**replace))
            session.flush()
        session.rollback()
    with Session(engine) as session:
        saved = session.scalar(select(ResearchDomainEvent))
        assert saved is not None and saved.payload_hash == values["payload_hash"]
        assert saved.sequence == 1


@pytest.mark.parametrize("changes", [{"sequence": -1}, {"sequence": 0}, {"source_revision": 0}, {"schema_version": 0}, {"payload_hash": "bad"}, {"payload": []}, {"source_ref": []}])
def test_invalid_event_shape_constraints(engine: Engine, changes: dict[str, Any]) -> None:
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(ResearchDomainEvent(**{**event_values(), **changes}))
        session.flush()


@pytest.mark.parametrize("changes", [{"state": "success"}, {"attempts": -1}, {"effect_cursor": 0}, {"effect_manifest": {}}, {"effect_manifest_hash": "a" * 64}, {"effect_cursor": -1}])
def test_invalid_receipt_progress_constraints(engine: Engine, changes: dict[str, Any]) -> None:
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(ConsumerReceipt(**{**receipt_values(), **changes}))
        session.flush()


@pytest.mark.parametrize("changes", [{"revision": 0}, {"scanned_sequence": -1}, {"retry_count": -1}, {"quarantined_count": -1}, {"consumer": "watchlist_sync.v1", "active_generation": str(UUID(int=2))}, {"consumer": "review_admission.v1", "rebuild_state": {}}, {"rebuild_state": {}}, {"active_generation": str(UUID(int=1)), "rebuild_state": []}])
def test_cursor_numeric_and_generation_scope_constraints(engine: Engine, changes: dict[str, Any]) -> None:
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(ProjectionCursor(**{**cursor_values(), **changes}))
        session.flush()


def test_manifest_progress_and_generation_head_are_persisted(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(ConsumerReceipt(**{**receipt_values(), "effect_manifest": {"generation": str(UUID(int=1)), "targets": []}, "effect_manifest_hash": "a" * 64, "effect_cursor": 0}))
        session.add(ProjectionCursor(**{**cursor_values(), "active_generation": str(UUID(int=1)), "rebuild_state": {"candidate_generation": str(UUID(int=2)), "domain_watermark": 70, "journal_watermark": 80, "last_replayed_sequence": 75, "state": "catching_up"}}))
    with Session(engine) as session, session.begin():
        row = session.get(ProjectionCursor, "research_inbox.v1")
        assert row is not None and row.rebuild_state is not None
        assert row.rebuild_state["last_replayed_sequence"] == 75
        result = session.execute(update(ProjectionCursor).where(ProjectionCursor.consumer == row.consumer, ProjectionCursor.revision == 1).values(revision=2, active_generation=str(UUID(int=2)), rebuild_state=None))
        assert result.rowcount == 1  # type: ignore[attr-defined]
    with Session(engine) as session:
        row = session.get(ProjectionCursor, "research_inbox.v1")
        assert row is not None and row.active_generation == str(UUID(int=2))
        assert row.rebuild_state is None and row.revision == 2
        assert session.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='research_domain_event'")) == 3

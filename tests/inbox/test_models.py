"""IT-002 persistence constraints, revision separation and journal protection."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import Engine, Table, create_engine, delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from investment_agent.storage.models import Base
from investment_agent.storage.models.monitoring import ReviewCycle
from investment_agent.storage.models.research_inbox import (
    ResearchInboxEntry,
    ResearchInboxEntrySource,
    ResearchInboxJournal,
)

NOW = datetime(2026, 11, 7, tzinfo=UTC)
GENERATION, ENTRY, EVENT, REQUEST, ACCOUNT, CASE = (str(UUID(int=i)) for i in range(1, 7))
TABLES = [cast(Table, model.__table__) for model in
          (ResearchInboxEntry, ResearchInboxJournal, ResearchInboxEntrySource, ReviewCycle)]


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'inbox-models.db'}")
    Base.metadata.create_all(engine, tables=TABLES)
    yield engine
    engine.dispose()


def entry(**overrides: Any) -> dict[str, Any]:
    return dict(generation=GENERATION, id=ENTRY, owner_id="local_owner", subject_kind="listing",
                listing_id=18, account_id=None, case_id=None, case_episode=None,
                group_key="listing:18:round:381", state="open", priority_rank=1,
                control_status="active", source_status="current", view={}) | overrides


def journal(**overrides: Any) -> dict[str, Any]:
    return dict(generation=GENERATION, entry_id=ENTRY, kind="source", domain_event_id=EVENT,
                effect_ordinal=0, owner_id="local_owner", request_id=None, request_hash=None,
                result={"state": "committed"}, entry_revision=1,
                entry_view={"id": ENTRY, "revision": 1, "unread": True}, recorded_at=NOW) | overrides


def test_read_does_not_consume_future_content_and_generation_is_independent(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(ResearchInboxEntry(**entry()))
        session.add(ResearchInboxEntry(**entry(generation=str(UUID(int=99)))))
    with Session(engine) as session, session.begin():
        row = session.get(ResearchInboxEntry, (GENERATION, ENTRY))
        assert row is not None
        assert (row.revision, row.content_revision, row.read_content_revision) == (1, 1, 0)
        row.revision, row.read_content_revision = 2, 1
    with Session(engine) as session, session.begin():
        row = session.get(ResearchInboxEntry, (GENERATION, ENTRY))
        assert row is not None
        row.revision, row.content_revision = 3, 2
    with Session(engine) as session:
        row = session.get(ResearchInboxEntry, (GENERATION, ENTRY))
        assert row is not None and row.read_content_revision < row.content_revision
        assert len(list(session.scalars(select(ResearchInboxEntry)))) == 2


@pytest.mark.parametrize("invalid", [
    {"account_id": ACCOUNT}, {"listing_id": None}, {"subject_kind": "account"},
    {"subject_kind": "other"}, {"case_id": CASE}, {"case_episode": 1},
    {"case_id": CASE, "case_episode": 0},
    {"subject_kind": "account", "listing_id": None, "account_id": ACCOUNT, "case_id": CASE, "case_episode": 1},
    {"read_content_revision": 2}, {"content_revision": 2}, {"content_revision": 0},
    {"read_content_revision": -1}, {"state": "unknown"}, {"source_status": "valid"},
    {"priority_rank": -1}, {"last_event_sequence": -1}, {"latest_journal_sequence": -1},
    {"view": []},
])
def test_invalid_subject_case_or_versions_are_rejected(engine: Engine, invalid: dict[str, Any]) -> None:
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(ResearchInboxEntry(**entry(**invalid)))
        session.flush()


def test_group_key_cannot_create_two_items_in_one_generation(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(ResearchInboxEntry(**entry()))
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(ResearchInboxEntry(**entry(id=str(UUID(int=99)))))
        session.flush()


@pytest.mark.parametrize("mutation", ["orm_update", "orm_delete", "bulk_update", "bulk_delete", "replace_event", "replace_request", "replace_rowid"])
def test_journal_history_is_immutable(engine: Engine, mutation: str) -> None:
    with Session(engine) as session, session.begin():
        session.add(ResearchInboxJournal(**journal(request_id=REQUEST, request_hash="a" * 64)))
    with Session(engine) as session:
        with pytest.raises((IntegrityError, ValueError)):
            if mutation.startswith("orm"):
                row = session.scalar(select(ResearchInboxJournal))
                assert row is not None
                if mutation == "orm_delete":
                    session.delete(row)
                else:
                    row.entry_view = {"changed": True}
            elif mutation == "bulk_update":
                session.execute(update(ResearchInboxJournal).values(owner_id="changed"))
            elif mutation == "bulk_delete":
                session.execute(delete(ResearchInboxJournal))
            elif mutation == "replace_event":
                session.execute(cast(Table, ResearchInboxJournal.__table__).insert().prefix_with("OR REPLACE").values(**journal()))
            elif mutation == "replace_request":
                session.execute(cast(Table, ResearchInboxJournal.__table__).insert().prefix_with("OR REPLACE").values(
                    **journal(generation=str(UUID(int=99)), domain_event_id=None, request_id=REQUEST, request_hash="b" * 64)))
            else:
                session.execute(text(
                    "INSERT OR REPLACE INTO research_inbox_journal "
                    "(rowid,generation,kind,effect_ordinal,owner_id,result,recorded_at) "
                    "VALUES (1,:generation,'repair',0,'other','{}',:now)"),
                    {"generation": GENERATION, "now": NOW})
            session.flush()
        session.rollback()
    with Session(engine) as session:
        row = session.scalar(select(ResearchInboxJournal))
        assert row is not None and row.owner_id == "local_owner"
        assert row.entry_view == journal()["entry_view"] and row.request_hash == "a" * 64


def test_scope_journal_null_identities_and_distinct_effect_ordinals_are_allowed(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add_all([ResearchInboxJournal(**journal()),
                         ResearchInboxJournal(**journal(effect_ordinal=1))])
        for _ in range(2):
            session.add(ResearchInboxJournal(**journal(kind="control", entry_id=None,
                        domain_event_id=None, entry_revision=None, entry_view=None)))
    with Session(engine) as session:
        assert [r.sequence for r in session.scalars(select(ResearchInboxJournal).order_by(ResearchInboxJournal.sequence))] == [1, 2, 3, 4]


@pytest.mark.parametrize("invalid", [
    {"sequence": 0}, {"effect_ordinal": -1}, {"request_id": REQUEST},
    {"request_hash": "a" * 64}, {"request_id": REQUEST, "request_hash": "short"},
    {"entry_view": None}, {"entry_view": []}, {"entry_revision": 0},
    {"entry_id": None}, {"result": []}, {"kind": "mutable"},
])
def test_journal_requires_complete_view_and_request_hash(engine: Engine, invalid: dict[str, Any]) -> None:
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(ResearchInboxJournal(**journal(**invalid)))
        session.flush()


def test_source_relation_requires_review_reference_and_is_rebuildable(engine: Engine) -> None:
    values = dict(generation=GENERATION, entry_id=ENTRY, event_id=EVENT,
                  exact_ref={"kind": "review_round", "id": 381, "revision": 1},
                  relation="support", relation_review_ref=None, active=True,
                  invalidated_by=None, group_member_ordinal=0)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(ResearchInboxEntrySource(**values))
        session.flush()
    with Session(engine) as session, session.begin():
        session.add(ResearchInboxEntrySource(**(values | {"relation": "unlinked"})))
    with Session(engine) as session, session.begin():
        row = session.get(ResearchInboxEntrySource, (GENERATION, ENTRY, EVENT))
        assert row is not None
        row.active = False
        row.invalidated_by = str(UUID(int=99))
    for fk in inspect(engine).get_foreign_keys("research_inbox_entry_source"):
        assert fk["options"].get("ondelete") != "CASCADE"


def test_cycle_revision_defaults_are_scalar_and_never_inferred_from_payload(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(ReviewCycle(id="python", case_id=CASE, task_id=None, state="pending", payload={"cycle_event_revision": 99}))
        session.execute(text("INSERT INTO investor_review_cycle (id,case_id,state,payload) VALUES ('sql',:case,'pending','{}')"), {"case": CASE})
    with Session(engine) as session:
        rows = list(session.scalars(select(ReviewCycle)))
        assert len(rows) == 2 and all(row.cycle_event_revision == 0 for row in rows)
        assert session.get(ReviewCycle, "python").payload == {"cycle_event_revision": 99}  # type: ignore[union-attr]
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.execute(update(ReviewCycle).values(cycle_event_revision=-1))
    columns = {column["name"]: column for column in inspect(engine).get_columns("investor_review_cycle")}
    assert str(columns["cycle_event_revision"]["type"]) == "INTEGER"
    assert columns["cycle_event_revision"]["nullable"] is False

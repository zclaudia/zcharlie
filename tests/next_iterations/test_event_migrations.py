"""EV-03: real migration chain on disposable SQLite, never the configured DB."""

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Table, create_engine
from sqlalchemy.orm import Session
from tests.support.next_iterations_migrations import migration_config, table_schema

from investment_agent.research_events.canonical import source_identity
from investment_agent.research_events.contracts import EventEnvelope
from investment_agent.storage.models import Base
from investment_agent.storage.models.research_events import (
    ConsumerReceipt,
    ProjectionCursor,
    ResearchDomainEvent,
)

ROOT = Path(__file__).resolve().parents[2]
TABLES = [cast(Table, cls.__table__) for cls in
          (ResearchDomainEvent, ConsumerReceipt, ProjectionCursor)]


@pytest.fixture(scope="module")
def predecessor(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("event-migration-base") / "base.db"
    with migration_config(database) as config:
        command.upgrade(config, "061")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_setting (key,value_json,updated_at,updated_by) VALUES (?,?,?,?)",
            ("offline-migration-probe", '{"retained":true}', "2026-11-07 00:00:00", "fixture"),
        )
    return database


@pytest.fixture
def migrated(predecessor: Path, tmp_path: Path) -> Path:
    database = tmp_path / "events.db"
    shutil.copy2(predecessor, database)
    with migration_config(database) as config:
        command.upgrade(config, "062")
    return database


def schema(database: Path) -> dict[str, Any]:
    return table_schema(database, TABLES)


def test_pristine_upgrade_and_single_head(tmp_path: Path) -> None:
    database = tmp_path / "fresh.db"
    with migration_config(database) as config:
        heads = ScriptDirectory.from_config(config).get_heads()
        assert len(heads) == 1
        # Future migrations may advance head; 062 must remain in the one chain.
        assert ScriptDirectory.from_config(config).get_revision("062").down_revision == "061"
        command.upgrade(config, "062")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("062",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM research_domain_event").fetchone() == (0,)


def test_upgrade_preserves_settings_and_matches_model_schema(migrated: Path, tmp_path: Path) -> None:
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT value_json FROM app_setting WHERE key='offline-migration-probe'").fetchone() == ('{"retained":true}',)
        triggers = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='research_domain_event'").fetchall()
        assert len(triggers) == 3
    model_db = tmp_path / "model.db"
    engine = create_engine(f"sqlite:///{model_db}")
    try:
        Base.metadata.create_all(engine, tables=TABLES)
    finally:
        engine.dispose()
    assert schema(migrated) == schema(model_db)


def test_empty_downgrade_and_reupgrade_preserves_prior_data(migrated: Path) -> None:
    with migration_config(migrated) as config:
        command.downgrade(config, "061")
        with sqlite3.connect(migrated) as connection:
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='research_domain_event'").fetchone() is None
            assert connection.execute("SELECT COUNT(*) FROM app_setting WHERE key='offline-migration-probe'").fetchone() == (1,)
        command.upgrade(config, "062")
    assert "research_domain_event" in schema(migrated)


def test_history_blocks_downgrade_and_migration_triggers_block_replace(migrated: Path) -> None:
    wire = json.loads((ROOT / "tests/fixtures/next_iterations/contracts/events.valid.json").read_text())["events"][1]
    envelope = EventEnvelope.model_validate(wire)
    values = {**wire, "occurred_at": envelope.occurred_at, "recorded_at": envelope.recorded_at,
              "source_identity": source_identity(envelope.source_ref)}
    engine = create_engine(f"sqlite:///{migrated}")
    try:
        with Session(engine) as session, session.begin():
            session.add(ResearchDomainEvent(**values))
    finally:
        engine.dispose()
    with migration_config(migrated) as config, pytest.raises(RuntimeError, match="history must be retained"):
        command.downgrade(config, "061")
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("062",)
        for sql in (
            "UPDATE research_domain_event SET schema_version=2",
            "DELETE FROM research_domain_event",
            "INSERT OR REPLACE INTO research_domain_event SELECT * FROM research_domain_event",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
        assert connection.execute("SELECT payload_hash FROM research_domain_event").fetchone() == (wire["payload_hash"],)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

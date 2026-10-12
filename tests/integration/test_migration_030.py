"""030 migrates retained event payloads and explicit SQLite AUTOINCREMENT."""

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_conversation_migration_retains_events_and_fresh_head(tmp_path, monkeypatch):
    for fresh in (False, True):
        db = tmp_path / f"conversation-{fresh}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
        command.upgrade(config, "029")
        payload = json.dumps({"occurred_at": "2026-01-01T08:00:00+08:00", "type": "progress", "payload": {"summary": "retained"}})
        with sqlite3.connect(db) as conn:
            if not fresh:
                conn.execute("INSERT INTO agent_task_event(event_id,task_id,payload) VALUES(73,'retained',?)", (payload,))
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{db}")
        with engine.connect() as conn:
            assert not [diff for diff in compare_metadata(MigrationContext.configure(conn), Base.metadata) if "agent_" in repr(diff)]
        engine.dispose()
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='agent_task_event'").fetchone()[0]
            assert "AUTOINCREMENT" in ddl
            if not fresh:
                assert json.loads(conn.execute("SELECT payload FROM agent_task_event").fetchone()[0]) == json.loads(payload)
                # The backfill writes the same six-digit UTC storage string the
                # ORM does, not the payload's +08:00 ISO form.
                assert conn.execute("SELECT occurred_at FROM agent_task_event").fetchone()[0] == "2026-01-01 00:00:00.000000"
                conn.execute("DELETE FROM agent_task_event")
                conn.execute("INSERT INTO agent_task_event(task_id,payload,occurred_at) VALUES('retained',?,'2026-02-01')", (payload,))
                assert conn.execute("SELECT event_id FROM agent_task_event").fetchone()[0] > 73
        command.downgrade(config, "029")
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='agent_conversation'").fetchone() is None
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='agent_task'").fetchone()
            if not fresh:
                assert json.loads(conn.execute("SELECT payload FROM agent_task_event").fetchone()[0]) == json.loads(payload)
        command.upgrade(config, "030")


def test_invalid_payload_fails_before_any_ddl(tmp_path, monkeypatch):
    db = tmp_path / "invalid-event.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    command.upgrade(config, "029")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO agent_task_event(event_id,task_id,payload) VALUES(1,'retained',?)",
            (json.dumps({"occurred_at": "2026-01-01T00:00:00"}),),  # naive: invalid
        )
    try:
        command.upgrade(config, "head")
    except ValueError:
        pass
    else:
        raise AssertionError("naive payload time must fail the migration")
    # The pysqlite driver autocommits DDL, so the migration must validate every
    # backfill value first: a late failure leaves no column behind and the
    # version at 029, keeping the re-runnable path intact.
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("029",)
        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_task_event)")}
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_task)")}
        assert "occurred_at" not in event_columns and "event_floor" not in task_columns

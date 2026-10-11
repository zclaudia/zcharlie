"""041 backfills conversation activity time and create-time subject projection."""

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base

CURRENT_HEAD = ScriptDirectory(
    str(Path(__file__).resolve().parents[2] / "alembic")
).get_current_head()

CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"
MESSAGE_ONE = "33333333-3333-4333-8333-333333333333"
MESSAGE_TWO = "44444444-4444-4444-8444-444444444444"


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    return config


def _conversation_payload(created_at: str) -> str:
    return json.dumps({"id": CONVERSATION_ID, "schema_version": 1, "revision": 1, "created_at": created_at,
        "actor": "local_owner", "origin_ref": None, "title": None,
        "context_refs": [{"kind": "listing", "id": 7, "revision": 1, "field_path": None},
            {"kind": "case", "id": "22222222-2222-4222-8222-222222222222", "revision": 1, "field_path": None}]})


def _message_payload(message_id: str, sequence: int, created_at: str) -> str:
    return json.dumps({"id": message_id, "schema_version": 1, "revision": 1, "created_at": created_at,
        "actor": "local_owner", "origin_ref": None,
        "conversation_ref": {"kind": "conversation", "id": CONVERSATION_ID, "revision": 1, "field_path": None},
        "sequence": sequence, "role": "user", "text": "问题", "content": [], "context_refs": [],
        "reply_to": None, "client_message_id": message_id,
        "task_ref": {"kind": "task", "id": "55555555-5555-4555-8555-555555555555", "revision": 1, "field_path": None},
        "artifact_refs": [], "disclosure_access": []})


def test_backfill_mixed_offsets_order_and_subject_projection(tmp_path, monkeypatch):
    db = tmp_path / "activity.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = _config()
    command.upgrade(config, "040")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO listing(id,company_id,market_code,ticker,is_primary) VALUES(7,1,'SH','600001',1)")
        conn.execute("INSERT INTO agent_conversation(id,owner_id,next_sequence,payload) VALUES(?, 'local_owner', 1, ?)",
            (CONVERSATION_ID, _conversation_payload("2026-03-01T12:30:00.5+08:00")))
        conn.execute("INSERT INTO agent_message(id,conversation_id,client_message_id,completion_key,input_hash,sequence,payload) VALUES(?,?,?,?,?,?,?)",
            (MESSAGE_ONE, CONVERSATION_ID, MESSAGE_ONE, None, "hash", 1, _message_payload(MESSAGE_ONE, 1, "2026-03-01T04:00:00.000001+00:00")))
        conn.execute("INSERT INTO agent_message(id,conversation_id,client_message_id,completion_key,input_hash,sequence,payload) VALUES(?,?,?,?,?,?,?)",
            (MESSAGE_TWO, CONVERSATION_ID, MESSAGE_TWO, None, "hash", 2, _message_payload(MESSAGE_TWO, 2, "2026-03-01T05:10:00.999999Z")))
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as connection:
        assert not [diff for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if "agent_conversation" in repr(diff)]
    engine.dispose()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        row = conn.execute("SELECT last_activity_at, title_revision, subject_refs FROM agent_conversation WHERE id=?",
            (CONVERSATION_ID,)).fetchone()
        # 12:30+08:00 is 04:30 UTC; the newest message wins and stays six-digit UTC.
        assert row[0] == "2026-03-01 05:10:00.999999", row
        assert row[1] == 1
        assert json.loads(row[2]) == [{"kind": "listing", "id": 7, "revision": 1}]
    command.downgrade(config, "040")
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_conversation)")}
        assert {"last_activity_at", "title_revision", "subject_refs"}.isdisjoint(columns)
        assert conn.execute("SELECT payload FROM agent_conversation WHERE id=?", (CONVERSATION_ID,)).fetchone() is not None
    # Re-applying the migration after the downgrade proves 041 is re-entrant on
    # the already-backfilled rows, and the database is left at head (045) rather
    # than parked on a mid version.
    command.upgrade(config, "head")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
        row = conn.execute(
            "SELECT last_activity_at, title_revision, subject_refs FROM agent_conversation WHERE id=?",
            (CONVERSATION_ID,),
        ).fetchone()
        assert row[0] == "2026-03-01 05:10:00.999999" and row[1] == 1


def test_naive_payload_time_fails_upgrade_and_rolls_back(tmp_path, monkeypatch):
    db = tmp_path / "invalid.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = _config()
    command.upgrade(config, "040")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO agent_conversation(id,owner_id,next_sequence,payload) VALUES(?, 'local_owner', 1, ?)",
            (CONVERSATION_ID, _conversation_payload("2026-03-01T12:30:00")))
    try:
        command.upgrade(config, "head")
    except ValueError:
        pass
    else:
        raise AssertionError("naive payload time must fail the migration")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("040",)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_conversation)")}
        assert "last_activity_at" not in columns


def test_fresh_head(tmp_path, monkeypatch):
    db = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = _config()
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as connection:
        assert not [diff for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if "agent_conversation" in repr(diff)]
    engine.dispose()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)

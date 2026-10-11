"""042 adds draft-request storage and backfills watchlist draft state.

Backfill branches (plan 1.5 存量处理): an open candidate-scope draft wins and
links as ``ready``; an entry without any candidate-scope rule becomes
``needs_template``; an entry with an active rule keeps ``none``.
"""

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    return config


def _insert_listing(conn: sqlite3.Connection, listing_id: int, ticker: str) -> None:
    conn.execute(
        "INSERT INTO listing(id,company_id,market_code,ticker,is_primary) VALUES(?,1,'SH',?,1)",
        (listing_id, ticker),
    )


def _insert_item(conn: sqlite3.Connection, listing_id: int) -> None:
    conn.execute(
        "INSERT INTO watchlist_item(listing_id,added_at,provider,enabled,last_refresh_status) "
        "VALUES(?,'2026-09-01 00:00:00','auto',1,'never')",
        (listing_id,),
    )


def _insert_rule(
    conn: sqlite3.Connection, listing_id: int, symbol: str, version: int, status: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO rule_instance(listing_id,symbol,scope,template_id,template_version,"
        "version,status,definition,hash,drafted_by,draft_rationale) VALUES(?,?,'candidate',"
        "'quality_growth',1,?,?,?,?,'llm:stub','{}')",
        (listing_id, symbol, version, status, json.dumps({"params": {}}), f"hash-{listing_id}-{version}"),
    )
    return int(cursor.lastrowid)


def test_backfill_draft_state_and_schema(tmp_path, monkeypatch):
    db = tmp_path / "watch.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = _config()
    command.upgrade(config, "041")
    draft_ids: dict[int, int] = {}
    with sqlite3.connect(db) as conn:
        # 7: active rule only -> stays none.
        _insert_listing(conn, 7, "600007")
        _insert_item(conn, 7)
        _insert_rule(conn, 7, "600007.SH", 1, "active")
        # 8: one open draft -> ready and linked.
        _insert_listing(conn, 8, "600008")
        _insert_item(conn, 8)
        draft_ids[8] = _insert_rule(conn, 8, "600008.SH", 1, "draft")
        # 9: nothing -> needs_template.
        _insert_listing(conn, 9, "600009")
        _insert_item(conn, 9)
        # 10: active plus a newer open draft -> the draft wins.
        _insert_listing(conn, 10, "600010")
        _insert_item(conn, 10)
        _insert_rule(conn, 10, "600010.SH", 1, "active")
        draft_ids[10] = _insert_rule(conn, 10, "600010.SH", 2, "draft")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as connection:
        diffs = [
            diff
            for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if "watchlist_item" in repr(diff)
            or "watch_draft_request" in repr(diff)
            or "rule_draft_generation" in repr(diff)
        ]
        assert not diffs, diffs
    engine.dispose()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
        rows = dict(
            conn.execute(
                "SELECT listing_id, draft_state FROM watchlist_item ORDER BY listing_id"
            ).fetchall()
        )
        assert rows == {7: "none", 8: "ready", 9: "needs_template", 10: "ready"}
        pointers = dict(
            conn.execute(
                "SELECT listing_id, draft_rule_instance_id FROM watchlist_item ORDER BY listing_id"
            ).fetchall()
        )
        assert pointers[8] == draft_ids[8]
        assert pointers[10] == draft_ids[10]
        assert pointers[7] is None and pointers[9] is None
        columns = {row[1] for row in conn.execute("PRAGMA table_info(watch_draft_request)")}
        assert {"id", "watchlist_id", "listing_id", "template_id", "template_version", "as_of",
                "status", "rule_instance_id", "superseded_by"} <= columns
        generation = {row[1] for row in conn.execute("PRAGMA table_info(rule_draft_generation)")}
        assert {"listing_id", "scope", "generation", "current_token",
                "current_origin_kind", "current_origin_id", "rule_head"} <= generation
    command.downgrade(config, "041")
    with sqlite3.connect(db) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "rule_draft_generation" not in tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(watchlist_item)")}
        assert {"draft_state", "draft_request_id", "draft_rule_instance_id"}.isdisjoint(columns)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "watch_draft_request" not in tables
    command.upgrade(config, "042")

"""044 adds the shared rule-draft generation claim per (listing, scope)."""

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
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[2] / "alembic")
    )
    return config


def test_rule_draft_generation_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "generations.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = _config()
    command.upgrade(config, "043")
    command.upgrade(config, "044")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as connection:
        diffs = [
            diff
            for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if "rule_draft_generation" in repr(diff)
        ]
        assert not diffs, diffs
    engine.dispose()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("044",)
        conn.execute(
            "INSERT INTO listing(id,company_id,market_code,ticker,is_primary) VALUES(9,1,'SH','600009',1)"
        )
        conn.execute("INSERT INTO rule_draft_generation(listing_id,scope) VALUES(9,'candidate')")
        # A claimed slot freezes generation zero before any token is issued;
        # both come from server defaults, never from the caller.
        row = conn.execute(
            "SELECT generation,current_token FROM rule_draft_generation WHERE listing_id=9"
        ).fetchone()
        assert row == (0, None), row
    command.upgrade(config, "head")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
        # 045 only adds its own tables; the 044 claim survives untouched.
        assert conn.execute(
            "SELECT generation,current_token FROM rule_draft_generation WHERE listing_id=9"
        ).fetchone() == (0, None)
    command.downgrade(config, "044")
    with sqlite3.connect(db) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "zone_event" not in tables and "zone_interval_state" not in tables
        assert conn.execute(
            "SELECT generation FROM rule_draft_generation WHERE listing_id=9"
        ).fetchone() == (0,)
    command.downgrade(config, "043")
    with sqlite3.connect(db) as conn:
        # 044 has no backfill: downgrading past it drops the claim rows by design
        # (a slot without a row simply has no claim at 043 semantics).
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "rule_draft_generation" not in tables
    command.upgrade(config, "head")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
        assert conn.execute("SELECT count(*) FROM rule_draft_generation").fetchone()[0] == 0

"""045 adds the zone event log and the per-(listing, scope, zone) interval state."""

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


def test_zone_event_schema_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "zones.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = _config()
    command.upgrade(config, "044")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as connection:
        diffs = [
            diff
            for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if "zone_event" in repr(diff) or "zone_interval_state" in repr(diff)
        ]
        assert not diffs, diffs
    engine.dispose()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"zone_event", "zone_interval_state"} <= tables
        # The rule reference is mandatory: an event without the rule version it
        # came from is the defect this table refuses to store (plan 6.2 WP8).
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(zone_event)")}
        assert columns["rule_instance_id"][3] == 1  # notnull
        assert columns["rule_version"][3] == 1
        assert columns["input_digest"][3] == 1
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(zone_event)")}
        # SQLite names a UNIQUE constraint's implicit index itself; the point is
        # that a duplicate identity is refused at the storage level.
        assert "sqlite_autoindex_zone_event_1" in indexes
    command.downgrade(config, "044")
    with sqlite3.connect(db) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "zone_event" not in tables and "zone_interval_state" not in tables
    command.upgrade(config, "head")

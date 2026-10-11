"""043 adds the valuation table and the account's current-valuation pointer."""

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


def test_valuation_schema_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "valuation.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = _config()
    command.upgrade(config, "042")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO portfolio_account(id,owner_id,revision,current_snapshot_id,input_hash,payload) "
            "VALUES('11111111-1111-4111-8111-111111111111','local_owner',0,NULL,'hash','{}')"
        )
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as connection:
        diffs = [
            diff
            for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if "portfolio_valuation" in repr(diff) or "portfolio_account" in repr(diff)
        ]
        assert not diffs, diffs
    engine.dispose()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(portfolio_account)")}
        assert "current_valuation_id" in columns
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "portfolio_valuation" in tables
    command.downgrade(config, "042")
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(portfolio_account)")}
        assert "current_valuation_id" not in columns
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "portfolio_valuation" not in tables
    command.upgrade(config, "head")

"""025 upgrades 024, matches metadata, and downgrades without touching legacy rows."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models import investor  # noqa: F401
from investment_agent.storage.models.base import Base


def test_upgrade_downgrade_and_fresh_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for fresh in (False, True):
        db = tmp_path / f"investor-{fresh}.db"
        url = f"sqlite+aiosqlite:///{db}"
        monkeypatch.setenv("DATABASE_URL", url)
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[2] / "alembic")
        )
        config.set_main_option("sqlalchemy.url", url)
        if not fresh:
            command.upgrade(config, "024")
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{db}")
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert [d for d in diffs if "investor_" in repr(d)] == []
        engine.dispose()
        with sqlite3.connect(db) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "045",
            )
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "investor_mutation" in tables and "investor_episode_revision" in tables
        command.downgrade(config, "024")
        with sqlite3.connect(db) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "024",
            )
            tables = {
                r[0]
                for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert not any(t.startswith("investor_") for t in tables)
            assert {"candidate", "watchlist_item", "earnings_forecast"} <= tables

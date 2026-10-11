"""035 temporary-file migration metadata and retained immutable history."""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_portfolio_migration_roundtrip(tmp_path, monkeypatch):
    database = tmp_path / "portfolio.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    command.upgrade(config, "034")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO investor_mandate_head VALUES ('local_owner','retain',1)")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        relevant = {"portfolio_account", "portfolio_event", "portfolio_snapshot", "portfolio_request"}
        assert not [diff for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata) if any(table in repr(diff) for table in relevant)]
    engine.dispose()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
        connection.execute(
            "INSERT INTO portfolio_account(id,owner_id,revision,current_snapshot_id,input_hash,payload) "
            "VALUES ('account','local_owner',0,NULL,'hash','{}')"
        )
    command.downgrade(config, "034")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT mandate_id FROM investor_mandate_head").fetchone() == ("retain",)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='portfolio_event'").fetchone() is None
    command.upgrade(config, "035")

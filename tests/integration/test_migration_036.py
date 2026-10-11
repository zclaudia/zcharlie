"""036 only runs against isolated temporary files."""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_portfolio_plans_migration_fresh_and_roundtrip(tmp_path, monkeypatch):
    for fresh in (True, False):
        database = tmp_path / f"plans-{fresh}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
        if not fresh:
            command.upgrade(config, "035")
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO portfolio_account VALUES ('account','local_owner',0,NULL,'hash','{}')")
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database}")
        with engine.connect() as connection:
            assert not [d for d in compare_metadata(MigrationContext.configure(connection), Base.metadata) if "portfolio_plan_" in repr(d)]
        engine.dispose()
        command.downgrade(config, "035")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("035",)
            if not fresh:
                assert connection.execute("SELECT id FROM portfolio_account").fetchone() == ("account",)
        command.upgrade(config, "036")

"""039 fresh and 038 roundtrip preserve pre-existing actual ledger objects."""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_purposes_fresh_roundtrip(tmp_path, monkeypatch):
    for fresh in (True, False):
        database = tmp_path / f"purposes-{fresh}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
        if not fresh:
            command.upgrade(config, "038")
        command.upgrade(config, "039")
        engine = create_engine(f"sqlite:///{database}")
        with engine.connect() as connection:
            assert not [d for d in compare_metadata(MigrationContext.configure(connection), Base.metadata) if "portfolio_purpose" in repr(d)]
        engine.dispose()
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO disclosure_blob VALUES ('retained', ?)", (b"original",))
        command.downgrade(config, "038")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT body FROM disclosure_blob").fetchone() == (b"original",)
        command.upgrade(config, "039")

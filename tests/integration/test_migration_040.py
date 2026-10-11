"""040 fresh and 039 roundtrip preserve pre-existing actual ledger objects."""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_method_versions_fresh_roundtrip(tmp_path, monkeypatch):
    for fresh in (True, False):
        database = tmp_path / f"methods-{fresh}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
        if not fresh:
            command.upgrade(config, "039")
        command.upgrade(config, "040")
        engine = create_engine(f"sqlite:///{database}")
        with engine.connect() as connection:
            assert not [d for d in compare_metadata(MigrationContext.configure(connection), Base.metadata) if any(name in repr(d) for name in ("investor_method", "agent_task_method"))]
        engine.dispose()
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO disclosure_blob VALUES ('retained', ?)", (b"original",))
        command.downgrade(config, "039")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT body FROM disclosure_blob").fetchone() == (b"original",)
        command.upgrade(config, "040")

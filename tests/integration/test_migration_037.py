"""037 fresh / roundtrip only on temporary SQLite; bytes survive reconnection."""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_disclosures_migration_fresh_roundtrip_and_original_restart(tmp_path, monkeypatch):
    for fresh in (True, False):
        database = tmp_path / f"originals-{fresh}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[2] / "alembic")
        )
        if not fresh:
            command.upgrade(config, "036")
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database}")
        with engine.connect() as connection:
            assert not [
                d
                for d in compare_metadata(MigrationContext.configure(connection), Base.metadata)
                if "disclosure_" in repr(d)
            ]
        engine.dispose()
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO disclosure_blob VALUES ('hash', ?)", (b"immutable-original",)
            )
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT body FROM disclosure_blob").fetchone() == (
                b"immutable-original",
            )
        command.downgrade(config, "036")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "036",
            )
        command.upgrade(config, "037")

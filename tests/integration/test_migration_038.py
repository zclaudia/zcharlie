"""038 owns only queue cursor and delivery rows; fresh and 037 roundtrip."""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_operations_schema_fresh_roundtrip(tmp_path, monkeypatch):
    for fresh in (True, False):
        database = tmp_path / f"operations-{fresh}.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[2] / "alembic")
        )
        if not fresh:
            command.upgrade(config, "037")
        command.upgrade(config, "038")
        engine = create_engine(f"sqlite:///{database}")
        with engine.connect() as connection:
            assert not [
                d
                for d in compare_metadata(MigrationContext.configure(connection), Base.metadata)
                if any(
                    table in repr(d)
                    for table in ("agent_operations_cursor", "investor_notification_delivery")
                )
            ]
        engine.dispose()
        with sqlite3.connect(database) as connection:
            connection.execute("INSERT INTO agent_operations_cursor VALUES (1, '{\"slot\":3}')")
            connection.execute("INSERT INTO disclosure_blob VALUES ('retained', ?)", (b"original",))
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT payload FROM agent_operations_cursor").fetchone() == (
                '{"slot":3}',
            )
        command.downgrade(config, "037")
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT body FROM disclosure_blob").fetchone() == (
                b"original",
            )
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                "037",
            )
        command.upgrade(config, "038")

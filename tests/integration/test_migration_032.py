"""032 round-trip migration, on a temporary DB only."""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_user_controls_migration_metadata_and_roundtrip(tmp_path, monkeypatch):
    database = tmp_path / "controls.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    command.upgrade(config, "031")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO investor_mandate_head VALUES ('local_owner', 'retained', 1)")
    command.upgrade(config, "032")
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        relevant = {"investor_user_object", "investor_user_object_revision", "investor_memory_state"}
        assert not [item for item in compare_metadata(MigrationContext.configure(connection), Base.metadata) if any(table in repr(item) for table in relevant)]
    engine.dispose()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("032",)
        connection.execute("INSERT INTO investor_memory_state VALUES ('local_owner', 2)")
    command.downgrade(config, "031")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT mandate_id FROM investor_mandate_head").fetchone() == ("retained",)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='investor_memory_state'").fetchone() is None
    command.upgrade(config, "head")

"""033 temporary-file migration metadata and retained prior history."""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_monitoring_migration_metadata_and_roundtrip(tmp_path, monkeypatch):
    database = tmp_path / "monitoring.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    command.upgrade(config, "032")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO investor_memory_state VALUES ('local_owner', 7)")
    command.upgrade(config, "033")
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        relevant = {"investor_monitor_receipt", "investor_review_cycle", "investor_notification"}
        assert not [item for item in compare_metadata(MigrationContext.configure(connection), Base.metadata) if any(table in repr(item) for table in relevant) and "investor_notification_delivery" not in repr(item)]
    engine.dispose()
    command.downgrade(config, "032")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT revision FROM investor_memory_state").fetchone() == (7,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='investor_review_cycle'").fetchone() is None
    command.upgrade(config, "head")

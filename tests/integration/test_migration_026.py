"""026 adds evidence, retains AG04/legacy rows, and reverses only its own tables."""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_evidence_upgrade_and_downgrade(tmp_path, monkeypatch):
    db = tmp_path / "evidence.db"
    url = f"sqlite+aiosqlite:///{db}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "025")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO investor_mandate_head VALUES ('local_owner', 'saved-mandate', 1)"
        )
    command.upgrade(config, "026")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as connection:
        assert not [
            d
            for d in compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if "investor_" in repr(d) and not any(
                later in repr(d)
                for later in ("investor_method_", "investor_publication_", "investor_user_object", "investor_memory_state", "investor_monitor_receipt", "investor_review_cycle", "investor_notification", "investor_discovery_cursor", "investor_discovery_batch")
            )
        ]
    engine.dispose()
    with sqlite3.connect(db) as connection:
        for table in ("investor_evidence", "investor_calculation"):
            connection.execute(f"INSERT INTO {table} VALUES ('record', 1, 'local_owner', '{{}}')")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("026",)
    command.downgrade(config, "025")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT mandate_id FROM investor_mandate_head").fetchone() == (
            "saved-mandate",
        )
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "investor_evidence" not in tables and "investor_calculation" not in tables
        assert "financial_snapshot" in tables

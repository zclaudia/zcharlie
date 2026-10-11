"""027 upgrade/downgrade on isolated files, with metadata and prior rows."""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base

CURRENT_HEAD = ScriptDirectory(
    str(Path(__file__).resolve().parents[2] / "alembic")
).get_current_head()


def test_agent_upgrade_downgrade(tmp_path, monkeypatch):
    db = tmp_path / "tasks.db"
    url = f"sqlite+aiosqlite:///{db}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    command.upgrade(config, "026")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO investor_mandate_head VALUES ('local_owner', 'retained', 1)")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        assert not [
            d
            for d in compare_metadata(MigrationContext.configure(conn), Base.metadata)
            if "agent_" in repr(d)
        ]
    engine.dispose()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
    command.downgrade(config, "026")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT mandate_id FROM investor_mandate_head").fetchone() == (
            "retained",
        )
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "agent_task" not in tables and "pipeline_run" in tables
    command.upgrade(config, "027")

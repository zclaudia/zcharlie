import sqlite3
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory

from tests.integration.test_migration_046 import _config

ROOT = Path(__file__).resolve().parents[2]


def test_055_adds_archived_at_and_downgrades_cleanly(tmp_path, monkeypatch):
    assert ScriptDirectory(str(ROOT / "alembic")).get_revision("055").down_revision == "054"
    database = tmp_path / "archive.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_conversation)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(agent_conversation)")}
    assert "archived_at" in columns and "ix_agent_conversation_archived_at" in indexes
    command.downgrade(config, "054")
    with sqlite3.connect(database) as connection:
        assert "archived_at" not in {row[1] for row in connection.execute("PRAGMA table_info(agent_conversation)")}

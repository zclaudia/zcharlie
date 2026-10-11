"""031 publication schema upgrade/downgrade uses only a temporary file."""
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base


def test_publication_migration_metadata_and_roundtrip(tmp_path, monkeypatch):
    database = tmp_path / "publication.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    command.upgrade(config, "030")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO investor_mandate_head VALUES ('local_owner', 'retained', 1)")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert not [difference for difference in compare_metadata(MigrationContext.configure(connection), Base.metadata) if "investor_publication" in repr(difference)]
    engine.dispose()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("045",)
    command.downgrade(config, "030")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT mandate_id FROM investor_mandate_head").fetchone() == ("retained",)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='investor_publication_artifact'").fetchone() is None
    command.upgrade(config, "head")

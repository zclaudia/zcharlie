"""env.py must survive a DATABASE_URL containing configparser metacharacters."""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_database_url_with_percent_upgrades(tmp_path: Path, monkeypatch) -> None:
    # Alembic validates set_main_option values through configparser
    # interpolation; without the %% doubling in env.py a bare % in DATABASE_URL
    # raises ValueError and every migration on such a URL is unusable.
    db = tmp_path / "gate%40project.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    # sqlalchemy.url stays unset on purpose: env.py derives it from DATABASE_URL.
    command.upgrade(config, "head")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "045",
        )

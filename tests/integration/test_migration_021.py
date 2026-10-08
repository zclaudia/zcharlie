"""Migration 021 (runtime settings): upgrade from 020 seeds the meta row,
fresh head matches the models, downgrade restores 020."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models import app_settings  # noqa: F401
from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = ("app_setting", "app_setting_change", "app_setting_meta")


def _config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _diffs(db_path: Path) -> list[object]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    return [d for d in diffs if any(table in repr(d) for table in TABLES)]


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_upgrade_from_020_seeds_meta_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from020.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "020")
    assert not set(TABLES) & _tables(db_path)

    command.upgrade(config, "head")
    assert set(TABLES) <= _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT id, revision, imported_at FROM app_setting_meta").fetchall() == [
            (1, 0, None)
        ]
    assert _diffs(db_path) == []

    command.downgrade(config, "020")
    assert not set(TABLES) & _tables(db_path)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert set(TABLES) <= _tables(db_path)
    assert _diffs(db_path) == []

"""Migration 060 adds the multi-perspective review tables without touching others."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from investment_agent.storage.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = ("review_round", "review_seat_run", "review_comparison")


def _config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _tables(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _metadata_diffs(db_path: Path) -> list[object]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    return [diff for diff in diffs if "review_" in repr(diff)]


def test_revision_060_follows_059() -> None:
    script = ScriptDirectory(str(REPO_ROOT / "alembic"))
    assert script.get_revision("060").down_revision == "059"


def test_upgrade_creates_and_downgrade_drops_the_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "review_panel.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "059")
    assert not set(TABLES) & _tables(db_path)

    command.upgrade(config, "head")
    assert set(TABLES) <= _tables(db_path)
    assert _metadata_diffs(db_path) == []

    command.downgrade(config, "059")
    assert not set(TABLES) & _tables(db_path)


def test_fresh_head_matches_model_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command.upgrade(_config(tmp_path / "fresh.db", monkeypatch), "head")
    assert _metadata_diffs(tmp_path / "fresh.db") == []

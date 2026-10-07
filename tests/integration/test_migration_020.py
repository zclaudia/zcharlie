"""Migration 020 (dividend coverage check history): upgrade from 019 seeds the
recorded checks, fresh head matches the models, downgrade restores 019."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models import pit_snapshot  # noqa: F401
from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLE = "dividend_coverage_check"


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
    return [d for d in diffs if TABLE in repr(d)]


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_upgrade_from_019_seeds_recorded_checks_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from019.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "019")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO market (code, name, timezone, currency) "
            "VALUES ('SSE', 'Shanghai Stock Exchange', 'Asia/Shanghai', 'CNY')"
        )
        conn.execute("INSERT INTO company (id, canonical_name) VALUES (1, '长江电力')")
        conn.execute(
            "INSERT INTO listing (id, company_id, market_code, ticker, is_primary) "
            "VALUES (1, 1, 'SSE', '600900', 1)"
        )
        conn.execute(
            "INSERT INTO dividend_coverage (listing_id, provider, year, first_checked_at, "
            "last_checked_at, event_count, is_backfill) VALUES "
            "(1, 'baostock', 2025, '2026-07-20 09:00:00', '2026-10-01 09:00:00', 2, 0), "
            "(1, 'baostock', 2024, '2026-10-06 10:00:00', '2026-10-06 10:00:00', 1, 1)"
        )
    assert TABLE not in _tables(db_path)

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as conn:
        checks = conn.execute(
            f"SELECT year, checked_at, is_backfill FROM {TABLE} ORDER BY year, checked_at"
        ).fetchall()
    assert checks == [
        (2024, "2026-10-06 10:00:00", 1),
        (2025, "2026-07-20 09:00:00", 0),
        (2025, "2026-10-01 09:00:00", 0),
    ]
    assert _diffs(db_path) == []

    command.downgrade(config, "019")
    assert TABLE not in _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dividend_coverage").fetchone() == (2,)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert TABLE in _tables(db_path)
    assert _diffs(db_path) == []

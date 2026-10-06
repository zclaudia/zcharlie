"""Migration 017 (dividend events + coverage): upgrade from 016, constraints,
fresh head matches the models, downgrade restores 016."""

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
TABLES = ("dividend_event", "dividend_coverage")


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


def _event(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO dividend_event (listing_id, ex_date, available_from, cash_per_share_pre_tax, "
        "operate_year, details, provider, captured_at, is_backfill, version) VALUES "
        "(1, '2026-07-17', '2026-04-30', 0.79, 2026, '{}', 'baostock', "
        f"'2026-10-06 09:00:00', 0, {version})"
    )


def _coverage(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO dividend_coverage (listing_id, provider, year, first_checked_at, "
        "last_checked_at, event_count, is_backfill) VALUES "
        "(1, 'baostock', 2026, '2026-10-06 09:00:00', '2026-10-06 09:00:00', 2, 0)"
    )


def test_upgrade_from_016_constraints_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from016.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "016")
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
    assert not set(TABLES) & _tables(db_path)

    command.upgrade(config, "head")
    assert set(TABLES) <= _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        _event(conn, 1)
        _event(conn, 2)
        with pytest.raises(sqlite3.IntegrityError):
            _event(conn, 2)  # (listing, ex_date, version) is unique
        _coverage(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _coverage(conn)  # (listing, provider, year) is unique
    assert _diffs(db_path) == []

    command.downgrade(config, "016")
    assert not set(TABLES) & _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM listing").fetchone() == (1,)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert set(TABLES) <= _tables(db_path)
    assert _diffs(db_path) == []

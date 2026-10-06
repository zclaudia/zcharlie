"""Migration 015 (market_snapshot.is_backfill): upgrade from 014 marks existing rows
as live captures; fresh head matches the models; downgrade restores 014."""

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
    return [d for d in diffs if "market_snapshot" in repr(d)]


def _columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(market_snapshot)")}


def test_upgrade_from_014_defaults_false_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from014.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "014")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO market (code, name, timezone, currency) "
            "VALUES ('SSE', 'Shanghai Stock Exchange', 'Asia/Shanghai', 'CNY')"
        )
        conn.execute("INSERT INTO company (id, canonical_name) VALUES (1, '贵州茅台')")
        conn.execute(
            "INSERT INTO listing (id, company_id, market_code, ticker, is_primary) "
            "VALUES (1, 1, 'SSE', '600519', 1)"
        )
        conn.execute(
            "INSERT INTO market_snapshot (listing_id, trade_date, open, high, low, close, "
            "volume, adjust, provider, captured_at, version) VALUES "
            "(1, '2026-09-30', 1, 1, 1, 1258.62, 100, 'none', 'baostock', "
            "'2026-10-06 10:00:00', 1)"
        )

    command.upgrade(config, "head")
    assert "is_backfill" in _columns(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT close, is_backfill FROM market_snapshot").fetchone() == (
            1258.62,
            0,
        )
        conn.execute(
            "INSERT INTO market_snapshot (listing_id, trade_date, open, high, low, close, "
            "volume, adjust, provider, captured_at, version, is_backfill) VALUES "
            "(1, '2021-10-08', 1, 1, 1, 1, 0, 'none', 'baostock', '2026-10-06 10:00:00', 1, 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO market_snapshot (listing_id, trade_date, open, high, low, close, "
                "volume, adjust, provider, captured_at, version, is_backfill) VALUES "
                "(1, '2021-10-11', 1, 1, 1, 1, 0, 'none', 'baostock', "
                "'2026-10-06 10:00:00', 1, NULL)"
            )
    assert _diffs(db_path) == []

    command.downgrade(config, "014")
    assert "is_backfill" not in _columns(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_snapshot").fetchone() == (2,)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert "is_backfill" in _columns(db_path)
    assert _diffs(db_path) == []

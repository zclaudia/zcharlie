"""Migration 014 (baostock breadth columns, report_overdue): upgrade from 013 keeps
existing rows valid; fresh head matches the models; downgrade restores 013."""

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
NEW_COLUMNS = {"pe_ttm", "pb_mrq", "ps_ttm", "turnover", "trade_status", "is_st"}


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
    return [d for d in diffs if "market_snapshot" in repr(d) or "report_overdue" in repr(d)]


def test_upgrade_from_013_keeps_rows_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from013.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "013")
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
            "volume, total_shares, adjust, provider, captured_at, raw_ref, version) VALUES "
            "(1, '2026-09-30', 1, 1, 1, 1258.62, 100, 1250081601, 'none', 'ifind', "
            "'2026-10-06 02:00:00', 'h.json#total_shares=i.json@2026-09-30', 1)"
        )

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshot)")}
        assert columns >= NEW_COLUMNS
        row = conn.execute(
            "SELECT close, total_shares, pe_ttm, trade_status, is_st, version "
            "FROM market_snapshot"
        ).fetchone()
        assert row == (1258.62, 1250081601, None, None, None, 1)
        conn.execute(
            "INSERT INTO market_snapshot (listing_id, trade_date, open, high, low, close, "
            "volume, adjust, provider, captured_at, version, pe_ttm, turnover, trade_status, "
            "is_st) VALUES (1, '2026-09-30', 1, 1, 1, 1, 0, 'none', 'baostock', "
            "'2026-10-06 10:00:00', 2, 19.32, 0.003066, 0, 1)"
        )
        conn.execute(
            "INSERT INTO report_overdue (listing_id, report_period, deadline, detected_on, "
            "provider) VALUES (1, '2026-06-30', '2026-08-31', '2026-09-01', 'baostock')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO report_overdue (listing_id, report_period, deadline, detected_on, "
                "provider) VALUES (1, '2026-06-30', '2026-08-31', '2026-09-02', 'baostock')"
            )
    assert _diffs(db_path) == []

    command.downgrade(config, "013")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshot)")}
        assert not (NEW_COLUMNS & columns)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "report_overdue" not in tables
        assert conn.execute("SELECT COUNT(*) FROM market_snapshot").fetchone() == (2,)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert _diffs(db_path) == []

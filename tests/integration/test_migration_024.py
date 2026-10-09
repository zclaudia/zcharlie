"""Migration 024 (earnings events): upgrade from 023 creates the three tables,
fresh head matches the models, downgrade restores 023."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models import earnings_events  # noqa: F401
from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = ("earnings_forecast", "disclosure_schedule", "business_segment")


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


def test_upgrade_from_023_creates_the_tables_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from023.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "023")
    assert not set(TABLES) & _tables(db_path)

    command.upgrade(config, "head")
    assert set(TABLES) <= _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO market (code, name, timezone, currency) "
            "VALUES ('SSE', 'Shanghai', 'Asia/Shanghai', 'CNY')"
        )
        conn.execute(
            "INSERT INTO company (canonical_name) VALUES ('贵州茅台')"
        )
        conn.execute(
            "INSERT INTO listing (company_id, market_code, ticker, is_primary) VALUES (1, 'SSE', '600519', 1)"
        )
        forecast = (
            "INSERT INTO earnings_forecast (listing_id, report_period, notice_date, forecast_type, "
            "indicator, profit_low, profit_high, yoy_low, yoy_high, content, provider, captured_at) "
            "VALUES (1, '2026-06-30', '2026-07-11', '预增', '归属于上市公司股东的净利润', "
            "23343000000, 23343000000, 0.7015, 0.7015, '预计…', 'akshare', '2026-10-08 23:46:00')"
        )
        conn.execute(forecast)
        with pytest.raises(sqlite3.IntegrityError):  # (listing, period, notice) is unique
            conn.execute(forecast)
        schedule = (
            "INSERT INTO disclosure_schedule (listing_id, report_period, first_date, "
            "scheduled_date, actual_date, provider, captured_at) VALUES (1, '2026-09-30', "
            "'2026-10-31', '2026-10-31', NULL, 'akshare', '2026-10-08 23:46:00')"
        )
        conn.execute(schedule)
        with pytest.raises(sqlite3.IntegrityError):  # one row per capture instant
            conn.execute(schedule)
        segment = (
            "INSERT INTO business_segment (listing_id, report_period, kind, name, revenue, profit, "
            "margin, share, provider, captured_at) VALUES (1, '2026-06-30', 'product', '茅台酒', "
            "77724437925.48, 71723553696.78, 0.922793, 0.856909, 'akshare', '2026-10-08 23:46:00')"
        )
        conn.execute(segment)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(segment)
        assert conn.execute("SELECT count(*) FROM business_segment").fetchone() == (1,)
    assert _diffs(db_path) == []

    command.downgrade(config, "023")
    assert not set(TABLES) & _tables(db_path)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert set(TABLES) <= _tables(db_path)
    assert _diffs(db_path) == []

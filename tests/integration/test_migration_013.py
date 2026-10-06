"""Migration 013 (point-in-time snapshot tables): fresh install and upgrade from 012."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from investment_agent.storage.models import pit_snapshot, reference  # noqa: F401
from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
NEW_TABLES = {"industry_classification", "market_snapshot", "financial_snapshot"}


def _config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    # No ini file: keeps alembic's fileConfig from reconfiguring test logging.
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _diffs_for_new_schema(db_path: Path) -> list[object]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()

    def touches_new_schema(diff: object) -> bool:
        text = repr(diff)
        return any(table in text for table in NEW_TABLES) or "universe_membership" in text

    return [diff for diff in diffs if touches_new_schema(diff)]


def test_upgrade_empty_database_to_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) >= NEW_TABLES
        membership_columns = {col["name"] for col in inspector.get_columns("universe_membership")}
        assert {"source", "captured_at"} <= membership_columns
        uniques = {
            uc["name"]: uc["column_names"]
            for table in NEW_TABLES
            for uc in inspector.get_unique_constraints(table)
        }
        assert uniques["uq_market_snapshot_version"] == [
            "listing_id",
            "trade_date",
            "adjust",
            "version",
        ]
        assert uniques["uq_financial_snapshot_version"] == [
            "listing_id",
            "report_period",
            "version",
        ]
        assert uniques["uq_industry_classification_interval"] == [
            "listing_id",
            "system",
            "valid_from",
        ]
        market_indexes = {ix["name"] for ix in inspector.get_indexes("market_snapshot")}
        assert "ix_market_snapshot_listing_date" in market_indexes
    finally:
        engine.dispose()
    assert _diffs_for_new_schema(db_path) == []


def test_upgrade_from_012_preserves_memberships_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from012.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "012")

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
        conn.execute("INSERT INTO universe (code, name) VALUES ('CSI300', '沪深300')")
        conn.execute(
            "INSERT INTO universe_membership (universe_code, listing_id, valid_from) "
            "VALUES ('CSI300', 1, '2025-01-06')"
        )

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT universe_code, listing_id, valid_from, valid_to, source, captured_at "
            "FROM universe_membership"
        ).fetchall()
        assert rows == [("CSI300", 1, "2025-01-06", None, None, None)]
        conn.execute(
            "INSERT INTO market_snapshot (listing_id, trade_date, open, high, low, close, "
            "volume, adjust, provider, captured_at, version) VALUES "
            "(1, '2026-09-30', 1, 1, 1, 1, 100, 'none', 'ifind', '2026-10-06 02:00:00', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO market_snapshot (listing_id, trade_date, open, high, low, close, "
                "volume, adjust, provider, captured_at, version) VALUES "
                "(1, '2026-09-30', 2, 2, 2, 2, 100, 'none', 'ifind', '2026-10-07 02:00:00', 1)"
            )
    assert _diffs_for_new_schema(db_path) == []

    command.downgrade(config, "012")
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not (NEW_TABLES & tables)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(universe_membership)")}
        assert "source" not in columns
        assert conn.execute("SELECT COUNT(*) FROM universe_membership").fetchone() == (1,)

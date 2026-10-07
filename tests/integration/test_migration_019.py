"""Migration 019 (iFinD verification queue + budget ledger): upgrade from 018,
partial unique index on open requests, fresh head matches the models,
downgrade restores 018."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models import verification  # noqa: F401
from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = ("ifind_call_ledger", "verification_request")


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


def _request(conn: sqlite3.Connection, status: str) -> None:
    conn.execute(
        "INSERT INTO verification_request (listing_id, ticker, trigger, priority, fields, "
        "report_period, detail, status, dedup_key, requested_at, attempts) VALUES "
        "(1, '600900.SH', 'conflict', 40, '[\"debt_ratio\"]', '2025-12-31', '{}', "
        f"'{status}', '1:conflict:2025-12-31', '2026-10-07 01:00:00', 0)"
    )


def test_upgrade_from_018_constraints_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from018.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "018")
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
        _request(conn, "done")
        _request(conn, "done")  # closed requests never collide
        _request(conn, "pending")
        with pytest.raises(sqlite3.IntegrityError):
            _request(conn, "deferred")  # one open request per dedup key
        conn.execute(
            "INSERT INTO ifind_call_ledger (at, endpoint, ticker_count, outcome, source) "
            "VALUES ('2026-10-07 01:00:00', '/stock/info', 3, 'ok', 'verification')"
        )
    assert _diffs(db_path) == []

    command.downgrade(config, "018")
    assert not set(TABLES) & _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM listing").fetchone() == (1,)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert set(TABLES) <= _tables(db_path)
    assert _diffs(db_path) == []

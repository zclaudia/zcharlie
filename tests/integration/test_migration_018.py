"""Migration 018 (candidate + decision): upgrade from 017, one open candidate
per listing, fresh head matches the models, downgrade restores 017."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models import candidates, rules  # noqa: F401
from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = ("candidate", "decision")


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
    return [d for d in diffs if any(f"'{table}'" in repr(d) for table in TABLES)]


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _candidate(conn: sqlite3.Connection, status: str) -> None:
    conn.execute(
        "INSERT INTO candidate (listing_id, ticker, status, opened_at, expires_at, sources, "
        "draft_state) VALUES (1, '600900.SH', ?, '2026-10-07 00:00:00', "
        "'2026-10-21 00:00:00', '[]', 'none')",
        (status,),
    )


def test_upgrade_from_017_constraints_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from017.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "017")
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
        _candidate(conn, "open")
        _candidate(conn, "dismissed")
        _candidate(conn, "expired")
        with pytest.raises(sqlite3.IntegrityError):
            _candidate(conn, "open")  # at most one open candidate per listing
        conn.execute(
            "INSERT INTO decision (listing_id, ticker, kind, candidate_id, context, actor, at) "
            "VALUES (1, '600900.SH', 'nominate', 1, '{}', 'user', '2026-10-07 00:00:00')"
        )
    assert _diffs(db_path) == []

    command.downgrade(config, "017")
    assert not set(TABLES) & _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM listing").fetchone() == (1,)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert set(TABLES) <= _tables(db_path)
    assert _diffs(db_path) == []

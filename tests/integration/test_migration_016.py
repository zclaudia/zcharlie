"""Migration 016 (rule store + screen results): upgrade from 015, constraints,
fresh head matches the models, downgrade restores 015."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from investment_agent.storage.models import rules  # noqa: F401
from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = ("rule_template", "rule_instance", "screen_result")


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


def _seed_listing(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO market (code, name, timezone, currency) "
        "VALUES ('SSE', 'Shanghai Stock Exchange', 'Asia/Shanghai', 'CNY')"
    )
    conn.execute("INSERT INTO company (id, canonical_name) VALUES (1, '贵州茅台')")
    conn.execute(
        "INSERT INTO listing (id, company_id, market_code, ticker, is_primary) "
        "VALUES (1, 1, 'SSE', '600519', 1)"
    )


def _instance(conn: sqlite3.Connection, version: int, status: str) -> None:
    conn.execute(
        "INSERT INTO rule_instance (listing_id, symbol, scope, template_id, template_version, "
        "version, status, definition, hash, drafted_by, draft_rationale) VALUES "
        f"(1, '600519.SH', 'candidate', 'quality_growth', 1, {version}, '{status}', '{{}}', "
        "'h', 'llm', '')"
    )


def test_upgrade_from_015_constraints_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from015.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "015")
    with sqlite3.connect(db_path) as conn:
        _seed_listing(conn)
    assert not set(TABLES) & _tables(db_path)

    command.upgrade(config, "head")
    assert set(TABLES) <= _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO rule_template (id, version, name, definition, hash, calibration_status)"
            " VALUES ('quality_growth', 1, 'QG', '{}', 'h', 'uncalibrated')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rule_template (id, version, name, definition, hash, "
                "calibration_status) VALUES ('quality_growth', 1, 'QG', '{}', 'h2', 'uncalibrated')"
            )
        _instance(conn, 1, "active")
        _instance(conn, 2, "draft")
        _instance(conn, 3, "retired")
        with pytest.raises(sqlite3.IntegrityError):
            _instance(conn, 4, "active")  # one active per (listing, scope)
        with pytest.raises(sqlite3.IntegrityError):
            _instance(conn, 2, "retired")  # version unique per (listing, scope)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rule_instance (listing_id, symbol, scope, template_id, "
                "template_version, version, status, definition, hash, drafted_by, "
                "draft_rationale) VALUES (1, '600519.SH', 'holding', 'quality_growth', 9, 1, "
                "'draft', '{}', 'h', 'llm', '')"
            )  # unknown template version
        conn.execute(
            "INSERT INTO pipeline_run (kind, triggered_by, started_at, status, stage, "
            "attempt_count, max_attempts, request_json, metadata_json) VALUES "
            "('screen_run', 'test', '2026-10-04 12:00:00', 'success', 'completed', 1, 3, "
            "'{}', '{}')"
        )
        conn.execute(
            "INSERT INTO screen_result (screen_run_id, as_of, template_id, template_version, "
            "listing_id, ticker, outcome, hit, rank, strength, reasons) VALUES "
            "(1, '2026-09-30', 'quality_growth', 1, 1, '600519.SH', 'hit', 1, 1, 0.5, '[]')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO screen_result (screen_run_id, as_of, template_id, "
                "template_version, listing_id, ticker, outcome, hit, reasons) VALUES "
                "(1, '2026-09-30', 'quality_growth', 1, 1, '600519.SH', 'miss', 0, '[]')"
            )
    assert _diffs(db_path) == []

    command.downgrade(config, "015")
    assert not set(TABLES) & _tables(db_path)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM listing").fetchone() == (1,)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert set(TABLES) <= _tables(db_path)
    assert _diffs(db_path) == []

"""Migration 048 (peer snapshots): upgrade from 047 creates the two immutable
tables with their named unique constraints, indexes and immutability triggers,
adds the nullable ``research_snapshot.industry_peer_context`` column, downgrade
restores 047, and a pre-migration backup restores and re-applies cleanly."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = ("peer_cohort_snapshot", "peer_annual_snapshot")
PREVIOUS_REVISION = "047"

COHORT_INSERT = (
    "INSERT INTO peer_cohort_snapshot (id, cohort_key, revision, observed_on, acquired_at, "
    "verification, inclusion_rule_version, content_hash, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
COHORT_ROW = (
    "0b7b8f2e-0000-4000-8000-000000000001",
    "baijiu_current",
    1,
    "2026-09-30",
    "2026-10-12 03:30:00",
    "verified",
    "run-02@2026-10-12",
    "a" * 64,
    '{"entries": []}',
)
ANNUAL_INSERT = (
    "INSERT INTO peer_annual_snapshot (id, company_id, listing_id, report_period, provider, "
    "adapter_version, captured_at, content_hash, semantic_hash, fields, verification) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
ANNUAL_ROW = (
    "0b7b8f2e-0000-4000-8000-000000000002",
    7,
    11,
    "2025-12-31",
    "eastmoney",
    "annual-1",
    "2026-10-12 03:30:00",
    "b" * 64,
    "c" * 64,
    "{}",
    "{}",
)
RESEARCH_SNAPSHOT_INSERT = (
    "INSERT INTO research_snapshot (pipeline_run_id, symbol, market, company_name, industry, "
    "as_of, report_year, canonical_schema_version, scoring_version, score_schema_version, "
    "score_json, explanation_json, artifact_manifest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
RESEARCH_ROW = (
    1,
    "600809.SH",
    "SSE",
    "山西汾酒",
    "白酒",
    "2026-10-12",
    2025,
    "v1",
    "v1",
    "v1",
    '{"items": {}}',
    None,
    "{}",
)


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
    return [d for d in diffs if any(table in repr(d) for table in (*TABLES, "research_snapshot"))]


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db_path: Path, table: str) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _constraints(db_path: Path, table: str) -> tuple[set[str], set[str]]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        uniques = {item["name"] for item in inspector.get_unique_constraints(table)}
        indexes = {item["name"] for item in inspector.get_indexes(table)}
    finally:
        engine.dispose()
    return uniques, indexes


def test_revision_048_follows_047() -> None:
    script = ScriptDirectory(str(REPO_ROOT / "alembic"))
    assert script.get_revision("048").down_revision == PREVIOUS_REVISION


def test_upgrade_from_047_creates_the_tables_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from047.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, PREVIOUS_REVISION)
    assert not set(TABLES) & _tables(db_path)
    assert "industry_peer_context" not in _columns(db_path, "research_snapshot")

    command.upgrade(config, "head")
    assert set(TABLES) <= _tables(db_path)

    assert {
        "id", "cohort_key", "revision", "observed_on", "acquired_at", "verification",
        "inclusion_rule_version", "content_hash", "payload", "created_at", "updated_at",
    } <= _columns(db_path, "peer_cohort_snapshot")
    cohort_uniques, cohort_indexes = _constraints(db_path, "peer_cohort_snapshot")
    assert "uq_peer_cohort_snapshot_revision" in cohort_uniques
    assert "ix_peer_cohort_snapshot_lookup" in cohort_indexes

    assert {
        "id", "company_id", "listing_id", "report_period", "provider", "adapter_version",
        "captured_at", "content_hash", "semantic_hash", "fields", "verification",
        "created_at", "updated_at",
    } <= _columns(db_path, "peer_annual_snapshot")
    annual_uniques, annual_indexes = _constraints(db_path, "peer_annual_snapshot")
    assert "uq_peer_annual_snapshot_version" in annual_uniques
    assert {"ix_peer_annual_snapshot_lookup", "ix_peer_annual_snapshot_captured"} <= annual_indexes

    assert "industry_peer_context" in _columns(db_path, "research_snapshot")
    with sqlite3.connect(db_path) as conn:
        # Old research rows keep a null binding.
        conn.execute(RESEARCH_SNAPSHOT_INSERT, RESEARCH_ROW)
        assert conn.execute(
            "SELECT industry_peer_context FROM research_snapshot"
        ).fetchall() == [(None,)]

        conn.execute(COHORT_INSERT, COHORT_ROW)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(COHORT_INSERT, COHORT_ROW)
        # A roster change appends the next revision (new UUID) instead of mutating.
        next_roster = ("0b7b8f2e-0000-4000-8000-0000000000aa", *COHORT_ROW[1:2], 2, *COHORT_ROW[3:])
        conn.execute(COHORT_INSERT, next_roster)

        conn.execute(ANNUAL_INSERT, ANNUAL_ROW)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(ANNUAL_INSERT, ANNUAL_ROW)
        # A different semantic hash is a new version (new UUID) of the same identity.
        revised = ("0b7b8f2e-0000-4000-8000-0000000000bb", *ANNUAL_ROW[1:8], "f" * 64, *ANNUAL_ROW[9:])
        conn.execute(ANNUAL_INSERT, revised)

        for table in TABLES:
            for sql in (
                f"UPDATE {table} SET payload='{{}}'" if table == "peer_cohort_snapshot"
                else f"UPDATE {table} SET fields='{{}}'",
                f"DELETE FROM {table}",
            ):
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    conn.execute(sql)
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
                )
    assert _diffs(db_path) == []

    command.downgrade(config, PREVIOUS_REVISION)
    assert not set(TABLES) & _tables(db_path)
    assert "industry_peer_context" not in _columns(db_path, "research_snapshot")
    with sqlite3.connect(db_path) as conn:
        # The restored schema keeps working for existing writers.
        conn.execute(RESEARCH_SNAPSHOT_INSERT, (2, *RESEARCH_ROW[1:]))
        assert conn.execute("SELECT count(*) FROM research_snapshot").fetchone() == (2,)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert set(TABLES) <= _tables(db_path)
    assert _diffs(db_path) == []


def test_pre_migration_backup_restores_and_reapplies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first-phase rollback path: restore the pre-migration backup, then the
    upgrade succeeds again from the restored database."""
    db_path = tmp_path / "restore.db"
    backup_path = tmp_path / "backup.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(db_path) as conn:
        conn.execute(RESEARCH_SNAPSHOT_INSERT, RESEARCH_ROW)
    shutil.copyfile(db_path, backup_path)

    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT industry_peer_context FROM research_snapshot"
        ).fetchall() == [(None,)]
        conn.execute(COHORT_INSERT, COHORT_ROW)
        conn.execute(ANNUAL_INSERT, ANNUAL_ROW)

    # Restoring the backup removes the new tables and rows with the database.
    shutil.copyfile(backup_path, db_path)
    assert not set(TABLES) & _tables(db_path)
    command.upgrade(config, "head")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT industry_peer_context FROM research_snapshot"
        ).fetchall() == [(None,)]
        assert conn.execute("SELECT count(*) FROM peer_cohort_snapshot").fetchone() == (0,)
        conn.execute(COHORT_INSERT, COHORT_ROW)
        conn.execute(ANNUAL_INSERT, ANNUAL_ROW)

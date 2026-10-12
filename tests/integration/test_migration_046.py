"""Migration 046 (research material): upgrade from 045 creates the two tables
with their named unique constraints and index, fresh head matches the models,
downgrade restores 045, and revision 046 follows 045."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from investment_agent.storage.models import research_material  # noqa: F401
from investment_agent.storage.models.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLES = ("research_source_binding", "research_dataset")
PREVIOUS_REVISION = "045"

DATASET_INSERT = (
    "INSERT INTO research_dataset (kind, subject_kind, subject_id, source, schema_version, "
    "adapter_version, period_end, period_basis, revision, previous_id, content_hash, captured_at, "
    "payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
BINDING_INSERT = (
    "INSERT INTO research_source_binding (subject_kind, subject_id, source, source_symbol, "
    "valid_from, valid_to, verified_at, verified_by, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

ANNUAL_VERSION = (
    "annual_facts",
    "company",
    7,
    "eastmoney_hk",
    1,
    "hk-native-1",
    "2024-09-29",
    "annual",
    1,
    None,
    "a" * 64,
    "2026-10-12 03:30:00",
    '{"facts": []}',
)
FILING_VERSION = (
    "filing_index",
    "company",
    7,
    "sec",
    1,
    "sec-1",
    None,
    None,
    1,
    None,
    "c" * 64,
    "2026-10-12 03:30:00",
    '{"facts": []}',
)
BINDING_ROW = (
    "company",
    7,
    "eastmoney_hk",
    "00700.HK",
    "2025-01-01",
    None,
    "2026-10-12 03:30:00",
    "pilot_list@2026-10-12",
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
    return [d for d in diffs if any(table in repr(d) for table in TABLES)]


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


def test_revision_046_follows_045() -> None:
    script = ScriptDirectory(str(REPO_ROOT / "alembic"))
    assert script.get_revision("046").down_revision == PREVIOUS_REVISION


def test_upgrade_from_024_creates_the_tables_and_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "from024.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, PREVIOUS_REVISION)
    assert not set(TABLES) & _tables(db_path)

    command.upgrade(config, "head")
    assert set(TABLES) <= _tables(db_path)

    assert {
        "id",
        "kind",
        "subject_kind",
        "subject_id",
        "source",
        "schema_version",
        "adapter_version",
        "period_end",
        "period_basis",
        "revision",
        "previous_id",
        "content_hash",
        "captured_at",
        "source_fetched_at",
        "raw_ref",
        "payload",
        "created_at",
        "updated_at",
    } <= _columns(db_path, "research_dataset")
    uniques, indexes = _constraints(db_path, "research_dataset")
    assert "uq_research_dataset_version" in uniques
    assert "ix_research_dataset_lookup" in indexes

    assert {
        "id",
        "subject_kind",
        "subject_id",
        "source",
        "source_symbol",
        "valid_from",
        "valid_to",
        "verified_at",
        "verified_by",
        "payload",
        "created_at",
        "updated_at",
    } <= _columns(db_path, "research_source_binding")
    binding_uniques, _ = _constraints(db_path, "research_source_binding")
    assert "uq_research_source_binding_identity" in binding_uniques

    with sqlite3.connect(db_path) as conn:
        conn.execute(DATASET_INSERT, ANNUAL_VERSION)
        with pytest.raises(sqlite3.IntegrityError):  # same key + hash is one version
            conn.execute(DATASET_INSERT, ANNUAL_VERSION)
        second = (*ANNUAL_VERSION[:10], "b" * 64, ANNUAL_VERSION[11], ANNUAL_VERSION[12])
        conn.execute(DATASET_INSERT, second)
        conn.execute(DATASET_INSERT, FILING_VERSION)  # null period is a legal key
        assert conn.execute("SELECT count(*) FROM research_dataset").fetchone() == (3,)
        conn.execute(BINDING_INSERT, BINDING_ROW)
        with pytest.raises(sqlite3.IntegrityError):  # same binding identity
            conn.execute(BINDING_INSERT, BINDING_ROW)
        assert conn.execute("SELECT count(*) FROM research_source_binding").fetchone() == (1,)
    assert _diffs(db_path) == []

    command.downgrade(config, PREVIOUS_REVISION)
    assert not set(TABLES) & _tables(db_path)


def test_fresh_head_matches_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert set(TABLES) <= _tables(db_path)
    assert _diffs(db_path) == []

"""Migration 049 adds append-only acquisition observations without touching datasets."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from investment_agent.storage.models import Base
from tests.integration.test_migration_046 import ANNUAL_VERSION, DATASET_INSERT, _config

REPO_ROOT = Path(__file__).resolve().parents[2]
TABLE = "research_material_acquisition"
ACQUISITION_INSERT = (
    "INSERT INTO research_material_acquisition (source, subject_id, adapter_version, captured_at, "
    "schema_version, identity_snapshot, periods, expected_requests, problems, archive_ref, "
    "manifest_sha256, dataset_links) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
ACQUISITION_ROW = (
    "sec.companyfacts.v1", 7, "1", "2026-10-12 03:30:00", 1,
    '{"company_key": "acme"}', '[{"end": "2025-12-31"}]',
    '["companyfacts", "submissions"]', '["one partial response"]',
    "research-material/archive/sec.companyfacts.v1/manifests/aa/" + "a" * 64 + ".json",
    "a" * 64, '[{"id": 1, "revision": 1, "content_hash": "b"' + "b" * 63 + '"}]',
)


def _metadata_diffs(db_path: Path) -> list[object]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    return [diff for diff in diffs if TABLE in repr(diff)]


def test_revision_049_follows_048() -> None:
    script = ScriptDirectory(str(REPO_ROOT / "alembic"))
    assert script.get_revision("049").down_revision == "048"


def test_upgrade_preserves_datasets_and_guards_acquisitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "material.db"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, "048")
    with sqlite3.connect(db_path) as connection:
        connection.execute(DATASET_INSERT, ANNUAL_VERSION)
        before = connection.execute("SELECT * FROM research_dataset").fetchall()
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns(TABLE)}
        uniques = {item["name"] for item in inspector.get_unique_constraints(TABLE)}
        indexes = {item["name"] for item in inspector.get_indexes(TABLE)}
    finally:
        engine.dispose()
    assert {
        "id", "source", "subject_id", "adapter_version", "captured_at", "schema_version",
        "identity_snapshot", "periods", "expected_requests", "problems", "archive_ref",
        "manifest_sha256", "dataset_links", "created_at", "updated_at",
    } <= columns
    assert "uq_material_acquisition_manifest" in uniques
    assert "ix_material_acquisition_subject_source" in indexes

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM research_dataset").fetchall() == before
        connection.execute(ACQUISITION_INSERT, ACQUISITION_ROW)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(ACQUISITION_INSERT, ACQUISITION_ROW)
        # A second observation is allowed when the archive evidence differs.
        revised = (*ACQUISITION_ROW[:10], "c" * 64, *ACQUISITION_ROW[11:])
        connection.execute(ACQUISITION_INSERT, revised)
        assert connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone() == (2,)
        for sql in (
            f"UPDATE {TABLE} SET problems='[]'",
            f"DELETE FROM {TABLE}",
            f"INSERT OR REPLACE INTO {TABLE} SELECT * FROM {TABLE}",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(ACQUISITION_INSERT.replace("INSERT INTO", "INSERT OR REPLACE INTO"), ACQUISITION_ROW)
    assert _metadata_diffs(db_path) == []

    command.downgrade(config, "048")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM research_dataset").fetchone() == (1,)
        assert TABLE not in {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_fresh_head_matches_model_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_config(db_path, monkeypatch), "head")
    assert _metadata_diffs(db_path) == []

"""PT-004 schema/upgrade checks on isolated SQLite, retaining prior bank data."""

import shutil
import sqlite3
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Table, create_engine
from tests.support.next_iterations_migrations import migration_config, table_schema

from investment_agent.storage.models import Base
from investment_agent.storage.models.portfolio_stress import (
    PortfolioStressHead,
    PortfolioStressRevision,
    PortfolioStudyOperation,
)

TABLES = [cast(Table, model.__table__) for model in
          (PortfolioStressHead, PortfolioStressRevision, PortfolioStudyOperation)]


@pytest.fixture(scope="module")
def previous_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("stress-migration-base") / "prior.db"
    with migration_config(database) as config:
        command.upgrade(config, "063")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO topic_case (id,owner_id,listing_id,template_key,base_period,head_revision,control_revision,state,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(UUID(int=1)), "local_owner", 18, "bank_nii_sustainability_v1", "2025-12-31", 1, 1, "draft", "2026-11-07 00:00:00"),
        )
    return database


@pytest.fixture
def migrated(previous_database: Path, tmp_path: Path) -> Path:
    database = tmp_path / "stress.db"
    shutil.copy2(previous_database, database)
    with migration_config(database) as config:
        command.upgrade(config, "064")
    return database


def triggers(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {row[0] for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name IN ('portfolio_stress_revision','portfolio_study_operation')"
        )}


def test_pristine_upgrade_remains_one_chain(tmp_path: Path) -> None:
    database = tmp_path / "fresh-stress.db"
    with migration_config(database) as config:
        script = ScriptDirectory.from_config(config)
        assert len(script.get_heads()) == 1
        assert script.get_revision("064").down_revision == "063"
        command.upgrade(config, "064")
    assert len(table_schema(database, TABLES)) == 3
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_migrated_schema_and_guards_match_models_and_retain_bank_data(migrated: Path, tmp_path: Path) -> None:
    model_db = tmp_path / "model-stress.db"
    engine = create_engine(f"sqlite:///{model_db}")
    try:
        Base.metadata.create_all(engine, tables=TABLES)
    finally:
        engine.dispose()
    assert table_schema(migrated, TABLES) == table_schema(model_db, TABLES)
    assert triggers(migrated) == triggers(model_db)
    assert len(triggers(migrated)) == 6
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT state FROM topic_case").fetchone() == ("draft",)
        for table in ("portfolio_stress_revision", "portfolio_study_operation"):
            definition = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
            assert "WITHOUT ROWID" in definition.upper()
            with pytest.raises(sqlite3.OperationalError, match="rowid"):
                connection.execute(f"SELECT rowid FROM {table}")


def test_empty_downgrade_and_reupgrade_keep_prior_study(migrated: Path) -> None:
    with migration_config(migrated) as config:
        command.downgrade(config, "063")
        with sqlite3.connect(migrated) as connection:
            assert connection.execute("SELECT COUNT(*) FROM topic_case").fetchone() == (1,)
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='portfolio_stress_head'").fetchone() is None
        command.upgrade(config, "064")
    assert len(triggers(migrated)) == 6


def test_populated_operation_cannot_be_dropped_or_replaced(migrated: Path) -> None:
    with sqlite3.connect(migrated) as connection:
        connection.execute(
            "INSERT INTO portfolio_study_operation (owner_id,operation_id,action,request_hash,response_payload,created_at) VALUES (?,?,?,?,?,?)",
            ("local_owner", str(UUID(int=2)), "create_stress", "a" * 64, '{"saved":true}', "2026-11-07 00:00:00"),
        )
    with migration_config(migrated) as config, pytest.raises(RuntimeError, match="history must be retained"):
        command.downgrade(config, "063")
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("064",)
        for sql in ("UPDATE portfolio_study_operation SET action='changed'",
                    "DELETE FROM portfolio_study_operation",
                    "INSERT OR REPLACE INTO portfolio_study_operation SELECT * FROM portfolio_study_operation"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
        assert connection.execute("SELECT action FROM portfolio_study_operation").fetchone() == ("create_stress",)

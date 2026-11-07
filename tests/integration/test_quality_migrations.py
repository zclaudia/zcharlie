"""QT-007/008 actual SQLite upgrades, narrow guards and lossless rollback gates."""

import shutil
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Table, create_engine

from investment_agent.storage.models import Base
from investment_agent.storage.models.investor import MethodArtifact
from investment_agent.storage.models.research_quality import (
    ResearchAssessment,
    ResearchExperiment,
    ResearchExperimentReceipt,
    ResearchExperimentTask,
    ResearchTrial,
)
from tests.support.next_iterations_migrations import migration_config, table_schema

TABLES = [cast(Table, model.__table__) for model in (
    ResearchExperiment, ResearchTrial, ResearchExperimentTask,
    ResearchAssessment, ResearchExperimentReceipt,
)]
KINDS = ("quality_case", "quality_envelope", "quality_spec", "quality_result",
         "quality_report", "quality_blind_map")


@pytest.fixture(scope="module")
def prior_database(tmp_path_factory):
    database = tmp_path_factory.mktemp("quality-migration") / "prior.db"
    with migration_config(database) as config:
        command.upgrade(config, "064")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO investor_method_artifact (id,owner_id,kind,payload) VALUES ('old','local_owner','method_version','{}')")
    return database


@pytest.fixture
def migrated(prior_database, tmp_path):
    database = tmp_path / "quality.db"
    shutil.copy2(prior_database, database)
    with migration_config(database) as config:
        command.upgrade(config, "065")
    return database


def triggers(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {row[0] for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND (name LIKE 'trg_quality_artifact_%' OR tbl_name IN ('research_experiment_task','research_assessment','research_experiment_receipt'))"
        )}


def test_fresh_upgrade_and_migrated_schema_match_models(migrated, tmp_path):
    with migration_config(migrated) as config:
        script = ScriptDirectory.from_config(config)
        assert len(script.get_heads()) == 1
        assert script.get_revision("065").down_revision == "064"
    model_db = tmp_path / "model.db"
    engine = create_engine(f"sqlite:///{model_db}")
    try:
        Base.metadata.create_all(engine, tables=[cast(Table, MethodArtifact.__table__), *TABLES])
    finally:
        engine.dispose()
    assert table_schema(migrated, TABLES) == table_schema(model_db, TABLES)
    assert triggers(migrated) == triggers(model_db)
    assert len(triggers(migrated)) == 13
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT kind FROM investor_method_artifact WHERE id='old'").fetchone() == ("method_version",)
        for table in ("research_experiment_task", "research_assessment", "research_experiment_receipt"):
            with pytest.raises(sqlite3.OperationalError, match="rowid"):
                connection.execute(f"SELECT rowid FROM {table}")


def test_empty_downgrade_reupgrade_preserves_original_artifacts(migrated):
    with migration_config(migrated) as config:
        command.downgrade(config, "064")
        assert not triggers(migrated)
        command.upgrade(config, "065")
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT kind FROM investor_method_artifact WHERE id='old'").fetchone() == ("method_version",)
    assert len(triggers(migrated)) == 13


@pytest.mark.parametrize("kind", KINDS)
def test_artifact_guards_protect_all_quality_kinds_and_rowid_without_changing_old_kinds(migrated, kind):
    with sqlite3.connect(migrated) as connection:
        connection.execute("PRAGMA recursive_triggers=OFF")
        connection.execute("INSERT INTO investor_method_artifact (id,owner_id,kind,payload) VALUES ('quality','local_owner',?,'{}')", (kind,))
        quality_rowid = connection.execute("SELECT rowid FROM investor_method_artifact WHERE id='quality'").fetchone()[0]
        old_rowid = connection.execute("SELECT rowid FROM investor_method_artifact WHERE id='old'").fetchone()[0]
        invalid = [
            ("UPDATE investor_method_artifact SET payload='{}' WHERE id='quality'", ()),
            ("UPDATE investor_method_artifact SET kind='method_version' WHERE id='quality'", ()),
            ("UPDATE investor_method_artifact SET kind=? WHERE id='old'", (kind,)),
            ("DELETE FROM investor_method_artifact WHERE id='quality'", ()),
            ("INSERT OR REPLACE INTO investor_method_artifact (id,owner_id,kind,payload) VALUES ('quality','local_owner','method_version','{}')", ()),
            ("INSERT OR REPLACE INTO investor_method_artifact (id,owner_id,kind,payload) VALUES ('old','local_owner',?,'{}')", (kind,)),
            ("INSERT OR REPLACE INTO investor_method_artifact (rowid,id,owner_id,kind,payload) VALUES (?,'different','local_owner','method_version','{}')", (quality_rowid,)),
            ("INSERT OR REPLACE INTO investor_method_artifact (rowid,id,owner_id,kind,payload) VALUES (?,'different','local_owner',?,'{}')", (old_rowid, kind)),
            ("UPDATE OR REPLACE investor_method_artifact SET id='quality' WHERE id='old'", ()),
            ("UPDATE OR REPLACE investor_method_artifact SET rowid=? WHERE id='old'", (quality_rowid,)),
        ]
        for sql, params in invalid:
            with pytest.raises(sqlite3.IntegrityError, match="quality"):
                connection.execute(sql, params)
        # The guard intentionally does not broaden the old SQL-level contract.
        connection.execute("UPDATE investor_method_artifact SET payload='{\"old\":true}' WHERE id='old'")
        connection.execute("INSERT OR REPLACE INTO investor_method_artifact (id,owner_id,kind,payload) VALUES ('old','local_owner','method_version','{}')")
        assert connection.execute("SELECT kind FROM investor_method_artifact WHERE id='quality'").fetchone() == (kind,)
    with migration_config(migrated) as config, pytest.raises(RuntimeError, match="artifacts must be retained"):
        command.downgrade(config, "064")
    assert len(triggers(migrated)) == 13


def test_history_prevents_destructive_downgrade(migrated):
    with sqlite3.connect(migrated) as connection:
        connection.execute("INSERT INTO research_experiment (id,owner_id,revision,state,created_at) VALUES ('experiment','local_owner',1,'draft','2026-11-07')")
    with migration_config(migrated) as config, pytest.raises(RuntimeError, match="history must be retained"):
        command.downgrade(config, "064")
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("065",)
        assert connection.execute("SELECT state FROM research_experiment").fetchone() == ("draft",)

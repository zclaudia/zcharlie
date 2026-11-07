"""BT-007 real sequential migrations on temporary SQLite; retain all old data."""

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
from investment_agent.storage.models.bank_topics import (
    TopicCase,
    TopicCheckRevision,
    TopicConditionHead,
    TopicConditionRevision,
    TopicFactRevision,
    TopicMutationReceipt,
    TopicRevision,
)

TABLES = [cast(Table, model.__table__) for model in (
    TopicCase, TopicRevision, TopicFactRevision, TopicMutationReceipt,
    TopicConditionRevision, TopicConditionHead, TopicCheckRevision,
)]
HISTORY = ("topic_revision", "topic_fact_revision", "topic_mutation_receipt",
           "topic_condition_revision", "topic_check_revision")


@pytest.fixture(scope="module")
def previous_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("bank-migration-base") / "prior.db"
    with migration_config(database) as config:
        command.upgrade(config, "062")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO projection_cursor (consumer,updated_at) VALUES (?,?)",
                           ("watchlist_sync.v1", "2026-11-07 00:00:00"))
        connection.execute("INSERT INTO app_setting (key,value_json,updated_at,updated_by) VALUES (?,?,?,?)",
                           ("bank-migration-probe", '"retained"', "2026-11-07 00:00:00", "fixture"))
    return database


@pytest.fixture
def migrated(previous_database: Path, tmp_path: Path) -> Path:
    database = tmp_path / "bank.db"
    shutil.copy2(previous_database, database)
    with migration_config(database) as config:
        command.upgrade(config, "063")
    return database


def trigger_sql(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {row[0] for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name LIKE 'topic_%'"
        )}


def test_pristine_upgrade_has_one_chain_and_seven_tables(tmp_path: Path) -> None:
    database = tmp_path / "fresh-bank.db"
    with migration_config(database) as config:
        script = ScriptDirectory.from_config(config)
        assert len(script.get_heads()) == 1
        assert script.get_revision("063").down_revision == "062"
        command.upgrade(config, "063")
    assert len(table_schema(database, TABLES)) == 7
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_old_rows_remain_and_schema_and_guards_match_models(migrated: Path, tmp_path: Path) -> None:
    model_db = tmp_path / "model-bank.db"
    engine = create_engine(f"sqlite:///{model_db}")
    try:
        Base.metadata.create_all(engine, tables=TABLES)
    finally:
        engine.dispose()
    assert table_schema(migrated, TABLES) == table_schema(model_db, TABLES)
    assert trigger_sql(migrated) == trigger_sql(model_db)
    assert len(trigger_sql(migrated)) == 15
    with sqlite3.connect(migrated) as connection:
        for table in HISTORY:
            definition = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            assert "WITHOUT ROWID" in definition.upper()
            with pytest.raises(sqlite3.OperationalError, match="rowid"):
                connection.execute(f"SELECT rowid FROM {table}")
        assert connection.execute("SELECT value_json FROM app_setting WHERE key='bank-migration-probe'").fetchone() == ('"retained"',)
        assert connection.execute("SELECT revision FROM projection_cursor WHERE consumer='watchlist_sync.v1'").fetchone() == (1,)


def test_empty_bank_downgrade_keeps_event_data_and_can_reupgrade(migrated: Path) -> None:
    with migration_config(migrated) as config:
        command.downgrade(config, "062")
        with sqlite3.connect(migrated) as connection:
            assert connection.execute("SELECT COUNT(*) FROM projection_cursor").fetchone() == (1,)
            assert connection.execute("SELECT name FROM sqlite_master WHERE name='topic_case'").fetchone() is None
        command.upgrade(config, "063")
    assert len(trigger_sql(migrated)) == 15


def test_receipt_history_blocks_downgrade_and_sql_mutation(migrated: Path) -> None:
    with sqlite3.connect(migrated) as connection:
        connection.execute(
            "INSERT INTO topic_mutation_receipt (owner_id,mutation_id,route,object_identity,request_hash,response,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("local_owner", str(UUID(int=1)), "create", "fixture", "a" * 64,
             '{"saved":true}', 201, "2026-11-07 00:00:00"),
        )
    with migration_config(migrated) as config, pytest.raises(RuntimeError, match="history must be retained"):
        command.downgrade(config, "062")
    with sqlite3.connect(migrated) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("063",)
        for sql in ("UPDATE topic_mutation_receipt SET status=200",
                    "DELETE FROM topic_mutation_receipt",
                    "INSERT OR REPLACE INTO topic_mutation_receipt SELECT * FROM topic_mutation_receipt"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
        assert connection.execute("SELECT status FROM topic_mutation_receipt").fetchone() == (201,)
        assert connection.execute("SELECT COUNT(*) FROM projection_cursor").fetchone() == (1,)

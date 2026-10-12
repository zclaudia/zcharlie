"""Existing 046 databases gain SQL immutability, without losing any rows."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from tests.integration.test_migration_046 import (
    ANNUAL_VERSION,
    BINDING_INSERT,
    BINDING_ROW,
    DATASET_INSERT,
    _config,
)


def test_upgrade_047_guards_existing_rows_and_downgrade_removes_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "material.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "046")
    with sqlite3.connect(database) as connection:
        connection.execute(DATASET_INSERT, ANNUAL_VERSION)
        connection.execute(BINDING_INSERT, BINDING_ROW)
    command.upgrade(config, "047")
    with sqlite3.connect(database) as connection:
        for table in ("research_dataset", "research_source_binding"):
            for sql in (f"UPDATE {table} SET payload='{{}}'", f"DELETE FROM {table}",
                        f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    connection.execute(sql)
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
        # A REPLACE on a unique key, without retaining the original primary id,
        # must also be blocked (SQLite defaults recursive_triggers to OFF).
        for statement, values in ((DATASET_INSERT, ANNUAL_VERSION), (BINDING_INSERT, BINDING_ROW)):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement.replace("INSERT INTO", "INSERT OR REPLACE INTO"), values)
        next_version = (*ANNUAL_VERSION[:10], "b" * 64, *ANNUAL_VERSION[11:])
        connection.execute(DATASET_INSERT, next_version)
        assert connection.execute("SELECT count(*) FROM research_dataset").fetchone() == (2,)
    command.downgrade(config, "046")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE research_dataset SET payload='{}'")
        connection.execute("DELETE FROM research_source_binding")
        assert connection.execute("SELECT count(*) FROM research_source_binding").fetchone() == (0,)
    command.upgrade(config, "047")
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute("DELETE FROM research_dataset")

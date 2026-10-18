"""Migration 050's immutable account-fit and ranking storage constraints."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from investment_agent.storage.models import Base
from tests.integration.test_migration_046 import _config

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSESSMENT = "investment_assessment"
ENTRY = "candidate_ranking_entry"
ACCOUNT = "11111111-1111-4111-8111-111111111111"
RANKING = "22222222-2222-4222-8222-222222222222"
OTHER_RANKING = "33333333-3333-4333-8333-333333333333"
ZERO_RANKING = "66666666-6666-4666-8666-666666666666"
REPLACE_RANKING = "77777777-7777-4777-8777-777777777777"
AS_OF = "2026-10-16 00:00:00.000000"
HASH = "a" * 64
OTHER_HASH = "b" * 64


def _header_values(
    *,
    ident: str = RANKING,
    kind: str = "candidate_ranking",
    entry_count: int | None = 1,
    owner: str = "local_owner",
    account: str = ACCOUNT,
    input_hash: str = HASH,
    payload: str = "{}",
) -> tuple[object, ...]:
    listing_id = None if kind == "candidate_ranking" else 7
    count = entry_count if kind == "candidate_ranking" else None
    manifest = HASH if kind == "candidate_ranking" else None
    universe = OTHER_HASH if kind == "candidate_ranking" else None
    return (
        ident, 1, owner, kind, account, listing_id, AS_OF, input_hash, count,
        manifest, universe, payload,
    )


def _insert_header(connection: sqlite3.Connection, **kwargs: object) -> None:
    connection.execute(
        "INSERT INTO investment_assessment "
        "(id, revision, owner_id, kind, account_id, listing_id, as_of, input_hash, "
        "entry_count, manifest_hash, universe_hash, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _header_values(**kwargs),
    )


def _insert_entry(
    connection: sqlite3.Connection,
    *,
    ranking_id: str = RANKING,
    revision: int = 1,
    owner: str = "local_owner",
    account: str = ACCOUNT,
    listing_id: int = 10,
    ordinal: int = 0,
    payload: str = "{}",
) -> None:
    connection.execute(
        "INSERT INTO candidate_ranking_entry "
        "(ranking_id, revision, owner_id, account_id, listing_id, ordinal, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ranking_id, revision, owner, account, listing_id, ordinal, payload),
    )


def _metadata_diffs(db_path: Path) -> list[object]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()
    return [diff for diff in diffs if ASSESSMENT in repr(diff) or ENTRY in repr(diff)]


def test_revision_050_follows_049_and_current_head() -> None:
    script = ScriptDirectory(str(REPO_ROOT / "alembic"))
    assert script.get_revision("050").down_revision == "049"
    assert script.get_current_head() == "055"


def test_sqlite_off_guards_assessment_and_ranking_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "account-fit.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "049")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (0,)
        _insert_header(connection, entry_count=1)
        _insert_entry(connection)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_header(
                connection,
                ident="88888888-8888-4888-8888-888888888888",
                payload="not-json",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_entry(connection, listing_id=11, ordinal=1, payload="not-json")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_header(
                connection,
                ident="99999999-9999-4999-8999-999999999999",
                payload="[]",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_entry(connection, listing_id=12, ordinal=1, payload="[]")

        for statement in (
            f"UPDATE {ASSESSMENT} SET payload='{{}}'",
            f"DELETE FROM {ASSESSMENT}",
            f"UPDATE {ENTRY} SET ordinal=1",
            f"DELETE FROM {ENTRY}",
            f"INSERT OR REPLACE INTO {ENTRY} SELECT * FROM {ENTRY}",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)

        # Equal hashes may belong to separate operations, but REPLACE of the
        # same immutable UUID is always blocked.
        _insert_header(connection, ident=OTHER_RANKING)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "INSERT OR REPLACE INTO investment_assessment "
                "SELECT * FROM investment_assessment WHERE id=?", (RANKING,),
            )

        _insert_header(connection, ident=REPLACE_RANKING, entry_count=2, input_hash="d" * 64)
        _insert_entry(connection, ranking_id=REPLACE_RANKING, listing_id=20, ordinal=0)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_entry(connection, ranking_id=REPLACE_RANKING, listing_id=21, ordinal=0)
        _insert_entry(connection, ranking_id=REPLACE_RANKING, listing_id=21, ordinal=1)

        # Completed snapshots cannot accept another member, even with a new id.
        with pytest.raises(sqlite3.IntegrityError, match="invalid or complete"):
            _insert_entry(connection, listing_id=11)

        # The zero-member snapshot is a valid complete result with no entries.
        _insert_header(
            connection,
            ident=ZERO_RANKING,
            entry_count=0,
            input_hash=OTHER_HASH,
        )
        assert connection.execute(
            f"SELECT entry_count FROM {ASSESSMENT} WHERE id=?", (ZERO_RANKING,),
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="invalid or complete"):
            _insert_entry(connection, ranking_id=ZERO_RANKING, listing_id=12)

        # Orphans, wrong-kind parents, owner/account mismatches and bad revisions
        # are blocked by the table-specific trigger while foreign_keys stays off.
        with pytest.raises(sqlite3.IntegrityError, match="invalid or complete"):
            _insert_entry(connection, ranking_id="44444444-4444-4444-8444-444444444444")
        _insert_header(
            connection,
            ident="55555555-5555-4555-8555-555555555555",
            kind="account_fit",
            entry_count=None,
            input_hash="c" * 64,
        )
        with pytest.raises(sqlite3.IntegrityError, match="invalid or complete"):
            _insert_entry(connection, ranking_id="55555555-5555-4555-8555-555555555555")
        with pytest.raises(sqlite3.IntegrityError, match="invalid or complete"):
            _insert_entry(connection, owner="someone_else")
        with pytest.raises(sqlite3.IntegrityError, match="invalid or complete"):
            _insert_entry(connection, account="99999999-9999-4999-8999-999999999999")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_entry(connection, revision=2)
        with pytest.raises(sqlite3.IntegrityError, match="invalid or complete"):
            _insert_entry(connection, listing_id=11, ordinal=1)

        assert connection.execute(f"SELECT count(*) FROM {ENTRY}").fetchone() == (3,)
        assert connection.execute(
            f"SELECT count(*) FROM {ASSESSMENT} WHERE kind='candidate_ranking'",
        ).fetchone() == (4,)

    assert _metadata_diffs(database) == []
    command.downgrade(config, "049")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM research_material_acquisition").fetchone() == (0,)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert ASSESSMENT not in tables and ENTRY not in tables
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert ASSESSMENT in tables and ENTRY in tables


def test_fresh_head_matches_registered_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "fresh.db"
    command.upgrade(_config(database, monkeypatch), "head")
    assert _metadata_diffs(database) == []

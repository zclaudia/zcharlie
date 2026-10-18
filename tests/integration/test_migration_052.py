"""Migration 052: rule-coverage assessment shape, bindings, and downgrade guards."""

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

ROOT = Path(__file__).resolve().parents[2]
AS_OF = "2026-10-17 00:00:00.000000"
ACCOUNT = "11111111-1111-4111-8111-111111111111"
RANKING = "22222222-2222-4222-8222-222222222222"
APPLICATION = "33333333-3333-4333-8333-333333333333"
TASK = "44444444-4444-4444-8444-444444444444"
HASH = "a" * 64

_GUARDED_TABLES = (
    "investment_assessment",
    "candidate_ranking_entry",
    "research_rule_binding",
)


def _metadata_diffs(path: Path) -> list[object]:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            return [
                diff for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
                if any(table in repr(diff) for table in _GUARDED_TABLES)
            ]
    finally:
        engine.dispose()


def _seed_p1_p2(database: Path) -> tuple[str, str, str]:
    """Seed one row per P1/P2 assessment kind plus one ranking entry."""

    payload = '{"as_of":"2026-10-17T00:00:00+00:00","preserve":"byte-for-byte"}'
    entry_payload = '{"ordinal":0,"preserve":"entry-bytes"}'
    application_payload = '{"synthetic":"rule application","frozen":true}'
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO investment_assessment "
            "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,payload) "
            "VALUES ('11111111-1111-4111-8111-111111111111',1,'local_owner','account_fit',?,7,?,?,NULL,NULL,NULL,?)",
            (ACCOUNT, AS_OF, HASH, payload),
        )
        connection.execute(
            "INSERT INTO investment_assessment "
            "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,payload) "
            "VALUES (?,1,'local_owner','candidate_ranking',?,NULL,?,?,1,?,?,?)",
            (RANKING, ACCOUNT, AS_OF, HASH, "b" * 64, "c" * 64, '{"ranking":"payload"}'),
        )
        connection.execute(
            "INSERT INTO candidate_ranking_entry "
            "(ranking_id,revision,owner_id,account_id,listing_id,ordinal,payload) VALUES (?,1,'local_owner',?,7,0,?)",
            (RANKING, ACCOUNT, entry_payload),
        )
        connection.execute(
            "INSERT INTO investment_assessment "
            "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,"
            "series_key,application_sequence,application_mode,payload) "
            "VALUES (?,1,'local_owner','rule_application',NULL,7,?,?,NULL,NULL,NULL,?,1,'live',?)",
            (APPLICATION, AS_OF, HASH, "d" * 64, application_payload),
        )
    return payload, entry_payload, application_payload


def _rows(database: Path, columns: str, where: str = "") -> list[tuple]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            f"SELECT {columns} FROM investment_assessment {where} ORDER BY id"
        ).fetchall()


def test_revision_chain_and_fresh_head_metadata(tmp_path, monkeypatch) -> None:
    script = ScriptDirectory(str(ROOT / "alembic"))
    assert script.get_revision("052").down_revision == "051"
    assert script.get_current_head() == "055"
    database = tmp_path / "fresh-052.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    assert _metadata_diffs(database) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='investment_assessment'"
            )
        }
        assert "ix_investment_assessment_rule_coverage_task" in indexes
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_research_rule_binding%'").fetchone()[0] == 3
    command.downgrade(config, "051")
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "research_rule_binding" not in tables
        assert "investment_assessment" in tables
    command.upgrade(config, "head")


def test_upgrade_and_downgrade_roundtrip_preserves_p1_p2_bytes(tmp_path, monkeypatch) -> None:
    database = tmp_path / "preserve.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "051")
    payload, entry_payload, application_payload = _seed_p1_p2(database)
    before = _rows(database, "*")
    command.upgrade(config, "052")
    assert _rows(database, "*") == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT payload FROM investment_assessment WHERE kind='rule_application'"
        ).fetchone()[0] == application_payload
        assert connection.execute(
            "SELECT payload FROM investment_assessment WHERE kind='account_fit'"
        ).fetchone()[0] == payload
        assert connection.execute(
            "SELECT payload FROM candidate_ranking_entry WHERE ranking_id=?", (RANKING,)
        ).fetchone()[0] == entry_payload
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%investment_assessment%'"
        ).fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    command.downgrade(config, "051")
    assert _rows(database, "*") == before
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(investment_assessment)")}
        assert {"series_key", "application_sequence", "application_mode"} <= columns
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='investment_assessment'"
        ).fetchone()[0]
        assert "rule_coverage" not in table_sql
        kinds = {row[0] for row in connection.execute("SELECT DISTINCT kind FROM investment_assessment")}
        assert kinds == {"account_fit", "candidate_ranking", "rule_application"}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    command.upgrade(config, "head")


_COVERAGE_COLUMNS = (
    "id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,"
    "series_key,application_sequence,application_mode,payload"
)


def _coverage_sql(
    *,
    account: str = "NULL",
    entry: str = "2",
    manifest: str = "'e'",
    series: str = "NULL",
    sequence: str = "NULL",
    mode: str = "NULL",
    ident: str = "55555555-5555-4555-8555-555555555555",
) -> tuple[str, tuple]:
    payload = f'{{"stage":"prepared","task_id":"{TASK}"}}'
    return (
        f"INSERT INTO investment_assessment ({_COVERAGE_COLUMNS}) "
        f"VALUES ('{ident}',1,'local_owner','rule_coverage',{account},7,?,?,{entry},{manifest},NULL,"
        f"{series},{sequence},{mode},'{payload}')",
        (AS_OF, HASH),
    )


def test_fresh_head_accepts_rule_coverage_shape_and_rejects_bypasses(tmp_path, monkeypatch) -> None:
    database = tmp_path / "coverage-shape.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        sql, args = _coverage_sql()
        connection.execute(sql, args)
        bypasses = (
            _coverage_sql(account="'99999999-9999-4999-8999-999999999999'"),
            _coverage_sql(entry="NULL"),
            _coverage_sql(entry="-1"),
            _coverage_sql(manifest="NULL"),
            _coverage_sql(series="'x'", sequence="1", mode="'live'"),
            (sql.replace("'rule_coverage'", "'unknown_kind'"), args),
        )
        for bypass_sql, bypass_args in bypasses:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(bypass_sql, bypass_args)
        # The partial expression index resolves the task for owner-scoped reads.
        assert connection.execute(
            "SELECT id FROM investment_assessment WHERE kind='rule_coverage' "
            "AND owner_id='local_owner' AND json_extract(payload,'$.task_id')=?", (TASK,)
        ).fetchone() == ("55555555-5555-4555-8555-555555555555",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _metadata_diffs(database) == []


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_direct_sql_guards_for_binding_rows(tmp_path, monkeypatch, foreign_keys: int) -> None:
    database = tmp_path / f"binding-guards-{foreign_keys}.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA foreign_keys={foreign_keys}")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (foreign_keys,)
        row = (
            "66666666-6666-4666-8666-666666666666", 1, "local_owner", TASK, 7,
            "company_research", AS_OF, 1, 0, HASH, HASH, HASH, AS_OF, "{}",
        )
        columns = (
            "id,revision,owner_id,task_id,listing_id,research_kind,admission_as_of,rules_generation,"
            "events_generation,rule_set_hash,input_hash,content_hash,created_at,payload"
        )
        connection.execute(f"INSERT INTO research_rule_binding ({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        # Same task cannot bind twice; same id cannot be re-inserted.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO research_rule_binding ({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("77777777-7777-4777-8777-777777777777", 1, "local_owner", TASK, 7,
                 "review", AS_OF, 1, 0, HASH, HASH, HASH, AS_OF, "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                f"INSERT INTO research_rule_binding ({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row,
            )
        # Shape guards: revision, hash lengths, JSON object payload.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO research_rule_binding ({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("88888888-8888-4888-8888-888888888888", 2, "local_owner",
                 "99999999-9999-4999-8999-999999999999", 7, "review", AS_OF, 1, 0, HASH, HASH, HASH, AS_OF, "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO research_rule_binding ({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("88888888-8888-4888-8888-888888888888", 1, "local_owner",
                 "99999999-9999-4999-8999-999999999999", 7, "review", AS_OF, 1, 0, "short", HASH, HASH, AS_OF, "{}"),
            )
        # Immutable rows: UPDATE, DELETE and REPLACE all abort.
        for sql in (
            "UPDATE research_rule_binding SET payload='{}'",
            "DELETE FROM research_rule_binding",
            "INSERT OR REPLACE INTO research_rule_binding SELECT * FROM research_rule_binding",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
        # Rebuilt assessment entry guards still reject orphans after 052.
        with pytest.raises(sqlite3.IntegrityError, match="invalid or complete"):
            connection.execute(
                "INSERT INTO candidate_ranking_entry "
                "(ranking_id,revision,owner_id,account_id,listing_id,ordinal,payload) "
                "VALUES ('00000000-0000-4000-8000-000000000000',1,'local_owner',?,7,0,'{}')", (ACCOUNT,),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _metadata_diffs(database) == []


@pytest.mark.parametrize("p3_record", ["coverage_row", "binding_row", "coverage_dependency"])
def test_downgrade_refuses_each_p3_record_kind(tmp_path, monkeypatch, p3_record: str) -> None:
    database = tmp_path / f"block-{p3_record}.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        if p3_record == "coverage_row":
            connection.execute(
                "INSERT INTO investment_assessment "
                "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,"
                "series_key,application_sequence,application_mode,payload) "
                "VALUES ('55555555-5555-4555-8555-555555555555',1,'local_owner','rule_coverage',NULL,7,?,?,1,?,NULL,"
                "NULL,NULL,NULL,'{}')",
                (AS_OF, HASH, "e" * 64),
            )
        elif p3_record == "binding_row":
            connection.execute(
                "INSERT INTO research_rule_binding "
                "(id,revision,owner_id,task_id,listing_id,research_kind,admission_as_of,rules_generation,"
                "events_generation,rule_set_hash,input_hash,content_hash,created_at,payload) "
                "VALUES ('66666666-6666-4666-8666-666666666666',1,'local_owner',?,7,'company_research',?,1,0,?,?,?,?,'{}')",
                (TASK, AS_OF, HASH, HASH, HASH, AS_OF),
            )
        else:
            connection.execute(
                "INSERT INTO investor_publication_dependency (recommendation_id,ordinal,payload) VALUES (?,?,?)",
                (
                    "77777777-7777-4777-8777-777777777777",
                    0,
                    '{"ref":{"kind":"rule_coverage","id":"55555555-5555-4555-8555-555555555555","revision":1},'
                    '"materiality":"critical","fields":["rules"]}',
                ),
            )
    with pytest.raises(RuntimeError, match="cannot downgrade 052"):
        command.downgrade(config, "051")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("052",)

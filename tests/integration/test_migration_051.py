"""Migration 051 preserves P1 rows and installs P2 SQL guards."""

from __future__ import annotations

import json
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
AS_OF = "2026-10-16 00:00:00.000000"
ACCOUNT = "11111111-1111-4111-8111-111111111111"
RANKING = "22222222-2222-4222-8222-222222222222"
HASH = "a" * 64


def _metadata_diffs(path: Path) -> list[object]:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            return [
                diff for diff in compare_metadata(MigrationContext.configure(connection), Base.metadata)
                if "investment_assessment" in repr(diff)
                or "candidate_ranking_entry" in repr(diff)
                or "supplemental_rule" in repr(diff)
                or "research_event_revision" in repr(diff)
            ]
    finally:
        engine.dispose()


def _seed_p1(database: Path, *, head: str = "050") -> tuple[str, str]:
    payload = '{"as_of":"2026-10-16T00:00:00+00:00","preserve":"byte-for-byte"}'
    entry_payload = '{"ordinal":0,"preserve":"entry-bytes"}'
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
        connection.execute("CREATE TABLE IF NOT EXISTS migration_marker(value TEXT NOT NULL)")
        connection.execute("DELETE FROM migration_marker")
        connection.execute("INSERT INTO migration_marker VALUES (?)", (head,))
    return payload, entry_payload


def test_revision_chain_and_fresh_head_metadata(tmp_path, monkeypatch) -> None:
    script = ScriptDirectory(str(ROOT / "alembic"))
    assert script.get_revision("051").down_revision == "050"
    assert script.get_current_head() == "051"
    database = tmp_path / "fresh-051.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    assert _metadata_diffs(database) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA table_info(investment_assessment)").fetchall()
        connection.execute(
            "INSERT INTO supplemental_registry_head "
            "(owner_id,registry_revision,rules_generation,events_generation,updated_at) "
            "VALUES ('local_owner',0,0,0,?)", (AS_OF,),
        )
    command.downgrade(config, "050")
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not ({"supplemental_rule_revision", "research_event_revision"} & tables)
        assert "investment_assessment" in tables
    command.downgrade(config, "049")
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "investment_assessment" not in tables
    command.upgrade(config, "head")


def test_upgrade_and_downgrade_preserve_p1_payload_and_entries(tmp_path, monkeypatch) -> None:
    database = tmp_path / "preserve-p1.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "050")
    payload, entry_payload = _seed_p1(database)
    command.upgrade(config, "051")
    with sqlite3.connect(database) as connection:
        fit = connection.execute("SELECT payload FROM investment_assessment WHERE kind='account_fit'").fetchone()[0]
        entry = connection.execute("SELECT payload FROM candidate_ranking_entry WHERE ranking_id=?", (RANKING,)).fetchone()[0]
        assert fit == payload
        assert entry == entry_payload
        assert connection.execute("SELECT series_key,application_sequence,application_mode FROM investment_assessment WHERE kind='account_fit'").fetchone() == (None, None, None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%investment_assessment%'").fetchone()[0] == 3
    command.downgrade(config, "050")
    with sqlite3.connect(database) as connection:
        fit = connection.execute("SELECT payload FROM investment_assessment WHERE kind='account_fit'").fetchone()[0]
        entry = connection.execute("SELECT payload FROM candidate_ranking_entry WHERE ranking_id=?", (RANKING,)).fetchone()[0]
        cols = {row[1] for row in connection.execute("PRAGMA table_info(investment_assessment)")}
        assert fit == payload and entry == entry_payload
        assert not ({"series_key", "application_sequence", "application_mode"} & cols)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_direct_sql_guards_for_p2_rows(tmp_path, monkeypatch, foreign_keys: int) -> None:
    database = tmp_path / f"guards-{foreign_keys}.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA foreign_keys={foreign_keys}")
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (foreign_keys,)
        connection.execute(
            "INSERT INTO supplemental_rule_revision (id,revision,owner_id,schema_version,content_hash,semantic_hash,payload) "
            "VALUES ('11111111-1111-4111-8111-111111111111',1,'local_owner',1,?,?,?)",
            (HASH, "b" * 64, '{"id":"11111111-1111-4111-8111-111111111111"}'),
        )
        connection.execute(
            "INSERT INTO supplemental_rule_head (owner_id,rule_id,head_revision,draft_revision,draft_state) "
            "VALUES ('local_owner','11111111-1111-4111-8111-111111111111',1,1,'draft')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="owner mismatch"):
            connection.execute(
                "INSERT INTO supplemental_rule_head (owner_id,rule_id,head_revision,draft_revision,draft_state) "
                "VALUES ('other','11111111-1111-4111-8111-111111111111',1,1,'draft')"
            )
        orphan_rule_id = "99999999-9999-4999-8999-999999999999"
        with pytest.raises(sqlite3.IntegrityError, match="owner mismatch"):
            connection.execute(
                "INSERT INTO supplemental_rule_head (owner_id,rule_id,head_revision) "
                "VALUES ('local_owner',?,0)", (orphan_rule_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="owner mismatch"):
            connection.execute(
                "UPDATE supplemental_rule_head SET rule_id=? WHERE owner_id='local_owner' "
                "AND rule_id='11111111-1111-4111-8111-111111111111'", (orphan_rule_id,),
            )
        connection.execute(
            "INSERT INTO supplemental_rule_event "
            "(owner_id,rule_id,sequence,rule_revision,operation_id,command,created_at,payload) "
            "VALUES ('local_owner','11111111-1111-4111-8111-111111111111',1,1,'22222222-2222-4222-8222-222222222222','draft','2026-10-16', '{}')"
        )
        event_row = (
            "33333333-3333-4333-8333-333333333333", 1, "local_owner", None, "event-1", "[]",
            None, None, None, "2026-10-16", "2026-10-16", "2026-10-16", HASH, "{}",
        )
        connection.execute(
            "INSERT INTO research_event_revision "
            "(id,revision,owner_id,previous_ref,event_key,event_aliases,published_at,effective_from,effective_to,acquired_at,available_from,created_at,content_hash,payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", event_row,
        )
        with pytest.raises(sqlite3.IntegrityError, match="previous revision"):
            connection.execute(
                "INSERT INTO research_event_revision "
                "(id,revision,owner_id,previous_ref,event_key,event_aliases,acquired_at,available_from,created_at,content_hash,payload) "
                "VALUES (?,2,'local_owner',NULL,'event-1','[]','2026-10-16','2026-10-16','2026-10-16',?,'{}')",
                (event_row[0], HASH),
            )
        with pytest.raises(sqlite3.IntegrityError, match="previous revision"):
            connection.execute(
                "INSERT INTO research_event_revision "
                "(id,revision,owner_id,previous_ref,event_key,event_aliases,acquired_at,available_from,created_at,content_hash,payload) "
                "VALUES (?,2,'local_owner','{}','event-1','[]','2026-10-16','2026-10-16','2026-10-16',?,'{}')",
                (event_row[0], HASH),
            )
        for sql, args in (
            (
                "INSERT INTO investment_assessment "
                "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,series_key,application_sequence,application_mode,payload) "
                "VALUES ('44444444-4444-4444-8444-444444444444',1,'local_owner','rule_application',NULL,7,?,?,NULL,NULL,NULL,?,1,'live','{}')",
                (AS_OF, HASH, "d" * 64),
            ),
        ):
            connection.execute(sql, args)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO investment_assessment "
                "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,series_key,application_sequence,application_mode,payload) "
                "VALUES ('55555555-5555-4555-8555-555555555555',1,'local_owner','rule_application',NULL,7,?,?,?,1,'live','{}')",
                (AS_OF, HASH, "d" * 64),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO investment_assessment "
                "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,payload) "
                "VALUES ('66666666-6666-4666-8666-666666666666',1,'local_owner','account_fit',NULL,7,?, ?,NULL,NULL,NULL,'{}')",
                (AS_OF, HASH),
            )
        for sql in (
            "UPDATE supplemental_rule_revision SET payload='{}'",
            "DELETE FROM supplemental_rule_revision",
            "INSERT OR REPLACE INTO supplemental_rule_revision SELECT * FROM supplemental_rule_revision",
            "UPDATE supplemental_rule_event SET payload='{}'",
            "DELETE FROM research_event_revision",
            "INSERT OR REPLACE INTO research_event_revision SELECT * FROM research_event_revision",
            "UPDATE investment_assessment SET payload='{}'",
            "DELETE FROM investment_assessment WHERE kind='rule_application'",
            "INSERT OR REPLACE INTO investment_assessment SELECT * FROM investment_assessment WHERE kind='rule_application'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _metadata_diffs(database) == []


def test_sql_head_checks_reject_null_branch_bypasses(tmp_path, monkeypatch) -> None:
    database = tmp_path / "head-null-branches.db"
    command.upgrade(_config(database, monkeypatch), "head")
    with sqlite3.connect(database) as connection:
        for n in range(1, 4):
            rule_id = f"{n:08d}-1111-4111-8111-111111111111"
            connection.execute(
                "INSERT INTO supplemental_rule_revision "
                "(id,revision,owner_id,schema_version,content_hash,semantic_hash,payload) "
                "VALUES (?,1,'local_owner',1,?,?,?)", (rule_id, HASH, "b" * 64, "{}"),
            )
            if n == 1:
                sql = "INSERT INTO supplemental_rule_head (owner_id,rule_id,head_revision,active_revision,active_state) VALUES ('local_owner',?,1,1,NULL)"
            elif n == 2:
                sql = "INSERT INTO supplemental_rule_head (owner_id,rule_id,head_revision,draft_state) VALUES ('local_owner',?,1,'draft')"
            else:
                sql = "INSERT INTO supplemental_rule_head (owner_id,rule_id,head_revision,last_validation_revision,last_validation_hash) VALUES ('local_owner',?,1,1,NULL)"
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, (rule_id,))


def test_create_all_installs_equivalent_head_and_previous_ref_guards(tmp_path) -> None:
    database = tmp_path / "create-all-guards.db"
    engine = create_engine(f"sqlite:///{database}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO supplemental_rule_revision "
            "(id,revision,owner_id,schema_version,content_hash,semantic_hash,payload) "
            "VALUES ('11111111-1111-4111-8111-111111111111',1,'local_owner',1,?,?,?)",
            (HASH, "b" * 64, "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="owner mismatch"):
            connection.execute(
                "INSERT INTO supplemental_rule_head (owner_id,rule_id,head_revision) "
                "VALUES ('local_owner','99999999-9999-4999-8999-999999999999',0)"
            )
        connection.execute(
            "INSERT INTO research_event_revision "
            "(id,revision,owner_id,event_key,event_aliases,acquired_at,available_from,created_at,content_hash,payload) "
            "VALUES ('33333333-3333-4333-8333-333333333333',1,'local_owner','event-1','[]',"
            "'2026-10-16','2026-10-16','2026-10-16',?,'{}')", (HASH,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="previous revision"):
            connection.execute(
                "INSERT INTO research_event_revision "
                "(id,revision,owner_id,previous_ref,event_key,event_aliases,acquired_at,available_from,created_at,content_hash,payload) "
                "VALUES ('33333333-3333-4333-8333-333333333333',2,'local_owner','{}','event-1','[]',"
                "'2026-10-16','2026-10-16','2026-10-16',?,'{}')", (HASH,),
            )


@pytest.mark.parametrize("p2_record", ["failed_zero_run", "receipt"])
def test_downgrade_refuses_even_failed_zero_member_run_and_receipt(tmp_path, monkeypatch, p2_record: str) -> None:
    database = tmp_path / f"block-{p2_record}.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        if p2_record == "failed_zero_run":
            connection.execute(
                "INSERT INTO pipeline_run (kind,triggered_by,started_at,status,stage,idempotency_key,request_json,metadata_json) "
                "VALUES ('supplemental_rule_evaluation','user','2026-10-16','failed','failed','run-key',?,?)",
                (json.dumps({"pairs": []}), json.dumps({"member_count": 0})),
            )
        else:
            connection.execute(
                "INSERT INTO investor_mutation (owner_id,key,request_hash,result) VALUES ('local_owner',?,?,?)",
                ("77777777-7777-4777-8777-777777777777", HASH, json.dumps({"result_kind": "evaluation_admission", "run_ref": {"kind": "pipeline_run", "id": 1, "revision": 1}})),
            )
    with pytest.raises(RuntimeError, match="cannot downgrade 051"):
        command.downgrade(config, "050")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("051",)

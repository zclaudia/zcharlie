from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from tests.integration.test_migration_046 import _config

ROOT = Path(__file__).resolve().parents[2]


def test_054_is_independent_successor_and_empty_downgrade_is_allowed(tmp_path, monkeypatch):
    script = ScriptDirectory(str(ROOT / "alembic"))
    assert script.get_revision("054").down_revision == "053"
    database = tmp_path / "empty.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("054",)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id,payload_hash,payload FROM supplemental_change_receipt "
            "WHERE owner_id=? AND listing_id=? AND context=? AND kind='source_page' "
            "AND json_extract(payload,'$.record_type')='poll_final_v1' AND committed_at<=? "
            "ORDER BY committed_at DESC,sequence DESC LIMIT 1",
            ("local_owner", 7, "company_research", "2026-10-17T00:00:00+00:00"),
        ).fetchall()
        assert any("ix_supplemental_change_poll_final_cutoff" in row[3] for row in plan), plan
    command.downgrade(config, "053")


@pytest.mark.parametrize(
    ("table", "column", "payload"),
    [
        ("investment_assessment", "payload", '{"schema_version":3,"source_coverage_binding":'
         '{"listing_ref":{},"context":"company_research","scope_key":"listing:7:company_research",'
         '"sources":[],"source_policy_hash":"x","observed_at":"2026-10-17T00:00:00Z"}}'),
        ("pipeline_run", "request_json", '{"proof_type":"poll_window_v1"}'),
        ("pipeline_run", "request_json", '{"record_type":"poll_step_v1","schema_version":1,'
         '"owner_id":"local_owner","poll_sequence":1,"poll_id":"id","poll_hash":"hash",'
         '"source_scope_key":"listing:7:company_research","source_scope_revision":1}'),
        ("agent_task", "payload", '{"record_type":"poll_final_v1"}'),
        ("agent_execution_grant", "payload", '{"record_type":"source_authorization_v1"}'),
        ("disclosure_receipt", "payload", '{"record_type":"http_attempt_v1"}'),
    ],
)
def test_054_blocks_downgrade_for_b_or_v3_legacy_payloads(
    tmp_path, monkeypatch, table, column, payload,
):
    database = tmp_path / f"{table}.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        if table == "pipeline_run":
            connection.execute(
                "INSERT INTO pipeline_run "
                "(id,kind,triggered_by,started_at,status,stage,attempt_count,max_attempts,request_json,metadata_json) "
                "VALUES (1,'agent_research','test','2026-10-17T00:00:00+00:00','failed','created',0,3,?, '{}')",
                (payload,),
            )
        elif table == "agent_task":
            connection.execute(
                "INSERT INTO pipeline_run "
                "(id,kind,triggered_by,started_at,status,stage,attempt_count,max_attempts,request_json,metadata_json) "
                "VALUES (1,'agent_research','test','2026-10-17T00:00:00+00:00','failed','created',0,3,'{}','{}')"
            )
            connection.execute(
                "INSERT INTO agent_task "
                "(id,owner_id,input_key,input_hash,revision,status,run_id,payload,checkpoint,resolved,consumed_events) "
                "VALUES ('task-1','local_owner','key','" + "b" * 64 + "',1,'failed',1,?,'{}','[]','[]')",
                (payload,),
            )
        elif table == "agent_execution_grant":
            connection.execute(
                "INSERT INTO pipeline_run "
                "(id,kind,triggered_by,started_at,status,stage,attempt_count,max_attempts,request_json,metadata_json) "
                "VALUES (1,'agent_research','test','2026-10-17T00:00:00+00:00','failed','created',0,3,'{}','{}')"
            )
            connection.execute(
                "INSERT INTO agent_task "
                "(id,owner_id,input_key,input_hash,revision,status,run_id,payload,checkpoint,resolved,consumed_events) "
                "VALUES ('task-1','local_owner','key','" + "b" * 64 + "',1,'failed',1,'{}','{}','[]','[]')"
            )
            connection.execute(
                "INSERT INTO agent_execution_grant (task_id,payload) VALUES ('task-1',?)", (payload,)
            )
        elif table == "disclosure_receipt":
            connection.execute(
                "INSERT INTO disclosure_receipt (operation_id,owner_id,request_hash,payload) "
                "VALUES ('op-1','local_owner',?,?)",
                ("a" * 64, payload),
            )
        else:
            connection.execute(
                "INSERT INTO investment_assessment "
                "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,"
                "series_key,application_sequence,application_mode,payload) "
                "VALUES ('id-1',1,'local_owner','account_fit','acct',7,'2026-10-17',?,NULL,NULL,NULL,NULL,NULL,NULL,?)",
                ("a" * 64, payload),
            )
        assert connection.execute("SELECT COUNT(*) FROM supplemental_change_receipt").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM supplemental_source_state").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM supplemental_refresh_state").fetchone() == (0,)
    with pytest.raises(RuntimeError, match="B poll or V3"):
        command.downgrade(config, "053")


def test_054_allows_ordinary_receipt_text_with_incidental_source_terms(tmp_path, monkeypatch):
    database = tmp_path / "ordinary-receipt.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO disclosure_receipt (operation_id,owner_id,request_hash,payload) "
            "VALUES ('op-ordinary','local_owner',?,?)",
            ("c" * 64, '{"title":"cninfo_szse_v1","source_coverage_binding":"example",'
             '"body":"poll_final_v1 is just a phrase in ordinary receipt text"}'),
        )
    command.downgrade(config, "053")

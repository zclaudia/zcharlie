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


def test_053_fresh_upgrade_metadata_and_empty_downgrade_roundtrip(tmp_path, monkeypatch):
    script = ScriptDirectory(str(ROOT / "alembic"))
    assert script.get_revision("053").down_revision == "052"
    database = tmp_path / "fresh.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "053")
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert not [
            diff
            for diff in diffs
            if "supplemental_change_receipt" in repr(diff)
            or "supplemental_source_state" in repr(diff)
            or "supplemental_refresh_state" in repr(diff)
            if "ix_supplemental_change_poll_final_cutoff" not in repr(diff)
        ]
    finally:
        engine.dispose()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("053",)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "supplemental_change_receipt",
            "supplemental_source_state",
            "supplemental_refresh_state",
        } <= tables
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='supplemental_change_receipt'"
            )
        }
        assert "ix_supplemental_change_owner_scope_sequence" in indexes
        assert "uq_supplemental_change_parent_member" in indexes
        assert "ix_supplemental_change_evidence_proof" in indexes
    command.downgrade(config, "052")
    command.upgrade(config, "053")


def test_053_downgrade_rejects_receipts_and_legacy_p4_payload(tmp_path, monkeypatch):
    database = tmp_path / "guard.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "053")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO supplemental_change_receipt "
            "(id,owner_id,change_key,schema_version,kind,acquired_at,available_from,committed_at,payload_hash,payload) "
            "VALUES ('00000000-0000-4000-8000-000000000001','local_owner','test:one',1,'bootstrap',"
            "'2026-10-17','2026-10-17','2026-10-17',?,?)",
            ("a" * 64, '{"schema_version":1}'),
        )
    with pytest.raises(RuntimeError, match="supplemental update state"):
        command.downgrade(config, "052")


def test_053_downgrade_rejects_p4_source_binding_in_existing_payload(tmp_path, monkeypatch):
    database = tmp_path / "legacy-payload.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "053")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO investment_assessment "
            "(id,revision,owner_id,kind,account_id,listing_id,as_of,input_hash,entry_count,manifest_hash,universe_hash,"
            "series_key,application_sequence,application_mode,payload) "
            "VALUES ('00000000-0000-4000-8000-000000000002',1,'local_owner','account_fit','acct',7,"
            "'2026-10-17','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',NULL,NULL,NULL,NULL,NULL,NULL,?)",
            ('{"update_source_binding":{"schema_version":2}}',),
        )
    with pytest.raises(RuntimeError, match="P4 source binding"):
        command.downgrade(config, "052")


@pytest.mark.parametrize(
    ("table", "column", "payload"),
    [
        ("pipeline_run", "request_json", '{"execution_envelope":{"supplemental_update_sequence":3}}'),
        ("pipeline_run", "metadata_json", '{"p4_resume_revision":1}'),
        ("pipeline_run", "request_json", '{"command":"supplemental_rule.update_recovery.v1"}'),
        ("pipeline_run", "kind", "supplemental_update_process"),
        ("agent_task", "payload", '{"supplemental_update_batch_id":"batch"}'),
        ("agent_task", "checkpoint", '{"p4_source_binding":{"scope":"listing:7"}}'),
        ("agent_execution_grant", "payload", '{"supplemental_update_batch_hash":"' + "a" * 64 + '"}'),
    ],
)
def test_053_empty_new_tables_still_guard_legacy_run_task_and_grant_payloads(
    tmp_path, monkeypatch, table, column, payload,
):
    database = tmp_path / f"legacy-{table}-{column}.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "053")
    with sqlite3.connect(database) as connection:
        if table == "pipeline_run":
            connection.execute(
                "INSERT INTO pipeline_run "
                "(id,kind,triggered_by,started_at,status,stage,attempt_count,max_attempts,request_json,metadata_json) "
                "VALUES (1,'supplemental_rule_evaluate','test','2026-10-17T00:00:00+00:00','failed','created',0,3,'{}','{}')"
            )
        else:
            connection.execute(
                "INSERT INTO pipeline_run "
                "(id,kind,triggered_by,started_at,status,stage,attempt_count,max_attempts,request_json,metadata_json) "
                "VALUES (1,'agent_research','test','2026-10-17T00:00:00+00:00','failed','created',0,3,'{}','{}')"
            )
            if table in {"agent_task", "agent_execution_grant"}:
                connection.execute(
                    "INSERT INTO agent_task "
                    "(id,owner_id,input_key,input_hash,revision,status,run_id,payload,checkpoint,resolved,consumed_events) "
                    "VALUES ('task-1','local_owner','key','" + "b" * 64 + "',1,'failed',1,'{}','{}','[]','[]')"
                )
        if table == "agent_execution_grant":
            connection.execute(
                "INSERT INTO agent_execution_grant (task_id,payload) VALUES ('task-1',?)",
                (payload,),
            )
        else:
            connection.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (payload, 1 if table == "pipeline_run" else "task-1"))
        assert connection.execute("SELECT COUNT(*) FROM supplemental_change_receipt").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM supplemental_source_state").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM supplemental_refresh_state").fetchone() == (0,)
    with pytest.raises(RuntimeError, match="P4 source binding"):
        command.downgrade(config, "052")

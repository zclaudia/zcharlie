"""034 temporary database only; complete metadata and retained legacy purpose."""
import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from investment_agent.storage.models.base import Base

CURRENT_HEAD = ScriptDirectory(
    str(Path(__file__).resolve().parents[2] / "alembic")
).get_current_head()


def test_discovery_migration_metadata_roundtrip_and_legacy_review(tmp_path, monkeypatch):
    database=tmp_path/"discovery.db"
    monkeypatch.setenv("DATABASE_URL",f"sqlite+aiosqlite:///{database}")
    config=Config()
    config.set_main_option("script_location",str(Path(__file__).resolve().parents[2]/"alembic"))
    command.upgrade(config,"033")
    # Minimal trusted queue/grant/expense history bypasses ORM business validation
    # to independently exercise the migration classification, not model text.
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO pipeline_run (id,kind,triggered_by,status,request_json,attempt_count,max_attempts,started_at,stage,metadata_json) VALUES (1,'agent_review','system','queued','{}',0,1,CURRENT_TIMESTAMP,'created','{}')")
        connection.execute("INSERT INTO agent_task (id,owner_id,input_key,input_hash,revision,status,run_id,payload,checkpoint,resolved,consumed_events) VALUES ('legacy','local_owner','legacy','hash',1,'queued',1,'{}','{}','[]','[]')")
        connection.execute("INSERT INTO agent_execution_grant VALUES ('legacy',?)",(json.dumps({"research_kind":"review"}),))
        connection.execute("INSERT INTO agent_task_attempt VALUES ('attempt','legacy',1,1,'fixture','running')")
        connection.execute("INSERT INTO agent_budget_entry (id,task_id,attempt_id,owner_id,day,state,kind,calls,estimated_tokens,cost_usd) VALUES ('charge','legacy','attempt','local_owner','2026-10-10','settled','model',1,0,.1)")
    command.upgrade(config,"head")
    engine=create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        relevant = {"investor_discovery_cursor", "investor_discovery_batch", "agent_budget_entry"}
        assert not [diff for diff in compare_metadata(MigrationContext.configure(connection),Base.metadata) if any(table in repr(diff) for table in relevant)]
    engine.dispose()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT purpose,cost_usd FROM agent_budget_entry").fetchone()==('review',.1)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()==(CURRENT_HEAD,)
    command.downgrade(config,"033")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT cost_usd FROM agent_budget_entry").fetchone()==(.1,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='investor_discovery_batch'").fetchone() is None
    command.upgrade(config,"034")

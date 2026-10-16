from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, literal_column, select, text
from sqlalchemy.dialects.sqlite import dialect

from investment_agent.storage.models import Base
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from tests.integration.test_migration_046 import _config

ROOT = Path(__file__).resolve().parents[2]


def _operation_index_is_used(connection) -> bool:
    statement = select(
        InvestmentAssessment.id, InvestmentAssessment.revision
    ).where(
        InvestmentAssessment.owner_id == "local_owner",
        InvestmentAssessment.kind == "rule_application",
        func.json_extract(
            InvestmentAssessment.payload, literal_column("'$.operation_id'")
        ) == "synthetic-operation",
    )
    compiled = statement.compile(dialect=dialect())
    parameters = tuple(compiled.params[key] for key in compiled.positiontup or ())
    plan = connection.exec_driver_sql(
        f"EXPLAIN QUERY PLAN {compiled}", parameters
    ).all()
    return any(
        "ix_investment_assessment_rule_application_operation" in str(row)
        for row in plan
    )


def _schema_diffs(database: Path) -> list[object]:
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            return list(compare_metadata(MigrationContext.configure(connection), Base.metadata))
    finally:
        engine.dispose()


def test_operation_partial_json_index_matches_orm_migration_and_recovery_query(
    tmp_path, monkeypatch,
) -> None:
    orm_database = tmp_path / "p2-operation-index-orm.db"
    orm_engine = create_engine(f"sqlite:///{orm_database}")
    try:
        Base.metadata.create_all(orm_engine)
        with orm_engine.connect() as connection:
            assert _operation_index_is_used(connection)
    finally:
        orm_engine.dispose()

    migrated_database = tmp_path / "p2-operation-index-migration.db"
    config = _config(migrated_database, monkeypatch)
    command.upgrade(config, "head")
    diffs = _schema_diffs(migrated_database)
    assert all(
        diff[0] == "remove_index"
        and getattr(diff[1], "name", None) == "ix_research_change_previous_snapshot"
        for diff in diffs
    )
    migrated_engine = create_engine(f"sqlite:///{migrated_database}")
    try:
        with migrated_engine.connect() as connection:
            assert _operation_index_is_used(connection)
            index_sql = connection.execute(
                text("SELECT sql FROM sqlite_master WHERE type='index' "
                     "AND name='ix_investment_assessment_rule_application_operation'")
            ).scalar_one()
            assert "WHERE kind = 'rule_application'" in index_sql
    finally:
        migrated_engine.dispose()

    command.downgrade(config, "050")
    downgraded_engine = create_engine(f"sqlite:///{migrated_database}")
    try:
        with downgraded_engine.connect() as connection:
            assert connection.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='index' "
                     "AND name='ix_investment_assessment_rule_application_operation'")
            ).first() is None
    finally:
        downgraded_engine.dispose()


def test_downgrade_detects_actual_p2_receipt_envelope(tmp_path, monkeypatch) -> None:
    database = tmp_path / "p2-receipt-envelope.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    receipt = {
        "p2_schema_version": 1,
        "receipt": {"result_kind": "evaluation_admission"},
        "result": {"admission": {}},
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO investor_mutation (owner_id,key,request_hash,result) "
            "VALUES ('local_owner',?,?,?)",
            ("77777777-7777-4777-8777-777777777777", "a" * 64, json.dumps(receipt)),
        )

    with pytest.raises(RuntimeError, match="cannot downgrade 051"):
        command.downgrade(config, "050")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("051",)


def test_migration_head_rejects_validation_for_non_draft_revision(tmp_path, monkeypatch) -> None:
    database = tmp_path / "head-validation-reference.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        rule_id = "88888888-8888-4888-8888-888888888888"
        connection.execute(
            "INSERT INTO supplemental_rule_revision "
            "(id,revision,owner_id,schema_version,content_hash,semantic_hash,payload) "
            "VALUES (?,1,'local_owner',1,?,?,?)",
            (rule_id, "a" * 64, "b" * 64, "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="head reference owner mismatch"):
            connection.execute(
                "INSERT INTO supplemental_rule_head "
                "(owner_id,rule_id,head_revision,draft_revision,draft_state,"
                "last_validation_revision,last_validation_hash) "
                "VALUES ('local_owner',?,1,1,'validated',2,?)",
                (rule_id, "c" * 64),
            )


def test_create_all_head_guard_matches_migration_validation_reference(tmp_path) -> None:
    database = tmp_path / "create-all-head-validation-reference.db"
    engine = create_engine(f"sqlite:///{database}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    with sqlite3.connect(database) as connection:
        rule_id = "99999999-9999-4999-8999-999999999999"
        connection.execute(
            "INSERT INTO supplemental_rule_revision "
            "(id,revision,owner_id,schema_version,content_hash,semantic_hash,payload) "
            "VALUES (?,1,'local_owner',1,?,?,?)",
            (rule_id, "a" * 64, "b" * 64, "{}"),
        )
        with pytest.raises(sqlite3.IntegrityError, match="head reference owner mismatch"):
            connection.execute(
                "INSERT INTO supplemental_rule_head "
                "(owner_id,rule_id,head_revision,draft_revision,draft_state,"
                "last_validation_revision,last_validation_hash) "
                "VALUES ('local_owner',?,1,1,'validated',2,?)",
                (rule_id, "c" * 64),
            )

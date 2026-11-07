"""Bank topic versions, finite conditions and immutable check history.

Revision ID: 063
Revises: 062

Frozen SQLite DDL; no live model imports. History tables have no hidden rowid,
so OR REPLACE cannot bypass their declared identity guards. Populated studies
must be retained; downgrade is available only before any topic data is saved.
"""

import sqlalchemy as sa
from alembic import op

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None

_STATEMENTS = (
    "CREATE TABLE topic_case (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\tlisting_id INTEGER NOT NULL, \n\ttemplate_key VARCHAR(80) NOT NULL, \n\tbase_period DATE NOT NULL, \n\thead_revision INTEGER NOT NULL, \n\tcontrol_revision INTEGER NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_topic_case_identity UNIQUE (owner_id, listing_id, template_key, base_period), \n\tCONSTRAINT ck_topic_case_head_revision CHECK (head_revision >= 1), \n\tCONSTRAINT ck_topic_case_control_revision CHECK (control_revision >= 1), \n\tCONSTRAINT ck_topic_case_state CHECK (state IN ('draft', 'ready', 'archived')), \n\tFOREIGN KEY(listing_id) REFERENCES listing (id)\n)",
    'CREATE INDEX ix_topic_case_owner_state ON topic_case (owner_id, state)',
    'CREATE TABLE topic_revision (\n\tid VARCHAR(36) NOT NULL, \n\tcase_id VARCHAR(36) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tschema_version INTEGER NOT NULL, \n\tquestion TEXT NOT NULL, \n\tperiod JSON NOT NULL, \n\tbaseline_period JSON NOT NULL, \n\taccounting_standard VARCHAR(16) NOT NULL, \n\treporting_scope VARCHAR(24) NOT NULL, \n\tcurrency VARCHAR(3) NOT NULL, \n\tas_of DATETIME NOT NULL, \n\tfact_bindings JSON NOT NULL, \n\tdecomposition JSON, \n\texplanations JSON NOT NULL, \n\tcondition_refs JSON NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tsupersedes_ref JSON, \n\tcreated_at DATETIME NOT NULL, \n\tactor VARCHAR(160) NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_topic_revision_case_version UNIQUE (case_id, revision), \n\tCONSTRAINT ck_topic_revision_revision CHECK (revision >= 1), \n\tCONSTRAINT ck_topic_revision_schema CHECK (schema_version = 1), \n\tFOREIGN KEY(case_id) REFERENCES topic_case (id)\n)\n WITHOUT ROWID',
    'CREATE INDEX ix_topic_revision_case_created ON topic_revision (case_id, created_at)',
    'CREATE TABLE topic_fact_revision (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\tcase_id VARCHAR(36) NOT NULL, \n\tstable_fact_id VARCHAR(80) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tschema_version INTEGER NOT NULL, \n\tmetric VARCHAR(64) NOT NULL, \n\tvalue TEXT, \n\tunit VARCHAR(24) NOT NULL, \n\traw_value TEXT, \n\traw_unit VARCHAR(32), \n\tperiod JSON NOT NULL, \n\tcomparison_period JSON, \n\taccounting_standard VARCHAR(16) NOT NULL, \n\treporting_scope VARCHAR(24) NOT NULL, \n\tcurrency VARCHAR(3) NOT NULL, \n\tsource_refs JSON NOT NULL, \n\tproof JSON, \n\tverification VARCHAR(32) NOT NULL, \n\treason VARCHAR(160), \n\tpublished_at DATETIME, \n\tpublished_date DATE, \n\tpublished_precision VARCHAR(16) NOT NULL, \n\tacquired_at DATETIME, \n\tchecked_at DATETIME, \n\treview_actor VARCHAR(160), \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_topic_fact_stable_version UNIQUE (case_id, stable_fact_id, revision), \n\tCONSTRAINT ck_topic_fact_revision CHECK (revision >= 1), \n\tCONSTRAINT ck_topic_fact_schema CHECK (schema_version = 1), \n\tCONSTRAINT ck_topic_fact_unknown_reason CHECK ((value IS NULL AND reason IS NOT NULL) OR (value IS NOT NULL AND reason IS NULL)), \n\tFOREIGN KEY(case_id) REFERENCES topic_case (id)\n)\n WITHOUT ROWID',
    'CREATE INDEX ix_topic_fact_case_metric_revision ON topic_fact_revision (case_id, metric, revision)',
    'CREATE TABLE topic_mutation_receipt (\n\towner_id VARCHAR(32) NOT NULL, \n\tmutation_id VARCHAR(36) NOT NULL, \n\troute VARCHAR(160) NOT NULL, \n\tobject_identity VARCHAR(160) NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tresponse JSON NOT NULL, \n\tstatus INTEGER NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (owner_id, mutation_id), \n\tCONSTRAINT ck_topic_receipt_status CHECK (status >= 100 AND status <= 599)\n)\n WITHOUT ROWID',
    'CREATE INDEX ix_topic_receipt_owner_created ON topic_mutation_receipt (owner_id, created_at)',
    'CREATE TABLE topic_condition_revision (\n\tcondition_id VARCHAR(36) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tschema_version INTEGER NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\tcase_id VARCHAR(36) NOT NULL, \n\ttopic_revision_ref JSON NOT NULL, \n\tdsl_version VARCHAR(64) NOT NULL, \n\tpredicate JSON NOT NULL, \n\tdependency_manifest JSON NOT NULL, \n\ttarget_selector VARCHAR(64) NOT NULL, \n\treview_due_at DATETIME NOT NULL, \n\texpires_at DATETIME NOT NULL, \n\ttimezone VARCHAR(64) NOT NULL, \n\tactivation_basis JSON, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tactor VARCHAR(160) NOT NULL, \n\tPRIMARY KEY (condition_id, revision), \n\tCONSTRAINT ck_topic_condition_revision CHECK (revision >= 1), \n\tCONSTRAINT ck_topic_condition_schema CHECK (schema_version = 1), \n\tCONSTRAINT ck_topic_condition_window CHECK (review_due_at < expires_at), \n\tFOREIGN KEY(case_id) REFERENCES topic_case (id)\n)\n WITHOUT ROWID',
    'CREATE INDEX ix_topic_condition_case_revision ON topic_condition_revision (case_id, revision)',
    "CREATE TABLE topic_condition_head (\n\tcondition_id VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\tcase_id VARCHAR(36) NOT NULL, \n\thead_revision INTEGER NOT NULL, \n\tcontrol_revision INTEGER NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tnext_due_at DATETIME, \n\tactive_run_id INTEGER, \n\tlast_check_ref JSON, \n\tPRIMARY KEY (condition_id), \n\tCONSTRAINT ck_topic_condition_head_revision CHECK (head_revision >= 1), \n\tCONSTRAINT ck_topic_condition_control_revision CHECK (control_revision >= 1), \n\tCONSTRAINT ck_topic_condition_state CHECK (state IN ('draft', 'armed', 'paused', 'cancelled', 'completed', 'expired')), \n\tFOREIGN KEY(case_id) REFERENCES topic_case (id), \n\tFOREIGN KEY(active_run_id) REFERENCES pipeline_run (id)\n)",
    'CREATE INDEX ix_topic_condition_due ON topic_condition_head (owner_id, state, next_due_at)',
    'CREATE TABLE topic_check_revision (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\tcondition_id VARCHAR(36) NOT NULL, \n\tcondition_revision INTEGER NOT NULL, \n\tschema_version INTEGER NOT NULL, \n\tcondition_ref JSON NOT NULL, \n\tevaluator_version VARCHAR(64) NOT NULL, \n\texecution_manifest JSON NOT NULL, \n\texecution_manifest_hash VARCHAR(64) NOT NULL, \n\tsemantic_input_hash VARCHAR(64) NOT NULL, \n\tas_of DATETIME NOT NULL, \n\t"trigger" VARCHAR(32) NOT NULL, \n\tresult JSON NOT NULL, \n\tunknown_reasons JSON NOT NULL, \n\tprior_check_ref JSON, \n\tsupersedes_ref JSON, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_topic_check_semantic_input UNIQUE (owner_id, condition_id, condition_revision, semantic_input_hash, evaluator_version), \n\tFOREIGN KEY(condition_id, condition_revision) REFERENCES topic_condition_revision (condition_id, revision), \n\tCONSTRAINT ck_topic_check_condition_revision CHECK (condition_revision >= 1), \n\tCONSTRAINT ck_topic_check_schema CHECK (schema_version = 1)\n)\n WITHOUT ROWID',
    'CREATE INDEX ix_topic_check_condition_created ON topic_check_revision (owner_id, condition_id, created_at)',
    "CREATE TRIGGER IF NOT EXISTS trg_topic_revision_immutable_update BEFORE UPDATE ON topic_revision BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_revision_immutable_delete BEFORE DELETE ON topic_revision BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_revision_immutable_insert BEFORE INSERT ON topic_revision WHEN EXISTS (SELECT 1 FROM topic_revision WHERE (id IS NEW.id) OR (case_id IS NEW.case_id AND revision IS NEW.revision)) BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_fact_revision_immutable_update BEFORE UPDATE ON topic_fact_revision BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_fact_revision_immutable_delete BEFORE DELETE ON topic_fact_revision BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_fact_revision_immutable_insert BEFORE INSERT ON topic_fact_revision WHEN EXISTS (SELECT 1 FROM topic_fact_revision WHERE (id IS NEW.id) OR (case_id IS NEW.case_id AND stable_fact_id IS NEW.stable_fact_id AND revision IS NEW.revision)) BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_mutation_receipt_immutable_update BEFORE UPDATE ON topic_mutation_receipt BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_mutation_receipt_immutable_delete BEFORE DELETE ON topic_mutation_receipt BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_mutation_receipt_immutable_insert BEFORE INSERT ON topic_mutation_receipt WHEN EXISTS (SELECT 1 FROM topic_mutation_receipt WHERE (owner_id IS NEW.owner_id AND mutation_id IS NEW.mutation_id)) BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_condition_revision_immutable_update BEFORE UPDATE ON topic_condition_revision BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_condition_revision_immutable_delete BEFORE DELETE ON topic_condition_revision BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_condition_revision_immutable_insert BEFORE INSERT ON topic_condition_revision WHEN EXISTS (SELECT 1 FROM topic_condition_revision WHERE (condition_id IS NEW.condition_id AND revision IS NEW.revision)) BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_check_revision_immutable_update BEFORE UPDATE ON topic_check_revision BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_check_revision_immutable_delete BEFORE DELETE ON topic_check_revision BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_topic_check_revision_immutable_insert BEFORE INSERT ON topic_check_revision WHEN EXISTS (SELECT 1 FROM topic_check_revision WHERE (id IS NEW.id) OR (owner_id IS NEW.owner_id AND condition_id IS NEW.condition_id AND condition_revision IS NEW.condition_revision AND semantic_input_hash IS NEW.semantic_input_hash AND evaluator_version IS NEW.evaluator_version)) BEGIN SELECT RAISE(ABORT, 'bank topic history and receipts are immutable'); END",
)

_TABLES = ('topic_check_revision', 'topic_condition_head', 'topic_condition_revision', 'topic_mutation_receipt', 'topic_fact_revision', 'topic_revision', 'topic_case')


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    for table in _TABLES:
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("063 downgrade refused: bank topic history must be retained")
    for table in _TABLES:
        op.drop_table(table)

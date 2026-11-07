"""Frozen research-quality storage and narrow artifact guards.

Revision ID: 065
Revises: 064

No runtime model imports; populated experiment history is never dropped.
"""

import sqlalchemy as sa
from alembic import op

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None

_STATEMENTS = (
    "CREATE TABLE research_experiment (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tspec_artifact_id VARCHAR(36), \n\tspec_hash VARCHAR(64), \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_quality_experiment_revision CHECK (revision >= 1), \n\tCONSTRAINT ck_quality_experiment_state CHECK (state IN ('draft','sealed','running','blocked','cancelling','cancelled','awaiting_review','reported')), \n\tCONSTRAINT ck_quality_experiment_spec CHECK ((spec_artifact_id IS NULL AND spec_hash IS NULL) OR (spec_artifact_id IS NOT NULL AND spec_hash IS NOT NULL AND length(spec_hash) = 64)), \n\tFOREIGN KEY(spec_artifact_id) REFERENCES investor_method_artifact (id)\n)",
    'CREATE INDEX ix_research_experiment_owner_created ON research_experiment (owner_id, created_at, id)',
    "CREATE TABLE research_trial (\n\tid VARCHAR(36) NOT NULL, \n\texperiment_id VARCHAR(36) NOT NULL, \n\tarm VARCHAR(16) NOT NULL, \n\trepeat_index INTEGER NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\ttask_id VARCHAR(36), \n\tresult_artifact_id VARCHAR(36), \n\terror_code VARCHAR(160), \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_quality_trial_arm_repeat UNIQUE (experiment_id, arm, repeat_index), \n\tCONSTRAINT ck_quality_trial_arm CHECK (arm IN ('baseline','candidate')), \n\tCONSTRAINT ck_quality_trial_repeat CHECK (repeat_index BETWEEN 1 AND 2), \n\tCONSTRAINT ck_quality_trial_state CHECK (state IN ('planned','queued','running','completed','failed','cancelled','blocked')), \n\tFOREIGN KEY(experiment_id) REFERENCES research_experiment (id), \n\tUNIQUE (task_id), \n\tFOREIGN KEY(task_id) REFERENCES agent_task (id), \n\tFOREIGN KEY(result_artifact_id) REFERENCES investor_method_artifact (id)\n)",
    'CREATE INDEX ix_research_trial_experiment_state ON research_trial (experiment_id, state)',
    'CREATE TABLE research_experiment_task (\n\ttask_id VARCHAR(36) NOT NULL, \n\ttrial_id VARCHAR(36) NOT NULL, \n\tenvelope_artifact_id VARCHAR(36) NOT NULL, \n\tenvelope_hash VARCHAR(64) NOT NULL, \n\tmethod_binding_hash VARCHAR(64) NOT NULL, \n\tmodel_config_hash VARCHAR(64) NOT NULL, \n\tprofile_version VARCHAR(80) NOT NULL, \n\tPRIMARY KEY (task_id), \n\tCONSTRAINT ck_quality_binding_hashes CHECK (length(envelope_hash) = 64 AND length(method_binding_hash) = 64 AND length(model_config_hash) = 64), \n\tCONSTRAINT ck_quality_binding_profile CHECK (length(profile_version) > 0), \n\tFOREIGN KEY(task_id) REFERENCES agent_task (id), \n\tUNIQUE (trial_id), \n\tFOREIGN KEY(trial_id) REFERENCES research_trial (id), \n\tFOREIGN KEY(envelope_artifact_id) REFERENCES investor_method_artifact (id)\n)\n WITHOUT ROWID',
    "CREATE TABLE research_assessment (\n\tid VARCHAR(36) NOT NULL, \n\texperiment_id VARCHAR(36) NOT NULL, \n\ttrial_id VARCHAR(36) NOT NULL, \n\treviewer_id VARCHAR(32) NOT NULL, \n\trevision INTEGER NOT NULL, \n\tpayload_hash VARCHAR(64) NOT NULL, \n\tpayload JSON NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_quality_assessment_revision UNIQUE (trial_id, reviewer_id, revision), \n\tCONSTRAINT ck_quality_assessment_revision CHECK (revision >= 1), \n\tCONSTRAINT ck_quality_assessment_hash CHECK (length(payload_hash) = 64), \n\tCONSTRAINT ck_quality_assessment_payload CHECK (json_valid(payload) AND json_type(payload) = 'object'), \n\tFOREIGN KEY(experiment_id) REFERENCES research_experiment (id), \n\tFOREIGN KEY(trial_id) REFERENCES research_trial (id)\n)\n WITHOUT ROWID",
    "CREATE TABLE research_experiment_receipt (\n\towner_id VARCHAR(32) NOT NULL, \n\tmutation_id VARCHAR(36) NOT NULL, \n\trequest_object_id VARCHAR(36), \n\trequest_hash VARCHAR(64) NOT NULL, \n\tpayload JSON NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (owner_id, mutation_id), \n\tCONSTRAINT ck_quality_receipt_hash CHECK (length(request_hash) = 64), \n\tCONSTRAINT ck_quality_receipt_payload CHECK (json_valid(payload) AND json_type(payload) = 'object'), \n\tFOREIGN KEY(request_object_id) REFERENCES research_experiment (id)\n)\n WITHOUT ROWID",
    "CREATE TRIGGER IF NOT EXISTS trg_research_experiment_task_immutable_update BEFORE UPDATE ON research_experiment_task BEGIN SELECT RAISE(ABORT, 'research quality history is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_experiment_task_immutable_delete BEFORE DELETE ON research_experiment_task BEGIN SELECT RAISE(ABORT, 'research quality history is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_experiment_task_immutable_insert BEFORE INSERT ON research_experiment_task WHEN EXISTS (SELECT 1 FROM research_experiment_task WHERE task_id = NEW.task_id OR trial_id = NEW.trial_id) BEGIN SELECT RAISE(ABORT, 'research quality history identity exists'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_assessment_immutable_update BEFORE UPDATE ON research_assessment BEGIN SELECT RAISE(ABORT, 'research quality history is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_assessment_immutable_delete BEFORE DELETE ON research_assessment BEGIN SELECT RAISE(ABORT, 'research quality history is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_assessment_immutable_insert BEFORE INSERT ON research_assessment WHEN EXISTS (SELECT 1 FROM research_assessment WHERE id = NEW.id OR (trial_id = NEW.trial_id AND reviewer_id = NEW.reviewer_id AND revision = NEW.revision)) BEGIN SELECT RAISE(ABORT, 'research quality history identity exists'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_experiment_receipt_immutable_update BEFORE UPDATE ON research_experiment_receipt BEGIN SELECT RAISE(ABORT, 'research quality history is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_experiment_receipt_immutable_delete BEFORE DELETE ON research_experiment_receipt BEGIN SELECT RAISE(ABORT, 'research quality history is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_experiment_receipt_immutable_insert BEFORE INSERT ON research_experiment_receipt WHEN EXISTS (SELECT 1 FROM research_experiment_receipt WHERE owner_id = NEW.owner_id AND mutation_id = NEW.mutation_id) BEGIN SELECT RAISE(ABORT, 'research quality history identity exists'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_research_assessment_continuous BEFORE INSERT ON research_assessment WHEN NEW.revision != COALESCE((SELECT MAX(revision) + 1 FROM research_assessment WHERE trial_id = NEW.trial_id AND reviewer_id = NEW.reviewer_id), 1) OR NOT EXISTS (SELECT 1 FROM research_trial WHERE id = NEW.trial_id AND experiment_id = NEW.experiment_id) OR NOT EXISTS (SELECT 1 FROM research_experiment WHERE id = NEW.experiment_id) BEGIN SELECT RAISE(ABORT, 'assessment requires existing trial and consecutive revision'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_quality_artifact_immutable_update BEFORE UPDATE ON investor_method_artifact WHEN OLD.kind IN ('quality_case','quality_envelope','quality_spec','quality_result','quality_report','quality_blind_map') OR NEW.kind IN ('quality_case','quality_envelope','quality_spec','quality_result','quality_report','quality_blind_map') OR EXISTS (SELECT 1 FROM investor_method_artifact WHERE (id = NEW.id OR rowid = NEW.rowid) AND kind IN ('quality_case','quality_envelope','quality_spec','quality_result','quality_report','quality_blind_map')) BEGIN SELECT RAISE(ABORT, 'quality artifacts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_quality_artifact_immutable_delete BEFORE DELETE ON investor_method_artifact WHEN OLD.kind IN ('quality_case','quality_envelope','quality_spec','quality_result','quality_report','quality_blind_map') BEGIN SELECT RAISE(ABORT, 'quality artifacts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_quality_artifact_immutable_insert BEFORE INSERT ON investor_method_artifact WHEN EXISTS (SELECT 1 FROM investor_method_artifact WHERE (id = NEW.id OR rowid = NEW.rowid) AND (kind IN ('quality_case','quality_envelope','quality_spec','quality_result','quality_report','quality_blind_map') OR NEW.kind IN ('quality_case','quality_envelope','quality_spec','quality_result','quality_report','quality_blind_map'))) BEGIN SELECT RAISE(ABORT, 'quality artifact identity exists'); END",
)

_TABLES = ('research_experiment_receipt', 'research_assessment', 'research_experiment_task', 'research_trial', 'research_experiment')
_ARTIFACT_KINDS = "('quality_case','quality_envelope','quality_spec','quality_result','quality_report','quality_blind_map')"


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    for table in _TABLES:
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("065 downgrade refused: quality history must be retained")
    if connection.execute(sa.text(
        f"SELECT 1 FROM investor_method_artifact WHERE kind IN {_ARTIFACT_KINDS} LIMIT 1"
    )).first() is not None:
        raise RuntimeError("065 downgrade refused: quality artifacts must be retained")
    for action in ("update", "delete", "insert"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_quality_artifact_immutable_{action}")
    for table in _TABLES:
        op.drop_table(table)

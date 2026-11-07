"""Static portfolio stress heads, frozen inputs and operation receipts.

Revision ID: 064
Revises: 063

Frozen SQLite DDL; no live application imports or destructive data downgrade.
History tables deliberately have no hidden rowid.
"""

import sqlalchemy as sa
from alembic import op

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None

_STATEMENTS = (
    "CREATE TABLE portfolio_stress_head (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\taccount_id VARCHAR(36) NOT NULL, \n\trevision INTEGER DEFAULT '1' NOT NULL, \n\tcontrol_revision INTEGER DEFAULT '1' NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tpermission_epoch INTEGER DEFAULT '1' NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_portfolio_stress_revision CHECK (revision >= 1), \n\tCONSTRAINT ck_portfolio_stress_control_revision CHECK (control_revision >= 1), \n\tCONSTRAINT ck_portfolio_stress_permission_epoch CHECK (permission_epoch >= 1), \n\tFOREIGN KEY(account_id) REFERENCES portfolio_account (id), \n\tCONSTRAINT ck_portfolio_stress_enabled CHECK (enabled IN (0, 1))\n)",
    'CREATE INDEX ix_portfolio_stress_owner_account_created ON portfolio_stress_head (owner_id, account_id, created_at, id)',
    "CREATE TABLE portfolio_stress_revision (\n\tstress_id VARCHAR(36) NOT NULL, \n\trevision INTEGER NOT NULL, \n\towner_id VARCHAR(32) NOT NULL, \n\taccount_id VARCHAR(36) NOT NULL, \n\tinput_hash VARCHAR(64) NOT NULL, \n\tresult_id VARCHAR(36) NOT NULL, \n\trequested_at DATETIME NOT NULL, \n\tpayload JSON NOT NULL, \n\tPRIMARY KEY (stress_id, revision), \n\tCONSTRAINT uq_portfolio_stress_result UNIQUE (owner_id, result_id), \n\tCONSTRAINT ck_portfolio_stress_history_revision CHECK (revision >= 1), \n\tCONSTRAINT ck_portfolio_stress_input_hash CHECK (length(input_hash) = 64), \n\tCONSTRAINT ck_portfolio_stress_payload CHECK (json_valid(payload) AND json_type(payload) = 'object'), \n\tFOREIGN KEY(stress_id) REFERENCES portfolio_stress_head (id), \n\tFOREIGN KEY(account_id) REFERENCES portfolio_account (id)\n)\n WITHOUT ROWID",
    'CREATE INDEX ix_portfolio_stress_history_account ON portfolio_stress_revision (owner_id, account_id, requested_at)',
    "CREATE TABLE portfolio_study_operation (\n\towner_id VARCHAR(32) NOT NULL, \n\toperation_id VARCHAR(36) NOT NULL, \n\taction VARCHAR(80) NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tresponse_payload JSON NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (owner_id, operation_id), \n\tCONSTRAINT ck_portfolio_study_action CHECK (length(action) > 0), \n\tCONSTRAINT ck_portfolio_study_request_hash CHECK (length(request_hash) = 64), \n\tCONSTRAINT ck_portfolio_study_response CHECK (json_valid(response_payload) AND json_type(response_payload) = 'object')\n)\n WITHOUT ROWID",
    'CREATE INDEX ix_portfolio_study_operation_created ON portfolio_study_operation (owner_id, created_at)',
    "CREATE TRIGGER IF NOT EXISTS trg_portfolio_stress_revision_immutable_update BEFORE UPDATE ON portfolio_stress_revision BEGIN SELECT RAISE(ABORT, 'portfolio study history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_portfolio_stress_revision_immutable_delete BEFORE DELETE ON portfolio_stress_revision BEGIN SELECT RAISE(ABORT, 'portfolio study history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_portfolio_stress_revision_immutable_insert BEFORE INSERT ON portfolio_stress_revision WHEN EXISTS (SELECT 1 FROM portfolio_stress_revision WHERE (stress_id IS NEW.stress_id AND revision IS NEW.revision) OR (owner_id IS NEW.owner_id AND result_id IS NEW.result_id)) BEGIN SELECT RAISE(ABORT, 'portfolio study history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_portfolio_study_operation_immutable_update BEFORE UPDATE ON portfolio_study_operation BEGIN SELECT RAISE(ABORT, 'portfolio study history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_portfolio_study_operation_immutable_delete BEFORE DELETE ON portfolio_study_operation BEGIN SELECT RAISE(ABORT, 'portfolio study history and receipts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_portfolio_study_operation_immutable_insert BEFORE INSERT ON portfolio_study_operation WHEN EXISTS (SELECT 1 FROM portfolio_study_operation WHERE (owner_id IS NEW.owner_id AND operation_id IS NEW.operation_id)) BEGIN SELECT RAISE(ABORT, 'portfolio study history and receipts are immutable'); END",
)

_TABLES = ('portfolio_study_operation', 'portfolio_stress_revision', 'portfolio_stress_head')


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    for table in _TABLES:
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("064 downgrade refused: portfolio study history must be retained")
    for table in _TABLES:
        op.drop_table(table)

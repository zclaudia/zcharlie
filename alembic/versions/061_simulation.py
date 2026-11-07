"""Independent daily-forward simulation accounts and immutable facts.

Revision ID: 061
Revises: 060
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "061"
down_revision: str | Sequence[str] | None = "060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "simulation_experiment",
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("revision >= 0", name="ck_sim_experiment_revision"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_simulation_experiment_status"), "simulation_experiment", ["status"], unique=False
    )
    op.create_table(
        "simulation_request",
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("result_id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "idempotency_key"),
    )
    op.create_table(
        "simulation_execution_evidence",
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=160), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.CheckConstraint(
            "revision > 0 AND listing_id > 0", name="ck_sim_execution_evidence_revision"
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "content_hash", name="uq_sim_execution_evidence_hash"),
        sa.UniqueConstraint(
            "experiment_id",
            "listing_id",
            "trade_date",
            "revision",
            name="uq_sim_execution_evidence_revision",
        ),
    )
    op.create_index(
        op.f("ix_simulation_execution_evidence_experiment_id"),
        "simulation_execution_evidence",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_execution_evidence_trade_date"),
        "simulation_execution_evidence",
        ["trade_date"],
        unique=False,
    )
    op.create_table(
        "simulation_participant",
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id"),
        sa.CheckConstraint("version > 0", name="ck_sim_participant_version"),
        sa.UniqueConstraint("experiment_id", "account_id", name="uq_sim_participant_account"),
        sa.UniqueConstraint("experiment_id", "name", "version", name="uq_sim_participant_version"),
    )
    op.create_index(
        op.f("ix_simulation_participant_experiment_id"),
        "simulation_participant",
        ["experiment_id"],
        unique=False,
    )
    op.create_table(
        "simulation_round",
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("execution_date", sa.Date(), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_sim_round_revision"),
        sa.CheckConstraint(
            "cutoff < decision_deadline AND decision_deadline < execution_at "
            "AND observation_date < execution_date",
            name="ck_sim_round_clock",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "observation_date", name="uq_sim_round_observation"),
    )
    op.create_index(
        op.f("ix_simulation_round_experiment_id"),
        "simulation_round",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_round_status"), "simulation_round", ["status"], unique=False
    )
    op.create_table(
        "simulation_account",
        sa.Column("participant_id", sa.String(length=160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_sim_account_revision"),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["simulation_participant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id"),
    )
    op.create_index(
        op.f("ix_simulation_account_experiment_id"),
        "simulation_account",
        ["experiment_id"],
        unique=False,
    )
    op.create_table(
        "simulation_cost",
        sa.Column("round_id", sa.String(length=160), nullable=False),
        sa.Column("call_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("participant_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["simulation_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["simulation_participant.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["simulation_round.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "call_key", name="uq_sim_cost_call"),
    )
    op.create_index(
        op.f("ix_simulation_cost_account_id"), "simulation_cost", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_cost_experiment_id"), "simulation_cost", ["experiment_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_cost_participant_id"),
        "simulation_cost",
        ["participant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_cost_round_id"), "simulation_cost", ["round_id"], unique=False
    )
    op.create_table(
        "simulation_decision",
        sa.Column("round_id", sa.String(length=160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("account_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("participant_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.CheckConstraint(
            "revision >= 0 AND account_revision >= 0", name="ck_sim_decision_revision"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["simulation_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["simulation_participant.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["simulation_round.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_sim_decision_key"),
        sa.UniqueConstraint(
            "participant_id", "round_id", "revision", name="uq_sim_decision_revision"
        ),
    )
    op.create_index(
        op.f("ix_simulation_decision_account_id"),
        "simulation_decision",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_decision_experiment_id"),
        "simulation_decision",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_decision_participant_id"),
        "simulation_decision",
        ["participant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_decision_round_id"), "simulation_decision", ["round_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_decision_status"), "simulation_decision", ["status"], unique=False
    )
    op.create_index(
        "uq_sim_decision_final",
        "simulation_decision",
        ["participant_id", "round_id"],
        unique=True,
        sqlite_where=sa.text("status = 'locked'"),
        postgresql_where=sa.text("status = 'locked'"),
    )
    op.create_table(
        "simulation_ledger_event",
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("account_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("economic_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("participant_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.CheckConstraint("sequence > 0 AND account_revision > 0", name="ck_sim_ledger_sequence"),
        sa.CheckConstraint("economic_at <= confirmed_at", name="ck_sim_ledger_clock"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["simulation_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["simulation_participant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "account_revision", name="uq_sim_ledger_revision"),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_sim_ledger_key"),
        sa.UniqueConstraint("account_id", "sequence", name="uq_sim_ledger_sequence"),
    )
    op.create_index(
        op.f("ix_simulation_ledger_event_account_id"),
        "simulation_ledger_event",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_ledger_event_experiment_id"),
        "simulation_ledger_event",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_ledger_event_participant_id"),
        "simulation_ledger_event",
        ["participant_id"],
        unique=False,
    )
    op.create_table(
        "simulation_nav",
        sa.Column("account_revision", sa.Integer(), nullable=False),
        sa.Column("valuation_date", sa.Date(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("quality", sa.String(length=32), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("valuation_version", sa.String(length=160), nullable=False),
        sa.Column("metrics_version", sa.String(length=160), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("participant_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.CheckConstraint("version > 0 AND account_revision >= 0", name="ck_sim_nav_revision"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["simulation_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["simulation_participant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "valuation_date",
            "account_revision",
            "input_hash",
            "valuation_version",
            "metrics_version",
            name="uq_sim_nav_input",
        ),
        sa.UniqueConstraint("account_id", "valuation_date", "version", name="uq_sim_nav_version"),
    )
    op.create_index(
        op.f("ix_simulation_nav_account_id"), "simulation_nav", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_nav_experiment_id"), "simulation_nav", ["experiment_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_nav_participant_id"), "simulation_nav", ["participant_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_nav_valuation_date"), "simulation_nav", ["valuation_date"], unique=False
    )
    op.create_table(
        "simulation_order",
        sa.Column("round_id", sa.String(length=160), nullable=False),
        sa.Column("decision_id", sa.String(length=160), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("account_revision", sa.Integer(), nullable=False),
        sa.Column("execution_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("participant_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.CheckConstraint("sequence > 0 AND account_revision >= 0", name="ck_sim_order_sequence"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["simulation_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["simulation_decision.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["simulation_participant.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["simulation_round.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_sim_order_key"),
        sa.UniqueConstraint("account_id", "round_id", "listing_id", name="uq_sim_order_direction"),
        sa.UniqueConstraint("decision_id", "sequence", name="uq_sim_order_sequence"),
    )
    op.create_index(
        op.f("ix_simulation_order_account_id"), "simulation_order", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_order_execution_date"),
        "simulation_order",
        ["execution_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_order_experiment_id"),
        "simulation_order",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_order_participant_id"),
        "simulation_order",
        ["participant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_order_round_id"), "simulation_order", ["round_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_order_status"), "simulation_order", ["status"], unique=False
    )
    op.create_table(
        "simulation_review",
        sa.Column("decision_id", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("participant_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["simulation_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["simulation_decision.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["simulation_participant.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("decision_id", "version", name="uq_sim_review_version"),
        sa.CheckConstraint("version > 0", name="ck_sim_review_version"),
    )
    op.create_index(
        op.f("ix_simulation_review_account_id"), "simulation_review", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_review_available_at"),
        "simulation_review",
        ["available_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_review_decision_id"), "simulation_review", ["decision_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_review_experiment_id"),
        "simulation_review",
        ["experiment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_review_participant_id"),
        "simulation_review",
        ["participant_id"],
        unique=False,
    )
    op.create_table(
        "simulation_fill",
        sa.Column("round_id", sa.String(length=160), nullable=False),
        sa.Column("order_id", sa.String(length=160), nullable=False),
        sa.Column("execution_key", sa.String(length=160), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("economic_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("participant_id", sa.String(length=160), nullable=False),
        sa.Column("account_id", sa.String(length=160), nullable=False),
        sa.Column("experiment_id", sa.String(length=160), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_sim_fill_sequence"),
        sa.CheckConstraint("economic_at <= confirmed_at", name="ck_sim_fill_clock"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["simulation_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["simulation_experiment.id"],
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["simulation_order.id"],
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"],
            ["simulation_participant.id"],
        ),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["simulation_round.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "execution_key", name="uq_sim_fill_key"),
        sa.UniqueConstraint("order_id", "execution_key", "sequence", name="uq_sim_fill_execution"),
    )
    op.create_index(
        op.f("ix_simulation_fill_account_id"), "simulation_fill", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_fill_experiment_id"), "simulation_fill", ["experiment_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_fill_order_id"), "simulation_fill", ["order_id"], unique=False
    )
    op.create_index(
        op.f("ix_simulation_fill_participant_id"),
        "simulation_fill",
        ["participant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_simulation_fill_round_id"), "simulation_fill", ["round_id"], unique=False
    )
    op.execute(
        "CREATE TRIGGER simulation_ledger_event_no_update BEFORE UPDATE ON simulation_ledger_event BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER simulation_ledger_event_no_delete BEFORE DELETE ON simulation_ledger_event BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER simulation_fill_no_update BEFORE UPDATE ON simulation_fill BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER simulation_fill_no_delete BEFORE DELETE ON simulation_fill BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER simulation_nav_no_update BEFORE UPDATE ON simulation_nav BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER simulation_nav_no_delete BEFORE DELETE ON simulation_nav BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER simulation_execution_evidence_no_update BEFORE UPDATE ON simulation_execution_evidence BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER simulation_execution_evidence_no_delete BEFORE DELETE ON simulation_execution_evidence BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_simulation_fill_round_id"), table_name="simulation_fill")
    op.drop_index(op.f("ix_simulation_fill_participant_id"), table_name="simulation_fill")
    op.drop_index(op.f("ix_simulation_fill_order_id"), table_name="simulation_fill")
    op.drop_index(op.f("ix_simulation_fill_experiment_id"), table_name="simulation_fill")
    op.drop_index(op.f("ix_simulation_fill_account_id"), table_name="simulation_fill")
    op.drop_table("simulation_fill")
    op.drop_index(op.f("ix_simulation_review_participant_id"), table_name="simulation_review")
    op.drop_index(op.f("ix_simulation_review_experiment_id"), table_name="simulation_review")
    op.drop_index(op.f("ix_simulation_review_decision_id"), table_name="simulation_review")
    op.drop_index(op.f("ix_simulation_review_available_at"), table_name="simulation_review")
    op.drop_index(op.f("ix_simulation_review_account_id"), table_name="simulation_review")
    op.drop_table("simulation_review")
    op.drop_index(op.f("ix_simulation_order_status"), table_name="simulation_order")
    op.drop_index(op.f("ix_simulation_order_round_id"), table_name="simulation_order")
    op.drop_index(op.f("ix_simulation_order_participant_id"), table_name="simulation_order")
    op.drop_index(op.f("ix_simulation_order_experiment_id"), table_name="simulation_order")
    op.drop_index(op.f("ix_simulation_order_execution_date"), table_name="simulation_order")
    op.drop_index(op.f("ix_simulation_order_account_id"), table_name="simulation_order")
    op.drop_table("simulation_order")
    op.drop_index(op.f("ix_simulation_nav_valuation_date"), table_name="simulation_nav")
    op.drop_index(op.f("ix_simulation_nav_participant_id"), table_name="simulation_nav")
    op.drop_index(op.f("ix_simulation_nav_experiment_id"), table_name="simulation_nav")
    op.drop_index(op.f("ix_simulation_nav_account_id"), table_name="simulation_nav")
    op.drop_table("simulation_nav")
    op.drop_index(
        op.f("ix_simulation_ledger_event_participant_id"), table_name="simulation_ledger_event"
    )
    op.drop_index(
        op.f("ix_simulation_ledger_event_experiment_id"), table_name="simulation_ledger_event"
    )
    op.drop_index(
        op.f("ix_simulation_ledger_event_account_id"), table_name="simulation_ledger_event"
    )
    op.drop_table("simulation_ledger_event")
    op.drop_index(
        "uq_sim_decision_final",
        table_name="simulation_decision",
        sqlite_where=sa.text("status = 'locked'"),
        postgresql_where=sa.text("status = 'locked'"),
    )
    op.drop_index(op.f("ix_simulation_decision_status"), table_name="simulation_decision")
    op.drop_index(op.f("ix_simulation_decision_round_id"), table_name="simulation_decision")
    op.drop_index(op.f("ix_simulation_decision_participant_id"), table_name="simulation_decision")
    op.drop_index(op.f("ix_simulation_decision_experiment_id"), table_name="simulation_decision")
    op.drop_index(op.f("ix_simulation_decision_account_id"), table_name="simulation_decision")
    op.drop_table("simulation_decision")
    op.drop_index(op.f("ix_simulation_cost_round_id"), table_name="simulation_cost")
    op.drop_index(op.f("ix_simulation_cost_participant_id"), table_name="simulation_cost")
    op.drop_index(op.f("ix_simulation_cost_experiment_id"), table_name="simulation_cost")
    op.drop_index(op.f("ix_simulation_cost_account_id"), table_name="simulation_cost")
    op.drop_table("simulation_cost")
    op.drop_index(op.f("ix_simulation_account_experiment_id"), table_name="simulation_account")
    op.drop_table("simulation_account")
    op.drop_index(op.f("ix_simulation_round_status"), table_name="simulation_round")
    op.drop_index(op.f("ix_simulation_round_experiment_id"), table_name="simulation_round")
    op.drop_table("simulation_round")
    op.drop_index(
        op.f("ix_simulation_participant_experiment_id"), table_name="simulation_participant"
    )
    op.drop_table("simulation_participant")
    op.drop_index(
        op.f("ix_simulation_execution_evidence_trade_date"),
        table_name="simulation_execution_evidence",
    )
    op.drop_index(
        op.f("ix_simulation_execution_evidence_experiment_id"),
        table_name="simulation_execution_evidence",
    )
    op.drop_table("simulation_execution_evidence")
    op.drop_table("simulation_request")
    op.drop_index(op.f("ix_simulation_experiment_status"), table_name="simulation_experiment")
    op.drop_table("simulation_experiment")

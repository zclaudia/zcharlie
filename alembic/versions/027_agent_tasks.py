"""Durable tasks, attempts, action ledger and public events; revision 027."""

import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_task",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("input_key", sa.String(128), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("pipeline_run.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("resolved", sa.JSON(), nullable=False),
        sa.Column("consumed_events", sa.JSON(), nullable=False),
        sa.Column("wait_token", sa.String(36), nullable=True),
        sa.UniqueConstraint("owner_id", "input_key"),
    )
    op.create_table(
        "agent_task_attempt",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("agent_task.id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("pipeline_run.id"), nullable=False),
        sa.Column("lease_attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.UniqueConstraint("run_id", "lease_attempt"),
    )
    op.create_table(
        "agent_tool_action",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("agent_task.id"), nullable=False),
        sa.Column("intent_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("effect", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.UniqueConstraint("task_id", "intent_key"),
    )
    op.create_table(
        "agent_task_event",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("agent_task.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_agent_task_event_task_id", "agent_task_event", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_task_event_task_id", table_name="agent_task_event")
    for table in ("agent_task_event", "agent_tool_action", "agent_task_attempt", "agent_task"):
        op.drop_table(table)

"""Trusted task grants and atomic per-request execution budget receipts."""

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_model_run") as batch:
        batch.alter_column("cost_usd", existing_type=sa.Numeric(12, 6), type_=sa.Numeric(24, 12))
    op.create_table(
        "agent_execution_grant",
        sa.Column("task_id", sa.String(), sa.ForeignKey("agent_task.id"), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "agent_budget_entry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("agent_task.id"), nullable=False),
        sa.Column("attempt_id", sa.String(), sa.ForeignKey("agent_task_attempt.id"), nullable=False),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(24, 12)),
        sa.Column("price_version", sa.String(128)),
        sa.Column("input_price", sa.Numeric(18, 8)),
        sa.Column("output_price", sa.Numeric(18, 8)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(24, 12)),
        sa.Column("unknown_reason", sa.String(128)),
    )
    for column in ("task_id", "owner_id", "day"):
        op.create_index(f"ix_agent_budget_entry_{column}", "agent_budget_entry", [column])


def downgrade() -> None:
    with op.batch_alter_table("agent_model_run") as batch:
        batch.alter_column("cost_usd", existing_type=sa.Numeric(24, 12), type_=sa.Numeric(12, 6))
    op.drop_table("agent_budget_entry")
    op.drop_table("agent_execution_grant")

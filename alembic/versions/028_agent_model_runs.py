"""Independent Agent model call audit with explicit unknown usage/price."""

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_model_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pipeline_run_id", sa.Integer(), sa.ForeignKey("pipeline_run.id")),
        sa.Column("task_id", sa.String(), sa.ForeignKey("agent_task.id")),
        sa.Column("attempt_id", sa.String(), sa.ForeignKey("agent_task_attempt.id")),
        sa.Column("config_version", sa.String(96), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("framework_version", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.Column("framework_requests", sa.Integer(), nullable=False),
        sa.Column("http_attempts", sa.Integer()),
        sa.Column("http_responses", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON()),
        sa.Column("usage_unknown_reason", sa.String(128)),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        sa.Column("cost_unknown_reason", sa.String(128)),
    )


def downgrade() -> None:
    op.drop_table("agent_model_run")

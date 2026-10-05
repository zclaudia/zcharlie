"""add persistent pipeline run queue fields

Revision ID: 009
Revises: 008
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: str | Sequence[str] | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pipeline_run") as batch:
        batch.add_column(
            sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False)
        )
        batch.add_column(
            sa.Column(
                "request_json",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            )
        )
        batch.add_column(sa.Column("available_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("worker_id", sa.String(length=128)))
        batch.create_index(
            "ix_pipeline_run_queue",
            ["status", "available_at", "id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("pipeline_run") as batch:
        batch.drop_index("ix_pipeline_run_queue")
        batch.drop_column("worker_id")
        batch.drop_column("claimed_at")
        batch.drop_column("available_at")
        batch.drop_column("request_json")
        batch.drop_column("max_attempts")

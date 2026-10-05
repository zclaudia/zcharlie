"""add durable pipeline run lifecycle fields

Revision ID: 008
Revises: 007
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: str | Sequence[str] | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pipeline_run") as batch:
        batch.add_column(
            sa.Column(
                "stage",
                sa.String(length=32),
                server_default="created",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("idempotency_key", sa.String(length=128)))
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
        batch.create_unique_constraint(
            "uq_pipeline_run_idempotency",
            ["kind", "idempotency_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("pipeline_run") as batch:
        batch.drop_constraint("uq_pipeline_run_idempotency", type_="unique")
        batch.drop_column("heartbeat_at")
        batch.drop_column("attempt_count")
        batch.drop_column("idempotency_key")
        batch.drop_column("stage")

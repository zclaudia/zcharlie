"""Bounded native queue cursor and optional delivery-only outbox."""

import sqlalchemy as sa
from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_operations_cursor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "investor_notification_delivery",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("notification_key", sa.String(160), nullable=False),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("investor_case.id"), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_investor_notification_delivery_notification_key",
        "investor_notification_delivery",
        ["notification_key"],
    )
    op.create_index(
        "ix_investor_notification_delivery_state", "investor_notification_delivery", ["state"]
    )


def downgrade() -> None:
    op.drop_table("investor_notification_delivery")
    op.drop_table("agent_operations_cursor")

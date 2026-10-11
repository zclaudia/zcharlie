"""Monitoring receipts, review cycles and in-app reminders."""
import sqlalchemy as sa
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("investor_monitor_receipt",
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("investor_case.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False))
    op.create_index("ix_investor_monitor_receipt_case_id", "investor_monitor_receipt", ["case_id"])
    op.create_table("investor_review_cycle",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("investor_case.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_task.id"), nullable=True, unique=True),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False))
    for field in ("case_id", "state"):
        op.create_index(f"ix_investor_review_cycle_{field}", "investor_review_cycle", [field])
    op.create_table("investor_notification",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(160), nullable=False, unique=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("investor_case.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False), sqlite_autoincrement=True)
    op.create_index("ix_investor_notification_owner_id", "investor_notification", ["owner_id"])


def downgrade() -> None:
    for name in ("notification", "review_cycle", "monitor_receipt"):
        op.drop_table(f"investor_{name}")

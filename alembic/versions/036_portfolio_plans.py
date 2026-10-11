"""Whole-account mutually exclusive plan CAS head and immutable audit artifacts."""
import sqlalchemy as sa
from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("portfolio_plan_head", sa.Column("account_id", sa.String(36), sa.ForeignKey("portfolio_account.id"), primary_key=True), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("plan_id", sa.String(36), nullable=True), sa.Column("checked_at", sa.String(40), nullable=True))
    op.create_table("portfolio_plan_artifact", sa.Column("id", sa.String(36), primary_key=True), sa.Column("owner_id", sa.String(32), nullable=False), sa.Column("account_id", sa.String(36), sa.ForeignKey("portfolio_account.id"), nullable=False), sa.Column("kind", sa.String(24), nullable=False), sa.Column("payload", sa.JSON(), nullable=False))
    op.create_index("ix_portfolio_plan_artifact_owner_id", "portfolio_plan_artifact", ["owner_id"])
    op.create_index("ix_portfolio_plan_artifact_account_id", "portfolio_plan_artifact", ["account_id"])
    op.create_table("portfolio_plan_event", sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("plan_id", sa.String(36), sa.ForeignKey("portfolio_plan_artifact.id"), nullable=False), sa.Column("event_key", sa.String(96), nullable=False), sa.Column("payload", sa.JSON(), nullable=False), sa.UniqueConstraint("plan_id", "event_key"), sqlite_autoincrement=True)
    op.create_index("ix_portfolio_plan_event_plan_id", "portfolio_plan_event", ["plan_id"])
    op.create_table("portfolio_plan_outbox", sa.Column("key", sa.String(128), primary_key=True), sa.Column("plan_id", sa.String(36), sa.ForeignKey("portfolio_plan_artifact.id"), nullable=False), sa.Column("state", sa.String(16), nullable=False), sa.Column("payload", sa.JSON(), nullable=False))


def downgrade() -> None:
    op.drop_table("portfolio_plan_outbox")
    op.drop_table("portfolio_plan_event")
    op.drop_table("portfolio_plan_artifact")
    op.drop_table("portfolio_plan_head")

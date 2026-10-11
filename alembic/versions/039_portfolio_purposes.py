"""Immutable user-confirmed purpose envelopes over exact actual cash."""
import sqlalchemy as sa
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("portfolio_purpose_head", sa.Column("account_id", sa.String(36), sa.ForeignKey("portfolio_account.id"), primary_key=True), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("version_id", sa.String(36), nullable=False))
    op.create_table("portfolio_purpose_version", sa.Column("id", sa.String(36), primary_key=True), sa.Column("account_id", sa.String(36), sa.ForeignKey("portfolio_account.id"), nullable=False), sa.Column("owner_id", sa.String(32), nullable=False), sa.Column("input_hash", sa.String(64), nullable=False), sa.Column("payload", sa.JSON(), nullable=False))
    op.create_index("ix_portfolio_purpose_version_account_id", "portfolio_purpose_version", ["account_id"])


def downgrade() -> None:
    op.drop_table("portfolio_purpose_version")
    op.drop_table("portfolio_purpose_head")

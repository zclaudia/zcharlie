"""Daily valuation storage: the immutable projection and its point-in-time prices."""

import sqlalchemy as sa
from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("portfolio_account", sa.Column("current_valuation_id", sa.String(36), nullable=True))
    op.create_table(
        "portfolio_valuation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("portfolio_account.id"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("ledger_revision", sa.Integer(), nullable=False),
        sa.Column("valuation_date", sa.Date(), nullable=False),
        sa.Column("algorithm_version", sa.String(32), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id", "id", name="uq_portfolio_valuation_identity"),
    )
    op.create_index(
        "ix_portfolio_valuation_current",
        "portfolio_valuation",
        ["account_id", "ledger_revision", "valuation_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_portfolio_valuation_current", table_name="portfolio_valuation")
    op.drop_table("portfolio_valuation")
    op.drop_column("portfolio_account", "current_valuation_id")

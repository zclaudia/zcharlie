"""User-confirmed portfolio ledger, immutable snapshots and per-account CAS."""
import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("portfolio_account",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("current_snapshot_id", sa.String(36), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False))
    op.create_index("ix_portfolio_account_owner_id", "portfolio_account", ["owner_id"])
    op.create_table("portfolio_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("portfolio_account.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(36), nullable=False),
        sa.Column("batch_id", sa.String(36), nullable=True),
        sa.Column("row_id", sa.String(256), nullable=True),
        sa.Column("reverses_id", sa.String(36), sa.ForeignKey("portfolio_event.id"), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("fact_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "revision", name="uq_portfolio_event_revision"),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_portfolio_event_key"),
        sa.UniqueConstraint("account_id", "batch_id", "row_id", name="uq_portfolio_import_row"),
        sa.UniqueConstraint("account_id", "reverses_id", name="uq_portfolio_reversal"))
    op.create_table("portfolio_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("portfolio_account.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "revision", name="uq_portfolio_snapshot_revision"))

    op.create_table("portfolio_request",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("portfolio_account.id"), primary_key=True),
        sa.Column("idempotency_key", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("portfolio_event.id"), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False))


def downgrade() -> None:
    op.drop_table("portfolio_request")
    op.drop_table("portfolio_snapshot")
    op.drop_table("portfolio_event")
    op.drop_table("portfolio_account")

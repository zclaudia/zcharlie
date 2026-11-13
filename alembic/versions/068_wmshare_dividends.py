"""Isolate dividend versions by provider and store wmshare platform captures."""

import sqlalchemy as sa
from alembic import op

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dividend_event") as batch:
        batch.drop_constraint("uq_dividend_event_version", type_="unique")
        batch.create_unique_constraint(
            "uq_dividend_event_version", ["listing_id", "provider", "ex_date", "version"]
        )
    op.create_table(
        "wmshare_dividend_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_available", sa.Boolean(), nullable=False),
        sa.Column("history_available", sa.Boolean(), nullable=False),
        sa.Column("quote_date", sa.Date(), nullable=True),
        sa.Column("yield_ttm", sa.Numeric(20, 10), nullable=True),
        sa.Column("payout_ttm", sa.Numeric(20, 10), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("pending", sa.JSON(), nullable=False),
        sa.Column("raw_ref", sa.String(1024), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_wmshare_dividend_snapshot_listing_id", "wmshare_dividend_snapshot", ["listing_id"]
    )


def downgrade() -> None:
    collisions = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM dividend_event GROUP BY listing_id, ex_date, version HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if collisions:
        raise RuntimeError("Cannot downgrade: dividend versions collide across providers")
    op.drop_index("ix_wmshare_dividend_snapshot_listing_id", table_name="wmshare_dividend_snapshot")
    op.drop_table("wmshare_dividend_snapshot")
    with op.batch_alter_table("dividend_event") as batch:
        batch.drop_constraint("uq_dividend_event_version", type_="unique")
        batch.create_unique_constraint(
            "uq_dividend_event_version", ["listing_id", "ex_date", "version"]
        )

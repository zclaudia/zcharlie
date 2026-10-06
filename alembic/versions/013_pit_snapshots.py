"""add point-in-time snapshot tables (P1-A2)

Revision ID: 013
Revises: 012
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: str | Sequence[str] | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    with op.batch_alter_table("universe_membership") as batch:
        batch.add_column(sa.Column("source", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "industry_classification",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("system", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "listing_id", "system", "valid_from", name="uq_industry_classification_interval"
        ),
    )
    op.create_index(
        "ix_industry_classification_listing_system",
        "industry_classification",
        ["listing_id", "system"],
        unique=False,
    )

    op.create_table(
        "market_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("volume", sa.Numeric(precision=24, scale=4), nullable=False),
        sa.Column("amount", sa.Numeric(precision=24, scale=4), nullable=True),
        sa.Column("total_shares", sa.Numeric(precision=24, scale=4), nullable=True),
        sa.Column("adjust", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_ref", sa.String(length=1024), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "listing_id", "trade_date", "adjust", "version", name="uq_market_snapshot_version"
        ),
    )
    op.create_index(
        "ix_market_snapshot_listing_date",
        "market_snapshot",
        ["listing_id", "trade_date"],
        unique=False,
    )

    op.create_table(
        "financial_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("period_kind", sa.String(length=16), nullable=False),
        sa.Column("available_from", sa.Date(), nullable=True),
        sa.Column("availability", sa.String(length=16), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_ref", sa.String(length=1024), nullable=True),
        sa.Column("is_backfill", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "listing_id", "report_period", "version", name="uq_financial_snapshot_version"
        ),
    )
    op.create_index(
        "ix_financial_snapshot_listing_available",
        "financial_snapshot",
        ["listing_id", "available_from"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_financial_snapshot_listing_available", table_name="financial_snapshot")
    op.drop_table("financial_snapshot")
    op.drop_index("ix_market_snapshot_listing_date", table_name="market_snapshot")
    op.drop_table("market_snapshot")
    op.drop_index(
        "ix_industry_classification_listing_system", table_name="industry_classification"
    )
    op.drop_table("industry_classification")
    with op.batch_alter_table("universe_membership") as batch:
        batch.drop_column("captured_at")
        batch.drop_column("source")

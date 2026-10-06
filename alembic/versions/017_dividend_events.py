"""dividend events and collection coverage (F2 / Q2: dividend-yield factor)

* ``dividend_event`` - one distribution (分红送转) per listing and ex-date,
  immutable and versioned (a changed re-capture inserts ``version + 1``);
* ``dividend_coverage`` - which ex-date years were collected per listing and
  provider, so "no dividend" can be told apart from "not collected".

Revision ID: 017
Revises: 016
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: str | Sequence[str] | None = "016"
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
    op.create_table(
        "dividend_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("pay_date", sa.Date(), nullable=True),
        sa.Column("announce_date", sa.Date(), nullable=True),
        sa.Column("implement_date", sa.Date(), nullable=True),
        sa.Column("available_from", sa.Date(), nullable=False),
        sa.Column("cash_per_share_pre_tax", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("stock_per_share", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("reserve_per_share", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("operate_year", sa.Integer(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_ref", sa.String(length=1024), nullable=True),
        sa.Column("is_backfill", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "ex_date", "version", name="uq_dividend_event_version"),
    )
    op.create_index(
        "ix_dividend_event_listing_ex_date",
        "dividend_event",
        ["listing_id", "ex_date"],
        unique=False,
    )
    op.create_table(
        "dividend_coverage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("first_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("is_backfill", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "provider", "year", name="uq_dividend_coverage_year"),
    )


def downgrade() -> None:
    op.drop_table("dividend_coverage")
    op.drop_index("ix_dividend_event_listing_ex_date", table_name="dividend_event")
    op.drop_table("dividend_event")

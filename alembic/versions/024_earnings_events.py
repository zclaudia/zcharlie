"""Earnings events from Eastmoney via AkShare (docs/product/mrd-intake-plan.md C1)

* ``earnings_forecast`` - one 业绩预告 per (listing, report period, notice
  date): normalized ``forecast_type``, ``indicator``, net-profit range
  ``profit_low`` / ``profit_high`` (CNY), year-on-year range ``yoy_low`` /
  ``yoy_high`` (fractions), ``content``, ``provider``, ``captured_at``,
  ``raw_ref``.
* ``disclosure_schedule`` - 预约披露时间 per (listing, report period) as of
  ``captured_at`` (append-only): ``first_date``, ``scheduled_date``,
  ``actual_date``.
* ``business_segment`` - 主营构成 rows per (listing, report period, kind,
  name, captured_at): ``revenue`` / ``profit`` (CNY), ``margin`` / ``share``
  (fractions).

Revision ID: 024
Revises: 023
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "024"
down_revision: str | Sequence[str] | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "earnings_forecast",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("notice_date", sa.Date(), nullable=False),
        sa.Column("forecast_type", sa.String(length=16), nullable=False),
        sa.Column("indicator", sa.String(length=64), nullable=False),
        sa.Column("profit_low", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("profit_high", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("yoy_low", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("yoy_high", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_ref", sa.String(length=1024), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "listing_id", "report_period", "notice_date", name="uq_earnings_forecast_notice"
        ),
    )
    op.create_index(
        "ix_earnings_forecast_period", "earnings_forecast", ["report_period", "notice_date"]
    )
    op.create_table(
        "disclosure_schedule",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("first_date", sa.Date(), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("actual_date", sa.Date(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_ref", sa.String(length=1024), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "listing_id", "report_period", "captured_at", name="uq_disclosure_schedule_capture"
        ),
    )
    op.create_index(
        "ix_disclosure_schedule_period",
        "disclosure_schedule",
        ["report_period", "scheduled_date"],
    )
    op.create_table(
        "business_segment",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("revenue", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("profit", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("margin", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("share", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_ref", sa.String(length=1024), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "listing_id",
            "report_period",
            "kind",
            "name",
            "captured_at",
            name="uq_business_segment_capture",
        ),
    )
    op.create_index(
        "ix_business_segment_listing_period", "business_segment", ["listing_id", "report_period"]
    )


def downgrade() -> None:
    op.drop_index("ix_business_segment_listing_period", table_name="business_segment")
    op.drop_table("business_segment")
    op.drop_index("ix_disclosure_schedule_period", table_name="disclosure_schedule")
    op.drop_table("disclosure_schedule")
    op.drop_index("ix_earnings_forecast_period", table_name="earnings_forecast")
    op.drop_table("earnings_forecast")

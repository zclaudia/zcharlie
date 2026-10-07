"""dividend coverage check history (point-in-time dividend freshness)

* ``dividend_coverage_check`` - append-only, one row per coverage check.
  ``dividend_coverage.last_checked_at`` moves with every re-collection, so
  the dividend factor read for a past ``as_of`` saw later checks; readers now
  take the latest check known by the end of ``as_of`` from this history.

Existing coverage rows are seeded with their first and (when different)
latest check, the only checks that were recorded.

Revision ID: 020
Revises: 019
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "020"
down_revision: str | Sequence[str] | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dividend_coverage_check",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("is_backfill", sa.Boolean(), nullable=False),
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
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dividend_coverage_check_lookup",
        "dividend_coverage_check",
        ["listing_id", "provider", "year", "checked_at"],
    )
    op.execute(
        "INSERT INTO dividend_coverage_check "
        "(listing_id, provider, year, checked_at, event_count, is_backfill) "
        "SELECT listing_id, provider, year, first_checked_at, event_count, is_backfill "
        "FROM dividend_coverage"
    )
    op.execute(
        "INSERT INTO dividend_coverage_check "
        "(listing_id, provider, year, checked_at, event_count, is_backfill) "
        "SELECT listing_id, provider, year, last_checked_at, event_count, is_backfill "
        "FROM dividend_coverage WHERE last_checked_at != first_checked_at"
    )


def downgrade() -> None:
    op.drop_index("ix_dividend_coverage_check_lookup", table_name="dividend_coverage_check")
    op.drop_table("dividend_coverage_check")

"""Generic upstream call ledger (docs/product/mrd-intake-plan.md A2)

* ``provider_call`` - one row per logical call of a free data provider
  (baostock / akshare / ...): ``at``, ``provider``, ``endpoint``, ``outcome``
  (``ok`` / ``transient`` / ``terminal`` / ``skipped_open``), ``duration_ms``,
  ``ticker_count``, ``run_id``; index ``(provider, at)`` for the 24-hour
  health-page summary and the 90-day prune.

The iFinD budget ledger (``ifind_call_ledger``, migration 019) is unchanged.

Revision ID: 022
Revises: 021
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: str | Sequence[str] | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_call",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ticker_count", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_call_provider_at", "provider_call", ["provider", "at"])


def downgrade() -> None:
    op.drop_index("ix_provider_call_provider_at", table_name="provider_call")
    op.drop_table("provider_call")

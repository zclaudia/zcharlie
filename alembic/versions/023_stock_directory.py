"""Local stock directory for name / pinyin search (docs/product/mrd-intake-plan.md D4)

* ``stock_directory`` - one row per A-share listing from baostock
  ``query_all_stock``: ``ticker`` (PK, ``600519.SH``), ``name``,
  ``pinyin_initials`` (indexed, prefix search), ``status``
  (``listed`` / ``suspended`` / ``delisted``), ``synced_on``, ``provider``,
  ``captured_at``. Refreshed in full after every successful ``universe_sync``;
  rows are never deleted (vanished tickers become ``delisted``).

Revision ID: 023
Revises: 022
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: str | Sequence[str] | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_directory",
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("pinyin_initials", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("synced_on", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ticker"),
    )
    op.create_index("ix_stock_directory_pinyin", "stock_directory", ["pinyin_initials"])


def downgrade() -> None:
    op.drop_index("ix_stock_directory_pinyin", table_name="stock_directory")
    op.drop_table("stock_directory")

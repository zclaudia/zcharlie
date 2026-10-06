"""market_snapshot.is_backfill (P1-A4)

Daily bars written by the historical backfill are captured years after their
trade date. ``is_backfill`` lets point-in-time readers treat them like
``financial_snapshot.is_backfill``: visible to historical queries by trade
date (prices are never restated) unless the reader asks for the strict
``captured_at`` rule. Existing rows are live captures -> ``false``.

Revision ID: 015
Revises: 014
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str | Sequence[str] | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("market_snapshot") as batch:
        batch.add_column(
            sa.Column(
                "is_backfill", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("market_snapshot") as batch:
        batch.drop_column("is_backfill")

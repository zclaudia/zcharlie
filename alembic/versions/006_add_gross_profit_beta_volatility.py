"""add gross_profit beta volatility_30d

Revision ID: 006
Revises: 005
Create Date: 2026-06-08

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '006'
down_revision: str | Sequence[str] | None = '005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("financial_report") as batch:
        batch.add_column(sa.Column("gross_profit", sa.Numeric(20, 2), nullable=True))
    with op.batch_alter_table("fundamental_snapshot") as batch:
        batch.add_column(sa.Column("beta", sa.Numeric(8, 4), nullable=True))
        batch.add_column(sa.Column("volatility_30d", sa.Numeric(8, 6), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("financial_report") as batch:
        batch.drop_column("gross_profit")
    with op.batch_alter_table("fundamental_snapshot") as batch:
        batch.drop_column("beta")
        batch.drop_column("volatility_30d")

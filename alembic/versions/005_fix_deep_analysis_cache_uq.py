"""fix deep_analysis_cache uq_deep_cache_key — add strategy_code

Revision ID: 005
Revises: 004
Create Date: 2026-06-08

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '005'
down_revision: Union[str, Sequence[str], None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("deep_analysis_cache") as batch:
        batch.drop_constraint("uq_deep_cache_key", type_="unique")
        batch.create_unique_constraint(
            "uq_deep_cache_key",
            ["listing_id", "latest_financial_report_period", "strategy_code"],
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("deep_analysis_cache") as batch:
        batch.drop_constraint("uq_deep_cache_key", type_="unique")
        batch.create_unique_constraint(
            "uq_deep_cache_key",
            ["listing_id", "latest_financial_report_period"],
        )

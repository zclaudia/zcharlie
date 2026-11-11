"""Consecutive-period memory on zone interval state.

Exit, trim and re-review conditions can require N consecutive evaluated
periods. The count lives on the interval row so a day that does not yet fire
the zone is still remembered, and a re-run of the same date does not add a
period.
"""

import sqlalchemy as sa
from alembic import op

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "zone_interval_state",
        sa.Column("condition_streak", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("zone_interval_state", "condition_streak")

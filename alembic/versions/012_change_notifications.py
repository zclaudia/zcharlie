"""add research change notification state

Revision ID: 012
Revises: 011
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: str | Sequence[str] | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("research_change_set") as batch:
        batch.add_column(sa.Column("notification_path", sa.String(length=1024)))
        batch.add_column(sa.Column("notified_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("research_change_set") as batch:
        batch.drop_column("notified_at")
        batch.drop_column("notification_path")

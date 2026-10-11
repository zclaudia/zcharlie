"""Persist immutable evidence and calculations.

Revision ID: 026
Revises: 025
"""

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("investor_evidence", "investor_calculation"):
        op.create_table(
            table,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("revision", sa.Integer(), primary_key=True),
            sa.Column("owner_id", sa.String(32), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])


def downgrade() -> None:
    for table in ("investor_calculation", "investor_evidence"):
        op.drop_index(f"ix_{table}_owner_id", table_name=table)
        op.drop_table(table)

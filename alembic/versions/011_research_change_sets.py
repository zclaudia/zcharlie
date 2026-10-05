"""add deterministic research change sets

Revision ID: 011
Revises: 010
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: str | Sequence[str] | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_change_set",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("current_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("previous_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("baseline", sa.Boolean(), nullable=False),
        sa.Column("has_meaningful_changes", sa.Boolean(), nullable=False),
        sa.Column("change_count", sa.Integer(), nullable=False),
        sa.Column("meaningful_change_count", sa.Integer(), nullable=False),
        sa.Column("diff_json", sa.JSON(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["current_snapshot_id"],
            ["research_snapshot.id"],
        ),
        sa.ForeignKeyConstraint(
            ["previous_snapshot_id"],
            ["research_snapshot.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "current_snapshot_id",
            name="uq_research_change_current_snapshot",
        ),
    )
    op.create_index(
        "ix_research_change_previous_snapshot",
        "research_change_set",
        ["previous_snapshot_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_change_previous_snapshot",
        table_name="research_change_set",
    )
    op.drop_table("research_change_set")

"""add immutable research snapshots

Revision ID: 007
Revises: 006
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: str | Sequence[str] | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_run_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("company_name", sa.String(length=256), nullable=False),
        sa.Column("industry", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("report_year", sa.Integer(), nullable=False),
        sa.Column("canonical_schema_version", sa.String(length=32), nullable=False),
        sa.Column("scoring_version", sa.String(length=32), nullable=False),
        sa.Column("score_schema_version", sa.String(length=32), nullable=False),
        sa.Column("score_json", sa.JSON(), nullable=False),
        sa.Column("explanation_json", sa.JSON(), nullable=True),
        sa.Column("artifact_manifest", sa.JSON(), nullable=False),
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
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_run_id", name="uq_research_snapshot_run"),
    )
    op.create_index(
        "ix_research_snapshot_pipeline_run_id",
        "research_snapshot",
        ["pipeline_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_research_snapshot_symbol",
        "research_snapshot",
        ["symbol"],
        unique=False,
    )
    op.create_index(
        "ix_research_snapshot_as_of",
        "research_snapshot",
        ["as_of"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_research_snapshot_as_of", table_name="research_snapshot")
    op.drop_index("ix_research_snapshot_symbol", table_name="research_snapshot")
    op.drop_index(
        "ix_research_snapshot_pipeline_run_id",
        table_name="research_snapshot",
    )
    op.drop_table("research_snapshot")

"""add durable watchlist research metadata

Revision ID: 010
Revises: 009
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: str | Sequence[str] | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("watchlist_item") as batch:
        batch.add_column(sa.Column("industry", sa.String(length=128)))
        batch.add_column(
            sa.Column(
                "provider",
                sa.String(length=32),
                server_default="auto",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("report_year", sa.Integer()))
        batch.add_column(
            sa.Column(
                "enabled",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "last_refresh_status",
                sa.String(length=16),
                server_default="never",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("last_refreshed_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("last_refresh_error", sa.Text()))
        batch.add_column(sa.Column("last_research_run_id", sa.Integer()))
        batch.create_foreign_key(
            "fk_watchlist_last_research_run",
            "pipeline_run",
            ["last_research_run_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("watchlist_item") as batch:
        batch.drop_constraint("fk_watchlist_last_research_run", type_="foreignkey")
        batch.drop_column("last_research_run_id")
        batch.drop_column("last_refresh_error")
        batch.drop_column("last_refreshed_at")
        batch.drop_column("last_refresh_status")
        batch.drop_column("enabled")
        batch.drop_column("report_year")
        batch.drop_column("provider")
        batch.drop_column("industry")

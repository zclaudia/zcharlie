"""Multi-perspective review rounds, seat runs and comparisons.

Revision ID: 060
Revises: 059
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "060"
down_revision: str | Sequence[str] | None = "059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_round",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("trigger_ref", sa.String(160), nullable=True),
        sa.Column("panel_id", sa.String(64), nullable=False),
        sa.Column("panel_snapshot", sa.JSON(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("evidence_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("base_pack", sa.JSON(), nullable=True),
        sa.Column("base_pack_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_requests", sa.Integer(), nullable=False),
        sa.Column("settled_usage", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_review_round_listing_created",
        "review_round",
        ["listing_id", "created_at"],
    )
    op.create_index(
        "uq_review_round_trigger",
        "review_round",
        ["listing_id", "trigger", "trigger_ref"],
        unique=True,
        sqlite_where=sa.text("trigger_ref IS NOT NULL"),
        postgresql_where=sa.text("trigger_ref IS NOT NULL"),
    )

    op.create_table(
        "review_seat_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "round_id",
            sa.Integer(),
            sa.ForeignKey("review_round.id"),
            nullable=False,
        ),
        sa.Column("seat_id", sa.String(64), nullable=False),
        sa.Column("lens_id", sa.String(64), nullable=False),
        sa.Column("lens_ref", sa.String(160), nullable=False),
        sa.Column("model_ref", sa.String(300), nullable=False),
        sa.Column("model_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "pipeline_run_id",
            sa.Integer(),
            sa.ForeignKey("pipeline_run.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("opinion_json", sa.JSON(), nullable=True),
        sa.Column("annotations_json", sa.JSON(), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "round_id", "seat_id", name="uq_review_seat_run_round_seat"
        ),
    )
    op.create_index("ix_review_seat_run_round", "review_seat_run", ["round_id"])
    op.create_index("ix_review_seat_run_status", "review_seat_run", ["status"])

    op.create_table(
        "review_comparison",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "round_id",
            sa.Integer(),
            sa.ForeignKey("review_round.id"),
            nullable=False,
        ),
        sa.Column("algorithm_version", sa.String(64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "round_id", "algorithm_version", name="uq_review_comparison_version"
        ),
    )


def downgrade() -> None:
    op.drop_table("review_comparison")
    op.drop_index("ix_review_seat_run_status", table_name="review_seat_run")
    op.drop_index("ix_review_seat_run_round", table_name="review_seat_run")
    op.drop_table("review_seat_run")
    op.drop_index("uq_review_round_trigger", table_name="review_round")
    op.drop_index("ix_review_round_listing_created", table_name="review_round")
    op.drop_table("review_round")

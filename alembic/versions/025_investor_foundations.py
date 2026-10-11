"""Investor mandate and case foundations.

Revision ID: 025
Revises: 024
"""

import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "investor_mandate_head",
        sa.Column("owner_id", sa.String(32), primary_key=True),
        sa.Column("mandate_id", sa.String(36), nullable=False, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "investor_mandate_revision",
        sa.Column("mandate_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "investor_case",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_id", "listing_id", name="uq_investor_case_owner_listing"),
    )
    op.create_table(
        "investor_case_revision",
        sa.Column("case_id", sa.String(36), sa.ForeignKey("investor_case.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "investor_episode_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("investor_case.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "investor_plan_revision",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("investor_case.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "investor_mutation",
        sa.Column("owner_id", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(36), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "investor_mutation",
        "investor_plan_revision",
        "investor_episode_revision",
        "investor_case_revision",
        "investor_case",
        "investor_mandate_revision",
        "investor_mandate_head",
    ):
        op.drop_table(table)

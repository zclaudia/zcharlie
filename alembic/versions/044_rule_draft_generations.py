"""Shared rule-draft generations per (listing, scope).

One current draft request per rule slot across both origins (candidate
lifecycle and watchlist entries): the enqueue claims a generation and freezes
its token plus the shared rule head, and the store/callback guards re-check
them. No backfill: a slot without a row has no claim, which the guards treat
as "legacy request, may still store" (plan 7.2 WP9 候选旧任务兼容).
"""

import sqlalchemy as sa
from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_draft_generation",
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("current_token", sa.String(160), nullable=True),
        sa.Column("current_origin_kind", sa.String(16), nullable=True),
        sa.Column("current_origin_id", sa.Integer(), nullable=True),
        sa.Column("template_ref", sa.String(80), nullable=True),
        sa.Column("rule_head", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rule_draft_generation")

"""Watchlist rule drafts: request identity, item pointers, decision kind.

Backfill (before nothing else depends on it): every existing observation entry
either already has a rule, an open draft, or needs a template - see
``watch-holding-plan.md`` 1.5 存量处理. The backfill never records a request
row: no drafting job was enqueued for these entries, so they are marked for an
explicit user re-draft instead of pretending a request exists.
"""

import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watch_draft_request",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("watchlist_id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), nullable=False),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rule_instance_id", sa.Integer(), sa.ForeignKey("rule_instance.id"), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("superseded_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_watch_draft_request_watchlist", "watch_draft_request", ["watchlist_id"])
    op.create_index(
        "ix_watch_draft_request_listing_status", "watch_draft_request", ["listing_id", "status"]
    )
    op.add_column(
        "watchlist_item",
        sa.Column("draft_state", sa.String(16), nullable=False, server_default="none"),
    )
    op.add_column("watchlist_item", sa.Column("draft_request_id", sa.String(36), nullable=True))
    # Plain integer, no foreign key (like candidate.last_screen_result_id):
    # an audit pointer, and SQLite cannot ALTER in a constraint anyway.
    op.add_column("watchlist_item", sa.Column("draft_rule_instance_id", sa.Integer(), nullable=True))
    # An open draft wins over the active rule (a re-draft the user has not
    # approved yet is the thing to show); the rule store keeps at most one
    # open draft per listing and scope.
    op.execute(
        """
        UPDATE watchlist_item SET
            draft_state = 'ready',
            draft_rule_instance_id = (
                SELECT ri.id FROM rule_instance ri
                WHERE ri.listing_id = watchlist_item.listing_id
                  AND ri.scope = 'candidate' AND ri.status = 'draft'
                ORDER BY ri.version DESC LIMIT 1
            )
        WHERE EXISTS (
            SELECT 1 FROM rule_instance ri
            WHERE ri.listing_id = watchlist_item.listing_id
              AND ri.scope = 'candidate' AND ri.status = 'draft'
        )
        """
    )
    op.execute(
        """
        UPDATE watchlist_item SET draft_state = 'needs_template'
        WHERE draft_state = 'none' AND NOT EXISTS (
            SELECT 1 FROM rule_instance ri
            WHERE ri.listing_id = watchlist_item.listing_id
              AND ri.scope = 'candidate' AND ri.status = 'active'
        )
        """
    )


def downgrade() -> None:
    op.drop_column("watchlist_item", "draft_rule_instance_id")
    op.drop_column("watchlist_item", "draft_request_id")
    op.drop_column("watchlist_item", "draft_state")
    op.drop_index("ix_watch_draft_request_listing_status", table_name="watch_draft_request")
    op.drop_index("ix_watch_draft_request_watchlist", table_name="watch_draft_request")
    op.drop_table("watch_draft_request")

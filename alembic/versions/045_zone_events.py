"""Zone events and per-(listing, scope, zone) interval state.

WP8 (docs/product/watch-holding-plan.md 6.2): daily deterministic zone
detection stores one immutable ``zone_event`` row per
``(listing, scope, zone, rule version, as_of, transition, input digest)`` and
keeps the enter/exit memory of funnel 9 in ``zone_interval_state``. No
backfill: before this migration no zone was ever evaluated, so an empty state
table is the truth (an interval starts with the first detection).
"""

import sqlalchemy as sa
from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "zone_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), nullable=False),
        sa.Column("ticker", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("zone", sa.String(32), nullable=False),
        sa.Column("transition", sa.String(8), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        # No nullable rule reference: an event without the rule version that
        # triggered it is the defect WP8 refuses to record.
        sa.Column(
            "rule_instance_id", sa.Integer(), sa.ForeignKey("rule_instance.id"), nullable=False
        ),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.String(64), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("calibration_status", sa.String(16), nullable=False),
        sa.Column("conditions_met", sa.JSON(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=True),
        sa.Column("valuation_ids", sa.JSON(), nullable=False),
        sa.Column("ledger_revision", sa.Integer(), nullable=True),
        sa.Column("knowledge_cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "listing_id",
            "scope",
            "zone",
            "rule_instance_id",
            "as_of",
            "transition",
            "input_digest",
            name="uq_zone_event_identity",
        ),
    )
    op.create_index(
        "ix_zone_event_listing_scope", "zone_event", ["listing_id", "scope", "as_of"]
    )
    op.create_index("ix_zone_event_scope_as_of", "zone_event", ["scope", "as_of"])
    op.create_index("ix_zone_event_rule", "zone_event", ["rule_instance_id"])
    op.create_table(
        "zone_interval_state",
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("zone", sa.String(32), primary_key=True),
        sa.Column("rule_instance_id", sa.Integer(), nullable=True),
        sa.Column("inside", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("entered_as_of", sa.Date(), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_id", sa.Integer(), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_as_of", sa.Date(), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("zone_interval_state")
    op.drop_index("ix_zone_event_rule", table_name="zone_event")
    op.drop_index("ix_zone_event_scope_as_of", table_name="zone_event")
    op.drop_index("ix_zone_event_listing_scope", table_name="zone_event")
    op.drop_table("zone_event")

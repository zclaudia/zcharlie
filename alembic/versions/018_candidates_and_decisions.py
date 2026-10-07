"""candidates and decision journal (P1-D1 / P1-D2)

* ``candidate`` - screen hits and manual nominations, ``open`` ->
  ``promoted`` / ``dismissed`` / ``expired``; at most one ``open`` row per
  listing (partial unique index);
* ``decision`` - immutable, append-only decision journal.

Revision ID: 018
Revises: 017
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: str | Sequence[str] | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("last_screen_result_id", sa.Integer(), nullable=True),
        sa.Column("draft_state", sa.String(length=16), nullable=False),
        sa.Column("draft_rule_instance_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.ForeignKeyConstraint(["draft_rule_instance_id"], ["rule_instance.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_candidate_open_listing",
        "candidate",
        ["listing_id"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index("ix_candidate_status_opened", "candidate", ["status", "opened_at"])
    op.create_index("ix_candidate_listing", "candidate", ["listing_id", "opened_at"])

    op.create_table(
        "decision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("rule_instance_id", sa.Integer(), nullable=True),
        sa.Column("rule_ref", sa.String(length=128), nullable=True),
        sa.Column("candidate_id", sa.Integer(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.ForeignKeyConstraint(["rule_instance_id"], ["rule_instance.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_decision_listing_at", "decision", ["listing_id", "at"])
    op.create_index("ix_decision_candidate", "decision", ["candidate_id"])
    op.create_index("ix_decision_kind_at", "decision", ["kind", "at"])


def downgrade() -> None:
    op.drop_index("ix_decision_kind_at", table_name="decision")
    op.drop_index("ix_decision_candidate", table_name="decision")
    op.drop_index("ix_decision_listing_at", table_name="decision")
    op.drop_table("decision")
    op.drop_index("ix_candidate_listing", table_name="candidate")
    op.drop_index("ix_candidate_status_opened", table_name="candidate")
    op.drop_index("uq_candidate_open_listing", table_name="candidate")
    op.drop_table("candidate")

"""rule store and screen results (P1-C3 / P1-C4)

* ``rule_template`` - immutable template versions (PK ``id`` + ``version``);
* ``rule_instance`` - versioned per-stock rules, draft -> active -> retired,
  at most one active per (listing, scope);
* ``screen_result`` - weekly screen outcome per run, template version and
  target-pool member.

Revision ID: 016
Revises: 015
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: str | Sequence[str] | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("(CURRENT_TIMESTAMP)"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "rule_template",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("calibration_status", sa.String(length=16), nullable=False),
        sa.Column("source_path", sa.String(length=512), nullable=True),
        _created_at(),
        sa.PrimaryKeyConstraint("id", "version"),
    )

    op.create_table(
        "rule_instance",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("drafted_by", sa.String(length=64), nullable=False),
        sa.Column("draft_rationale", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["rule_template.id", "rule_template.version"],
            name="fk_rule_instance_template",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "scope", "version", name="uq_rule_instance_version"),
    )
    op.create_index(
        "uq_rule_instance_active",
        "rule_instance",
        ["listing_id", "scope"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "screen_result",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("screen_run_id", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("hit", sa.Boolean(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("strength", sa.Float(), nullable=True),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(["screen_run_id"], ["pipeline_run.id"]),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["rule_template.id", "rule_template.version"],
            name="fk_screen_result_template",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "screen_run_id",
            "template_id",
            "template_version",
            "listing_id",
            name="uq_screen_result_member",
        ),
    )
    op.create_index(
        "ix_screen_result_as_of_template", "screen_result", ["as_of", "template_id"]
    )
    op.create_index("ix_screen_result_listing", "screen_result", ["listing_id", "as_of"])


def downgrade() -> None:
    op.drop_index("ix_screen_result_listing", table_name="screen_result")
    op.drop_index("ix_screen_result_as_of_template", table_name="screen_result")
    op.drop_table("screen_result")
    op.drop_index("uq_rule_instance_active", table_name="rule_instance")
    op.drop_table("rule_instance")
    op.drop_table("rule_template")

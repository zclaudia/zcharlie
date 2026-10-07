"""iFinD on-demand verification queue and budget ledger (P1-A13)

* ``ifind_call_ledger`` - one row per ifind-proxy call; the verification
  budget counts a rolling 5-hour window of it;
* ``verification_request`` - queued iFinD verifications; at most one open
  (pending / deferred) request per ``dedup_key`` (partial unique index).

Revision ID: 019
Revises: 017 (re-chained onto 018 when the candidates migration lands)
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: str | Sequence[str] | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN = sa.text("status IN ('pending', 'deferred')")


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "ifind_call_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("ticker_count", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("verification_request_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ifind_call_ledger_at", "ifind_call_ledger", ["at"], unique=False)
    op.create_table(
        "verification_request",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("report_period", sa.Date(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("source_run_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_verification_request_open",
        "verification_request",
        ["dedup_key"],
        unique=True,
        sqlite_where=_OPEN,
        postgresql_where=_OPEN,
    )
    op.create_index(
        "ix_verification_request_queue",
        "verification_request",
        ["status", "priority", "requested_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_verification_request_queue", table_name="verification_request")
    op.drop_index("uq_verification_request_open", table_name="verification_request")
    op.drop_table("verification_request")
    op.drop_index("ix_ifind_call_ledger_at", table_name="ifind_call_ledger")
    op.drop_table("ifind_call_ledger")

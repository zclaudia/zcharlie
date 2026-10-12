"""Industry-peer snapshots: two immutable tables plus the research binding column.

* ``peer_cohort_snapshot`` - one immutable roster version of a named cohort
  (``cohort_key`` + fixed ``revision``), payload never updated.
* ``peer_annual_snapshot`` - one immutable version of one company's annual
  facts per ``(company_id, report_period, provider, adapter_version,
  semantic_hash)``; field values and verification proofs live in JSON.
* ``research_snapshot.industry_peer_context`` - nullable JSON binding saved
  verbatim at snapshot write time; old rows stay null and are never backfilled.

Immutability on SQLite mirrors migration 047: UPDATE/DELETE triggers plus an
insert guard that also blocks INSERT OR REPLACE (which bypasses DELETE
triggers unless recursive_triggers is enabled). Revision 048 follows 047.

Revision ID: 048
Revises: 047
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "048"
down_revision: str | Sequence[str] | None = "047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_INSERT_KEYS = {
    "peer_cohort_snapshot": ("cohort_key", "revision"),
    "peer_annual_snapshot": (
        "company_id", "report_period", "provider", "adapter_version", "semantic_hash",
    ),
}
TABLES = tuple(IMMUTABLE_INSERT_KEYS)


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _create_immutability_triggers() -> None:
    # Same statements the model module installs for create_all-based setups;
    # alembic-built tables need them created here.
    for table in TABLES:
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_{operation.lower()} "
                f"BEFORE {operation} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'saved peer snapshots are immutable'); END"
            )
        key_match = " AND ".join(f"{column} IS NEW.{column}" for column in IMMUTABLE_INSERT_KEYS[table])
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_insert "
            f"BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE id = NEW.id OR ({key_match})) "
            "BEGIN SELECT RAISE(ABORT, 'saved peer snapshots are immutable'); END"
        )


def _drop_immutability_triggers() -> None:
    for table in TABLES:
        for operation in ("update", "delete", "insert"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_{operation}")


def upgrade() -> None:
    op.create_table(
        "peer_cohort_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cohort_key", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("observed_on", sa.Date(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification", sa.String(16), nullable=False),
        sa.Column("inclusion_rule_version", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("cohort_key", "revision", name="uq_peer_cohort_snapshot_revision"),
    )
    op.create_index(
        "ix_peer_cohort_snapshot_lookup",
        "peer_cohort_snapshot",
        ["cohort_key", "revision"],
    )
    op.create_table(
        "peer_annual_snapshot",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("company.id"), nullable=False),
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("adapter_version", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("semantic_hash", sa.String(64), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("verification", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "company_id",
            "report_period",
            "provider",
            "adapter_version",
            "semantic_hash",
            name="uq_peer_annual_snapshot_version",
        ),
    )
    op.create_index(
        "ix_peer_annual_snapshot_lookup",
        "peer_annual_snapshot",
        ["company_id", "report_period"],
    )
    op.create_index(
        "ix_peer_annual_snapshot_captured",
        "peer_annual_snapshot",
        ["company_id", "captured_at"],
    )
    _create_immutability_triggers()
    op.add_column("research_snapshot", sa.Column("industry_peer_context", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_snapshot", "industry_peer_context")
    _drop_immutability_triggers()
    op.drop_index("ix_peer_annual_snapshot_captured", table_name="peer_annual_snapshot")
    op.drop_index("ix_peer_annual_snapshot_lookup", table_name="peer_annual_snapshot")
    op.drop_table("peer_annual_snapshot")
    op.drop_index("ix_peer_cohort_snapshot_lookup", table_name="peer_cohort_snapshot")
    op.drop_table("peer_cohort_snapshot")

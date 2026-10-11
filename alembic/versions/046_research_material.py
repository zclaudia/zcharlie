"""Immutable research material: source bindings and versioned datasets.

* ``research_source_binding`` - one source's symbol (or account id) for a
  controlled company/listing over a validity interval; ``verified_at`` /
  ``verified_by`` record the controlled-list confirmation.
* ``research_dataset`` - one immutable version of a source's material for a
  subject and period: ``content_hash`` identifies the version, ``revision`` /
  ``previous_id`` chain supersessions. ``subject_id`` is ``company.id`` or
  ``listing.id`` per ``subject_kind`` (no polymorphic foreign key);
  ``period_end`` is null for company-level ``filing_index`` rows.

Revision ID: 046
Revises: 045
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "046"
down_revision: str | Sequence[str] | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "research_source_binding",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_symbol", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_by", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_kind",
            "subject_id",
            "source",
            "source_symbol",
            "valid_from",
            name="uq_research_source_binding_identity",
        ),
    )
    op.create_table(
        "research_dataset",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("adapter_version", sa.String(length=32), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("period_basis", sa.String(length=32), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("previous_id", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_ref", sa.String(length=512), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "kind",
            "subject_kind",
            "subject_id",
            "source",
            "period_end",
            "content_hash",
            name="uq_research_dataset_version",
        ),
    )
    op.create_index(
        "ix_research_dataset_lookup",
        "research_dataset",
        ["kind", "subject_kind", "subject_id", "period_end"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_dataset_lookup", table_name="research_dataset")
    op.drop_table("research_dataset")
    op.drop_table("research_source_binding")

"""Store immutable research-material acquisition observations.

Revision ID: 049
Revises: 048
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "049"
down_revision: str | Sequence[str] | None = "048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "research_material_acquisition"


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        raise NotImplementedError("material acquisition immutability requires SQLite")
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("adapter_version", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("identity_snapshot", sa.JSON(), nullable=False),
        sa.Column("periods", sa.JSON(), nullable=False),
        sa.Column("expected_requests", sa.JSON(), nullable=False),
        sa.Column("problems", sa.JSON(), nullable=False),
        sa.Column("archive_ref", sa.String(length=512), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("dataset_links", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manifest_sha256", name="uq_material_acquisition_manifest"),
    )
    op.create_index("ix_material_acquisition_subject_source", TABLE, ["source", "captured_at"])
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_immutable_{operation.lower()} "
            f"BEFORE {operation} ON {TABLE} "
            "BEGIN SELECT RAISE(ABORT, 'material acquisition observations are immutable'); END"
        )
    op.execute(
        f"CREATE TRIGGER IF NOT EXISTS trg_{TABLE}_immutable_insert "
        f"BEFORE INSERT ON {TABLE} "
        f"WHEN EXISTS (SELECT 1 FROM {TABLE} WHERE id = NEW.id "
        "OR manifest_sha256 IS NEW.manifest_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'material acquisition observations are immutable'); END"
    )


def downgrade() -> None:
    for operation in ("update", "delete", "insert"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_{operation}")
    op.drop_index("ix_material_acquisition_subject_source", table_name=TABLE)
    op.drop_table(TABLE)

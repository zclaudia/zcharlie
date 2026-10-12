"""Atomic company publication and append-only validity."""
import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("investor_publication_artifact",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("investor_case.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False))
    for field in ("owner_id", "case_id"):
        op.create_index(f"ix_investor_publication_artifact_{field}", "investor_publication_artifact", [field])
    op.create_table("investor_publication_event",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("recommendation_id", sa.String(36), nullable=False),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False), sqlite_autoincrement=True)
    op.create_index("ix_investor_publication_event_recommendation_id", "investor_publication_event", ["recommendation_id"])
    op.create_table("investor_publication_dependency",
        sa.Column("recommendation_id", sa.String(36), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False))
    op.create_table("investor_publication_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("notification_key", sa.String(160), nullable=False, unique=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False))
    # Outbox dispatch polls pending rows by state; keep it off the full scan.
    op.create_index("ix_investor_publication_outbox_state", "investor_publication_outbox", ["state"])


def downgrade() -> None:
    for table in ("outbox", "dependency", "event", "artifact"):
        op.drop_table(f"investor_publication_{table}")

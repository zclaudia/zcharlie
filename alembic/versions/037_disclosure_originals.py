"""Immutable bounded official disclosure originals and acquisition receipts."""

import sqlalchemy as sa
from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "disclosure_blob",
        sa.Column("content_hash", sa.String(64), primary_key=True),
        sa.Column("body", sa.LargeBinary(), nullable=False),
    )
    op.create_table(
        "disclosure_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_id", "identity_hash"),
    )
    op.create_index("ix_disclosure_item_owner_id", "disclosure_item", ["owner_id"])
    op.create_index("ix_disclosure_item_listing_id", "disclosure_item", ["listing_id"])
    op.create_table(
        "disclosure_original",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("item_id", sa.String(36), sa.ForeignKey("disclosure_item.id"), nullable=False),
        sa.Column(
            "content_hash",
            sa.String(64),
            sa.ForeignKey("disclosure_blob.content_hash"),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_id", "item_id", "content_hash"),
    )
    op.create_index("ix_disclosure_original_owner_id", "disclosure_original", ["owner_id"])
    op.create_index("ix_disclosure_original_item_id", "disclosure_original", ["item_id"])
    op.create_table(
        "disclosure_receipt",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_id", "operation_id"),
    )


def downgrade() -> None:
    op.drop_table("disclosure_receipt")
    op.drop_table("disclosure_original")
    op.drop_table("disclosure_item")
    op.drop_table("disclosure_blob")

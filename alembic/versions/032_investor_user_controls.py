"""User decisions, controls, proposals and revocable memory."""
import sqlalchemy as sa
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("investor_user_object",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False))
    for field in ("owner_id", "kind"):
        op.create_index(f"ix_investor_user_object_{field}", "investor_user_object", [field])
    op.create_table("investor_user_object_revision",
        sa.Column("id", sa.String(36), sa.ForeignKey("investor_user_object.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False))
    op.create_table("investor_memory_state",
        sa.Column("owner_id", sa.String(32), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False))


def downgrade() -> None:
    for table in ("memory_state", "user_object_revision", "user_object"):
        op.drop_table(f"investor_{table}")

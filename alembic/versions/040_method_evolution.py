"""Scoped, explicitly approved method versions and immutable task bindings."""
import sqlalchemy as sa
from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("investor_method_artifact", sa.Column("id", sa.String(36), primary_key=True), sa.Column("owner_id", sa.String(32), nullable=False), sa.Column("kind", sa.String(24), nullable=False), sa.Column("payload", sa.JSON(), nullable=False))
    op.create_table("investor_method_proposal", sa.Column("id", sa.String(36), primary_key=True), sa.Column("owner_id", sa.String(32), nullable=False), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("payload", sa.JSON(), nullable=False))
    op.create_table("investor_method_head", sa.Column("owner_id", sa.String(32), primary_key=True), sa.Column("listing_id", sa.Integer(), sa.ForeignKey("listing.id"), primary_key=True), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("payload", sa.JSON(), nullable=False))
    op.create_table("agent_task_method_binding", sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_task.id"), primary_key=True), sa.Column("payload", sa.JSON(), nullable=False))


def downgrade() -> None:
    for table in ("agent_task_method_binding", "investor_method_head", "investor_method_proposal", "investor_method_artifact"):
        op.drop_table(table)

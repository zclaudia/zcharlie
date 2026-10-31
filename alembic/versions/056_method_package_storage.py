"""Method package storage: global default heads and committed tool action order."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "056"
down_revision: str | Sequence[str] | None = "055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investor_method_default_head",
        sa.Column("owner_id", sa.String(32), primary_key=True),
        sa.Column("channel", sa.String(32), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.add_column("agent_tool_action", sa.Column("committed_sequence", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_tool_action") as batch:
        batch.drop_column("committed_sequence")
    op.drop_table("investor_method_default_head")

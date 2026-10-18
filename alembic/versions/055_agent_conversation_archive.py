"""Add agent_conversation.archived_at for archive / 30-day purge."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "055"
down_revision: str | Sequence[str] | None = "054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_conversation", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_agent_conversation_archived_at", "agent_conversation", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_conversation_archived_at", table_name="agent_conversation")
    with op.batch_alter_table("agent_conversation") as batch:
        batch.drop_column("archived_at")

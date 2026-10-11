"""Conversation receipts and monotonically retained public events."""

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_task", sa.Column("event_floor", sa.Integer(), nullable=False, server_default="0"))
    # SQLite needs the explicit table option; ordinary INTEGER PRIMARY KEY can
    # reuse an ID after deleting the maximum row or the whole table.
    with op.batch_alter_table("agent_task_event", recreate="always", table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.add_column(sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    for row in bind.execute(sa.text("SELECT event_id, payload FROM agent_task_event")):
        import json

        payload = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
        at = datetime.fromisoformat(payload["occurred_at"])
        bind.execute(sa.text("UPDATE agent_task_event SET occurred_at=:at WHERE event_id=:id"), {"at": at.isoformat(), "id": row.event_id})
    # A second batch recreation retains the copied maximum and sequence.
    with op.batch_alter_table("agent_task_event", recreate="always", table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.alter_column("occurred_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        batch.create_index("ix_agent_task_event_occurred_at", ["occurred_at"])
    op.create_table("agent_conversation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(32), nullable=False),
        sa.Column("next_sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False))
    op.create_index("ix_agent_conversation_owner_id", "agent_conversation", ["owner_id"])
    op.create_table("agent_message",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("agent_conversation.id"), nullable=False),
        sa.Column("client_message_id", sa.String(36)),
        sa.Column("completion_key", sa.String(36)),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("conversation_id", "client_message_id"),
        sa.UniqueConstraint("conversation_id", "sequence"),
        sa.UniqueConstraint("completion_key"))
    op.create_index("ix_agent_message_conversation_id", "agent_message", ["conversation_id"])


def downgrade() -> None:
    op.drop_table("agent_message")
    op.drop_table("agent_conversation")
    with op.batch_alter_table("agent_task_event", recreate="always", table_kwargs={"sqlite_autoincrement": False}) as batch:
        batch.drop_index("ix_agent_task_event_occurred_at")
        batch.drop_column("occurred_at")
    with op.batch_alter_table("agent_task") as batch:
        batch.drop_column("event_floor")

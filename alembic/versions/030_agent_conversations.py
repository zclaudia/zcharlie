"""Conversation receipts and monotonically retained public events."""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def _payload_time(value: object) -> datetime:
    """Parse a contract timestamp; naive values are invalid, never assumed UTC."""
    if not isinstance(value, str):
        raise ValueError("payload timestamps must be ISO strings")
    at = datetime.fromisoformat(value)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("payload timestamps must include a timezone")
    return at


def _storage_string(at: datetime) -> str:
    # Same six-digit microsecond UTC format the ORM DateTime column writes on
    # SQLite; comparisons and keyset pagination rely on this exact shape.
    return at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _has_column(bind, table: str, column: str) -> bool:
    return any(row[1] == column for row in bind.execute(sa.text(f"PRAGMA table_info({table})")))


def upgrade() -> None:
    bind = op.get_bind()
    # Validate and compute every backfill value BEFORE any DDL: the pysqlite
    # driver autocommits DDL, so a late failure could not roll the columns back
    # and a plain re-run would die on the first duplicate column. An invalid
    # payload time fails the whole migration instead of being stored raw.
    backfill: list[tuple[int, str]] = []
    for row in bind.execute(sa.text("SELECT event_id, payload FROM agent_task_event")).all():
        payload = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
        backfill.append((row.event_id, _storage_string(_payload_time(payload["occurred_at"]))))
    # An aborted earlier attempt may already have committed some DDL; these
    # guards keep such a re-run going instead of failing on a duplicate column.
    if not _has_column(bind, "agent_task", "event_floor"):
        op.add_column("agent_task", sa.Column("event_floor", sa.Integer(), nullable=False, server_default="0"))
    if not _has_column(bind, "agent_task_event", "occurred_at"):
        # SQLite needs the explicit table option; ordinary INTEGER PRIMARY KEY can
        # reuse an ID after deleting the maximum row or the whole table.
        with op.batch_alter_table("agent_task_event", recreate="always", table_kwargs={"sqlite_autoincrement": True}) as batch:
            batch.add_column(sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
    for event_id, at in backfill:
        bind.execute(sa.text("UPDATE agent_task_event SET occurred_at=:at WHERE event_id=:id"), {"at": at, "id": event_id})
    # A second batch recreation retains the copied maximum and sequence. It has
    # no guard of its own, but every input above is validated up front, so the
    # only way to get here with a broken schema is a crash inside this step.
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

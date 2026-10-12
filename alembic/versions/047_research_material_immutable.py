"""Protect saved research material from direct SQL updates/deletes.

Revision ID: 047
Revises: 046
"""

from collections.abc import Sequence

from alembic import op

revision: str = "047"
down_revision: str | Sequence[str] | None = "046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFLICT_KEYS = {
    "research_source_binding": (
        "subject_kind", "subject_id", "source", "source_symbol", "valid_from",
    ),
    "research_dataset": (
        "kind", "subject_kind", "subject_id", "source", "period_end", "content_hash",
    ),
}
TABLES = tuple(CONFLICT_KEYS)


def upgrade() -> None:
    # SQLite is the application's supported database. Do not silently claim
    # immutability on a different database without an equivalent implementation.
    if op.get_bind().dialect.name != "sqlite":
        raise NotImplementedError("research material immutability requires SQLite")
    for table in TABLES:
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_{operation.lower()} "
                f"BEFORE {operation} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'saved research material is immutable'); END"
            )

        key_match = " AND ".join(f"{column} IS NEW.{column}" for column in CONFLICT_KEYS[table])
        op.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_insert "
            f"BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE id = NEW.id OR ({key_match})) "
            "BEGIN SELECT RAISE(ABORT, 'saved research material is immutable'); END"
        )


def downgrade() -> None:
    for table in TABLES:
        for operation in ("update", "delete", "insert"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_{operation}")

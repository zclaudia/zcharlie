"""SQLAlchemy declarative base + timestamp mixin."""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Project-wide declarative base.

    Foreign keys across the schema are declarative only: SQLite runs without
    ``PRAGMA foreign_keys``, so nothing enforces them at the storage layer.
    Deleting a parent row silently orphanizes children; the application layer
    owns the deletion order (see the note on migration 025).
    """


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

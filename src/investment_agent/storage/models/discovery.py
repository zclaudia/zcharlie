"""Durable bounded discovery batches and per-listing source cursors."""
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class DiscoveryCursor(Base):
    __tablename__ = "investor_discovery_cursor"
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class DiscoveryBatch(Base):
    __tablename__ = "investor_discovery_batch"
    sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[str] = mapped_column(String(36), unique=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_task.id"), nullable=True, unique=True)
    state: Mapped[str] = mapped_column(String(24), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

"""Durable monitor receipts, review cycles and in-app notification projection."""
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class MonitorReceipt(Base):
    __tablename__ = "investor_monitor_receipt"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investor_case.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ReviewCycle(Base):
    __tablename__ = "investor_review_cycle"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investor_case.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_task.id"), nullable=True, unique=True)
    state: Mapped[str] = mapped_column(String(24), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class InvestorNotification(Base):
    __tablename__ = "investor_notification"
    sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    __table_args__ = {"sqlite_autoincrement": True}
    key: Mapped[str] = mapped_column(String(160), unique=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investor_case.id"))
    revision: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class OperationsCursor(Base):
    __tablename__ = "agent_operations_cursor"
    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class NotificationDelivery(Base):
    """Frozen safe publication projection, independent of paid analysis."""
    __tablename__ = "investor_notification_delivery"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    notification_key: Mapped[str] = mapped_column(String(160), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investor_case.id"))
    state: Mapped[str] = mapped_column(String(24), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

"""One mutable CAS head; immutable plan bodies and append-only state events."""
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class PortfolioPlanHead(Base):
    __tablename__ = "portfolio_plan_head"
    account_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    checked_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class PortfolioPlanArtifact(Base):
    __tablename__ = "portfolio_plan_artifact"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PortfolioPlanEvent(Base):
    __tablename__ = "portfolio_plan_event"
    __table_args__ = (UniqueConstraint("plan_id", "event_key"), {"sqlite_autoincrement": True})
    sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("portfolio_plan_artifact.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(96))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PortfolioPlanOutbox(Base):
    __tablename__ = "portfolio_plan_outbox"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("portfolio_plan_artifact.id"))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


def _immutable(*args: object) -> None:
    raise ValueError("portfolio plan bodies and events are immutable")


for _model in (PortfolioPlanArtifact, PortfolioPlanEvent):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)


class PortfolioPurposeHead(Base):
    __tablename__ = "portfolio_purpose_head"
    account_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(nullable=False)
    version_id: Mapped[str] = mapped_column(String(36), nullable=False)


class PortfolioPurposeVersion(Base):
    __tablename__ = "portfolio_purpose_version"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


event.listen(PortfolioPurposeVersion, "before_update", _immutable)
event.listen(PortfolioPurposeVersion, "before_delete", _immutable)

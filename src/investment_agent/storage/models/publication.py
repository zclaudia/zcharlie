"""Immutable publication artifacts and append-only validity events."""
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class PublicationArtifact(Base):
    __tablename__ = "investor_publication_artifact"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investor_case.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PublicationEvent(Base):
    __tablename__ = "investor_publication_event"
    __table_args__ = {"sqlite_autoincrement": True}
    sequence: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    recommendation_id: Mapped[str] = mapped_column(String(36), index=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PublicationDependency(Base):
    __tablename__ = "investor_publication_dependency"
    recommendation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ordinal: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PublicationOutbox(Base):
    __tablename__ = "investor_publication_outbox"
    __table_args__ = (UniqueConstraint("notification_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notification_key: Mapped[str] = mapped_column(String(160))
    owner_id: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="pending")


def _immutable(*args: object) -> None:
    raise ValueError("publication artifacts and events are immutable")


for _model in (PublicationArtifact, PublicationEvent, PublicationDependency):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)

"""Immutable owner-scoped evidence and calculation payloads."""

from typing import Any

from sqlalchemy import JSON, String, event
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class SavedEvidence(Base):
    __tablename__ = "investor_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class SavedCalculation(Base):
    __tablename__ = "investor_calculation"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


def _immutable(*args: object) -> None:
    raise ValueError("saved evidence and calculations are immutable")


for _model in (SavedEvidence, SavedCalculation):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)

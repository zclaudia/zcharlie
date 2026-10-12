"""Investor foundations; immutable revision payloads and atomic current pointers."""

from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class MandateHead(Base):
    __tablename__ = "investor_mandate_head"
    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mandate_id: Mapped[str] = mapped_column(String(36), unique=True)
    revision: Mapped[int]


class MandateRevision(Base):
    __tablename__ = "investor_mandate_revision"
    mandate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class InvestorCase(Base):
    __tablename__ = "investor_case"
    __table_args__ = (
        UniqueConstraint("owner_id", "listing_id", name="uq_investor_case_owner_listing"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"))
    revision: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON)


class CaseRevision(Base):
    __tablename__ = "investor_case_revision"
    case_id: Mapped[str] = mapped_column(ForeignKey("investor_case.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class EpisodeRevision(Base):
    __tablename__ = "investor_episode_revision"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investor_case.id"), index=True)
    number: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PlanRevision(Base):
    __tablename__ = "investor_plan_revision"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investor_case.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class InvestorMutation(Base):
    __tablename__ = "investor_mutation"
    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)


def _immutable(*args: object) -> None:
    raise ValueError("investor revisions and mutation receipts are immutable")


for _model in (MandateRevision, CaseRevision, EpisodeRevision, PlanRevision, InvestorMutation):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)


class UserObject(Base):
    """Mutable head; immutable payload versions are retained separately."""
    __tablename__ = "investor_user_object"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    revision: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class UserObjectRevision(Base):
    __tablename__ = "investor_user_object_revision"
    id: Mapped[str] = mapped_column(ForeignKey("investor_user_object.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class MemoryState(Base):
    """Owner-wide personalization fence. Revision zero means no saved memories."""
    __tablename__ = "investor_memory_state"
    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int]


event.listen(UserObjectRevision, "before_update", _immutable)
event.listen(UserObjectRevision, "before_delete", _immutable)


class MethodArtifact(Base):
    """Append-only process reviews, evaluations, shadows and method versions."""
    __tablename__ = "investor_method_artifact"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class MethodProposal(Base):
    __tablename__ = "investor_method_proposal"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class MethodHead(Base):
    __tablename__ = "investor_method_head"
    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), primary_key=True)
    revision: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class TaskMethodBinding(Base):
    __tablename__ = "agent_task_method_binding"
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


for _method_model in (MethodArtifact, TaskMethodBinding):
    event.listen(_method_model, "before_update", _immutable)
    event.listen(_method_model, "before_delete", _immutable)

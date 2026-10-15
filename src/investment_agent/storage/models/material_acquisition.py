"""Immutable observation rows linking source captures to saved datasets."""

from datetime import datetime
from typing import Any

from sqlalchemy import DDL, JSON, DateTime, Index, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class ImmutableMaterialAcquisitionError(ValueError, RuntimeError):
    """Raised when an acquisition observation is changed or removed."""


class ResearchMaterialAcquisition(Base, TimestampMixin):
    """One immutable record of an adapter's observed source evidence."""

    __tablename__ = "research_material_acquisition"
    __table_args__ = (
        UniqueConstraint("manifest_sha256", name="uq_material_acquisition_manifest"),
        Index("ix_material_acquisition_subject_source", "source", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    identity_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    periods: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    expected_requests: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    problems: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    archive_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_links: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)


def _immutable(*args: object) -> None:
    raise ImmutableMaterialAcquisitionError("material acquisition observations are immutable")


event.listen(ResearchMaterialAcquisition, "before_update", _immutable)
event.listen(ResearchMaterialAcquisition, "before_delete", _immutable)

for _operation in ("UPDATE", "DELETE"):
    event.listen(
        ResearchMaterialAcquisition.__table__,
        "after_create",
        DDL(  # type: ignore[no-untyped-call]
            "CREATE TRIGGER IF NOT EXISTS trg_research_material_acquisition_immutable_"
            f"{_operation.lower()} BEFORE {_operation} ON research_material_acquisition "
            "BEGIN SELECT RAISE(ABORT, 'material acquisition observations are immutable'); END"
        ).execute_if(dialect="sqlite"),
    )

event.listen(
    ResearchMaterialAcquisition.__table__,
    "after_create",
    DDL(  # type: ignore[no-untyped-call]
        "CREATE TRIGGER IF NOT EXISTS trg_research_material_acquisition_immutable_insert "
        "BEFORE INSERT ON research_material_acquisition "
        "WHEN EXISTS (SELECT 1 FROM research_material_acquisition "
        "WHERE id = NEW.id OR manifest_sha256 IS NEW.manifest_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'material acquisition observations are immutable'); END"
    ).execute_if(dialect="sqlite"),
)

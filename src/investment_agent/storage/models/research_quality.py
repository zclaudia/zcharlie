"""QT-005: experiment indexes and append-only history, independently importable.

Foreign keys document relationships; repositories validate them with FK checks
disabled too. Exported SQLite DDL must also be installed by the migration.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base
from investment_agent.storage.models.investor import MethodArtifact


class ResearchExperiment(Base):
    __tablename__ = "research_experiment"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_quality_experiment_revision"),
        CheckConstraint(
            "state IN ('draft','sealed','running','blocked','cancelling','cancelled','awaiting_review','reported')",
            name="ck_quality_experiment_state",
        ),
        CheckConstraint(
            "(spec_artifact_id IS NULL AND spec_hash IS NULL) OR (spec_artifact_id IS NOT NULL AND spec_hash IS NOT NULL AND length(spec_hash) = 64)",
            name="ck_quality_experiment_spec",
        ),
        Index("ix_research_experiment_owner_created", "owner_id", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int]
    state: Mapped[str] = mapped_column(String(24))
    spec_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("investor_method_artifact.id"))
    spec_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchTrial(Base):
    __tablename__ = "research_trial"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "arm", "repeat_index", name="uq_quality_trial_arm_repeat"
        ),
        CheckConstraint("arm IN ('baseline','candidate')", name="ck_quality_trial_arm"),
        CheckConstraint("repeat_index BETWEEN 1 AND 2", name="ck_quality_trial_repeat"),
        CheckConstraint(
            "state IN ('planned','queued','running','completed','failed','cancelled','blocked')",
            name="ck_quality_trial_state",
        ),
        Index("ix_research_trial_experiment_state", "experiment_id", "state"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("research_experiment.id"))
    arm: Mapped[str] = mapped_column(String(16))
    repeat_index: Mapped[int]
    state: Mapped[str] = mapped_column(String(16))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_task.id"), unique=True)
    result_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("investor_method_artifact.id")
    )
    error_code: Mapped[str | None] = mapped_column(String(160))


class ResearchExperimentTask(Base):
    __tablename__ = "research_experiment_task"
    __table_args__ = (
        CheckConstraint(
            "length(envelope_hash) = 64 AND length(method_binding_hash) = 64 AND length(model_config_hash) = 64",
            name="ck_quality_binding_hashes",
        ),
        CheckConstraint("length(profile_version) > 0", name="ck_quality_binding_profile"),
        {"sqlite_with_rowid": False},
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_task.id"), primary_key=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey("research_trial.id"), unique=True)
    envelope_artifact_id: Mapped[str] = mapped_column(ForeignKey("investor_method_artifact.id"))
    envelope_hash: Mapped[str] = mapped_column(String(64))
    method_binding_hash: Mapped[str] = mapped_column(String(64))
    model_config_hash: Mapped[str] = mapped_column(String(64))
    profile_version: Mapped[str] = mapped_column(String(80))


class ResearchAssessment(Base):
    __tablename__ = "research_assessment"
    __table_args__ = (
        UniqueConstraint(
            "trial_id", "reviewer_id", "revision", name="uq_quality_assessment_revision"
        ),
        CheckConstraint("revision >= 1", name="ck_quality_assessment_revision"),
        CheckConstraint("length(payload_hash) = 64", name="ck_quality_assessment_hash"),
        CheckConstraint(
            "json_valid(payload) AND json_type(payload) = 'object'",
            name="ck_quality_assessment_payload",
        ),
        {"sqlite_with_rowid": False},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("research_experiment.id"))
    trial_id: Mapped[str] = mapped_column(ForeignKey("research_trial.id"))
    reviewer_id: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int]
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchExperimentReceipt(Base):
    __tablename__ = "research_experiment_receipt"
    __table_args__ = (
        CheckConstraint("length(request_hash) = 64", name="ck_quality_receipt_hash"),
        CheckConstraint(
            "json_valid(payload) AND json_type(payload) = 'object'",
            name="ck_quality_receipt_payload",
        ),
        {"sqlite_with_rowid": False},
    )
    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mutation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_object_id: Mapped[str | None] = mapped_column(ForeignKey("research_experiment.id"))
    request_hash: Mapped[str] = mapped_column(String(64))
    # Complete closed DTO preserves original status, response and exact result refs.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _immutable(*_args: object) -> None:
    raise ValueError("research quality history is immutable")


_HISTORY_IDENTITIES = {
    ResearchExperimentTask: "task_id = NEW.task_id OR trial_id = NEW.trial_id",
    ResearchAssessment: "id = NEW.id OR (trial_id = NEW.trial_id AND reviewer_id = NEW.reviewer_id AND revision = NEW.revision)",
    ResearchExperimentReceipt: "owner_id = NEW.owner_id AND mutation_id = NEW.mutation_id",
}
QUALITY_IMMUTABILITY_DDL: tuple[str, ...] = tuple(
    statement
    for model, identity in _HISTORY_IDENTITIES.items()
    for statement in (
        f"CREATE TRIGGER IF NOT EXISTS trg_{model.__tablename__}_immutable_update BEFORE UPDATE ON {model.__tablename__} BEGIN SELECT RAISE(ABORT, 'research quality history is immutable'); END",
        f"CREATE TRIGGER IF NOT EXISTS trg_{model.__tablename__}_immutable_delete BEFORE DELETE ON {model.__tablename__} BEGIN SELECT RAISE(ABORT, 'research quality history is immutable'); END",
        f"CREATE TRIGGER IF NOT EXISTS trg_{model.__tablename__}_immutable_insert BEFORE INSERT ON {model.__tablename__} WHEN EXISTS (SELECT 1 FROM {model.__tablename__} WHERE {identity}) BEGIN SELECT RAISE(ABORT, 'research quality history identity exists'); END",
    )
)
QUALITY_ASSESSMENT_DDL = (
    "CREATE TRIGGER IF NOT EXISTS trg_research_assessment_continuous BEFORE INSERT ON research_assessment "
    "WHEN NEW.revision != COALESCE((SELECT MAX(revision) + 1 FROM research_assessment WHERE trial_id = NEW.trial_id AND reviewer_id = NEW.reviewer_id), 1) "
    "OR NOT EXISTS (SELECT 1 FROM research_trial WHERE id = NEW.trial_id AND experiment_id = NEW.experiment_id) "
    "OR NOT EXISTS (SELECT 1 FROM research_experiment WHERE id = NEW.experiment_id) "
    "BEGIN SELECT RAISE(ABORT, 'assessment requires existing trial and consecutive revision'); END",
)
for _model in _HISTORY_IDENTITIES:
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
    for _statement in QUALITY_IMMUTABILITY_DDL:
        if f" ON {_model.__tablename__} " in _statement:
            event.listen(
                _model.__table__,
                "after_create",
                DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
            )
for _statement in QUALITY_ASSESSMENT_DDL:
    event.listen(
        ResearchAssessment.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )

# The original shared artifact table retains its layout and historical kinds.
# SQLite REPLACE can delete a conflicting row without firing DELETE triggers;
# check both the public key and its existing hidden rowid before that happens.
_QUALITY_KINDS_SQL = (
    "('quality_case','quality_envelope','quality_spec','quality_result',"
    "'quality_report','quality_blind_map')"
)
QUALITY_ARTIFACT_DDL: tuple[str, ...] = (
    "CREATE TRIGGER IF NOT EXISTS trg_quality_artifact_immutable_update "
    "BEFORE UPDATE ON investor_method_artifact "
    f"WHEN OLD.kind IN {_QUALITY_KINDS_SQL} OR NEW.kind IN {_QUALITY_KINDS_SQL} "
    "OR EXISTS (SELECT 1 FROM investor_method_artifact "
    "WHERE (id = NEW.id OR rowid = NEW.rowid) "
    f"AND kind IN {_QUALITY_KINDS_SQL}) "
    "BEGIN SELECT RAISE(ABORT, 'quality artifacts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_quality_artifact_immutable_delete "
    "BEFORE DELETE ON investor_method_artifact "
    f"WHEN OLD.kind IN {_QUALITY_KINDS_SQL} "
    "BEGIN SELECT RAISE(ABORT, 'quality artifacts are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS trg_quality_artifact_immutable_insert "
    "BEFORE INSERT ON investor_method_artifact "
    "WHEN EXISTS (SELECT 1 FROM investor_method_artifact "
    "WHERE (id = NEW.id OR rowid = NEW.rowid) "
    f"AND (kind IN {_QUALITY_KINDS_SQL} OR NEW.kind IN {_QUALITY_KINDS_SQL})) "
    "BEGIN SELECT RAISE(ABORT, 'quality artifact identity exists'); END",
)
for _statement in QUALITY_ARTIFACT_DDL:
    event.listen(
        MethodArtifact.__table__,
        "after_create",
        DDL(_statement).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )

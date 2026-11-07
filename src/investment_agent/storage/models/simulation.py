"""Simulation-only persistence; never references confirmed portfolio tables.

Payloads are frozen contract JSON (Decimal strings). Indexed columns support
scope authorization, compare-and-swap and deterministic recovery. The service
owns cross-row scope validation and all account state transitions. Ledger facts
are append-only, including against direct SQLite SQL; corrections append facts.
"""

from datetime import date, datetime
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class _Payload:
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SimulationExperimentRecord(_Payload, Base):
    __tablename__ = "simulation_experiment"
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_sim_experiment_revision"),)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SimulationParticipantRecord(_Payload, Base):
    __tablename__ = "simulation_participant"
    __table_args__ = (
        UniqueConstraint("experiment_id", "account_id", name="uq_sim_participant_account"),
        UniqueConstraint("experiment_id", "name", "version", name="uq_sim_participant_version"),
        CheckConstraint("version > 0", name="ck_sim_participant_version"),
    )
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_experiment.id"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class _ExperimentScope:
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_experiment.id"), nullable=False, index=True
    )


class _AccountScope(_ExperimentScope):
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_participant.id"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_account.id"), nullable=False, index=True
    )


class SimulationAccountRecord(_Payload, _ExperimentScope, Base):
    __tablename__ = "simulation_account"
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_sim_account_revision"),)
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_participant.id"), nullable=False, unique=True
    )
    revision: Mapped[int] = mapped_column(nullable=False)


class SimulationRoundRecord(_Payload, _ExperimentScope, Base):
    __tablename__ = "simulation_round"
    __table_args__ = (
        UniqueConstraint("experiment_id", "observation_date", name="uq_sim_round_observation"),
        CheckConstraint("revision >= 0", name="ck_sim_round_revision"),
        CheckConstraint(
            "cutoff < decision_deadline AND decision_deadline < execution_at "
            "AND observation_date < execution_date",
            name="ck_sim_round_clock",
        ),
    )
    observation_date: Mapped[date] = mapped_column(nullable=False)
    execution_date: Mapped[date] = mapped_column(nullable=False)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(nullable=False)


class SimulationDecisionRecord(_Payload, _AccountScope, Base):
    __tablename__ = "simulation_decision"
    __table_args__ = (
        UniqueConstraint("participant_id", "round_id", "revision", name="uq_sim_decision_revision"),
        UniqueConstraint("account_id", "idempotency_key", name="uq_sim_decision_key"),
        Index(
            "uq_sim_decision_final",
            "participant_id",
            "round_id",
            unique=True,
            sqlite_where=text("status = 'locked'"),
            postgresql_where=text("status = 'locked'"),
        ),
        CheckConstraint("revision >= 0 AND account_revision >= 0", name="ck_sim_decision_revision"),
    )
    round_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_round.id"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(nullable=False)
    account_revision: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)


class SimulationOrderRecord(_Payload, _AccountScope, Base):
    __tablename__ = "simulation_order"
    __table_args__ = (
        UniqueConstraint("decision_id", "sequence", name="uq_sim_order_sequence"),
        UniqueConstraint("account_id", "idempotency_key", name="uq_sim_order_key"),
        UniqueConstraint("account_id", "round_id", "listing_id", name="uq_sim_order_direction"),
        CheckConstraint("sequence > 0 AND account_revision >= 0", name="ck_sim_order_sequence"),
    )
    round_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_round.id"), nullable=False, index=True
    )
    decision_id: Mapped[str] = mapped_column(ForeignKey("simulation_decision.id"), nullable=False)
    listing_id: Mapped[int] = mapped_column(nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    account_revision: Mapped[int] = mapped_column(nullable=False)
    execution_date: Mapped[date] = mapped_column(nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)


class SimulationFillRecord(_Payload, _AccountScope, Base):
    __tablename__ = "simulation_fill"
    __table_args__ = (
        UniqueConstraint("order_id", "execution_key", "sequence", name="uq_sim_fill_execution"),
        UniqueConstraint("account_id", "execution_key", name="uq_sim_fill_key"),
        CheckConstraint("sequence > 0", name="ck_sim_fill_sequence"),
        CheckConstraint("economic_at <= confirmed_at", name="ck_sim_fill_clock"),
    )
    round_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_round.id"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_order.id"), nullable=False, index=True
    )
    execution_key: Mapped[str] = mapped_column(String(160), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    economic_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SimulationLedgerEventRecord(_Payload, _AccountScope, Base):
    __tablename__ = "simulation_ledger_event"
    __table_args__ = (
        UniqueConstraint("account_id", "sequence", name="uq_sim_ledger_sequence"),
        UniqueConstraint("account_id", "account_revision", name="uq_sim_ledger_revision"),
        UniqueConstraint("account_id", "idempotency_key", name="uq_sim_ledger_key"),
        CheckConstraint("sequence > 0 AND account_revision > 0", name="ck_sim_ledger_sequence"),
        CheckConstraint("economic_at <= confirmed_at", name="ck_sim_ledger_clock"),
    )
    sequence: Mapped[int] = mapped_column(nullable=False)
    account_revision: Mapped[int] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    economic_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SimulationNavRecord(_Payload, _AccountScope, Base):
    __tablename__ = "simulation_nav"
    __table_args__ = (
        UniqueConstraint("account_id", "valuation_date", "version", name="uq_sim_nav_version"),
        UniqueConstraint(
            "account_id",
            "valuation_date",
            "account_revision",
            "input_hash",
            "valuation_version",
            "metrics_version",
            name="uq_sim_nav_input",
        ),
        CheckConstraint("version > 0 AND account_revision >= 0", name="ck_sim_nav_revision"),
    )
    account_revision: Mapped[int] = mapped_column(nullable=False)
    valuation_date: Mapped[date] = mapped_column(nullable=False, index=True)
    version: Mapped[int] = mapped_column(nullable=False)
    quality: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    valuation_version: Mapped[str] = mapped_column(String(160), nullable=False)
    metrics_version: Mapped[str] = mapped_column(String(160), nullable=False)


class SimulationReviewRecord(_Payload, _AccountScope, Base):
    __tablename__ = "simulation_review"
    __table_args__ = (
        UniqueConstraint("decision_id", "version", name="uq_sim_review_version"),
        CheckConstraint("version > 0", name="ck_sim_review_version"),
    )
    decision_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_decision.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class SimulationCostRecord(_Payload, _AccountScope, Base):
    __tablename__ = "simulation_cost"
    __table_args__ = (UniqueConstraint("participant_id", "call_key", name="uq_sim_cost_call"),)
    round_id: Mapped[str] = mapped_column(
        ForeignKey("simulation_round.id"), nullable=False, index=True
    )
    call_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)


class SimulationRequestRecord(Base):
    __tablename__ = "simulation_request"
    scope: Mapped[str] = mapped_column(String(200), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_id: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SimulationExecutionEvidenceRecord(_Payload, _ExperimentScope, Base):
    __tablename__ = "simulation_execution_evidence"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "listing_id",
            "trade_date",
            "revision",
            name="uq_sim_execution_evidence_revision",
        ),
        UniqueConstraint("experiment_id", "content_hash", name="uq_sim_execution_evidence_hash"),
        CheckConstraint(
            "revision > 0 AND listing_id > 0", name="ck_sim_execution_evidence_revision"
        ),
    )
    listing_id: Mapped[int] = mapped_column(nullable=False)
    trade_date: Mapped[date] = mapped_column(nullable=False, index=True)
    revision: Mapped[int] = mapped_column(nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


# Native SQLite statements protect facts even when bypassing the ORM. Payload
# immutability on other databases remains a repository/service responsibility.
for _model in (
    SimulationLedgerEventRecord,
    SimulationFillRecord,
    SimulationNavRecord,
    SimulationExecutionEvidenceRecord,
):
    for _operation in ("UPDATE", "DELETE"):
        event.listen(
            _model.__table__,
            "after_create",
            DDL(  # type: ignore[no-untyped-call]  # SQLAlchemy does not annotate DDL.__init__.
                f"CREATE TRIGGER {_model.__tablename__}_no_{_operation.lower()} "
                f"BEFORE {_operation} ON {_model.__tablename__} "
                "BEGIN SELECT RAISE(ABORT, 'simulation facts are append-only'); END"
            ).execute_if(dialect="sqlite"),
        )

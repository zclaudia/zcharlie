"""Static stress configuration heads, frozen inputs and mutation receipts.

Services must check owner/account relationships explicitly: this application
does not enforce SQLite foreign keys. Job state lives in the existing queue;
result IDs and frozen inputs are never updated in a historical revision.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
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


class PortfolioStressHead(Base):
    __tablename__ = "portfolio_stress_head"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_portfolio_stress_revision"),
        CheckConstraint("control_revision >= 1", name="ck_portfolio_stress_control_revision"),
        CheckConstraint("permission_epoch >= 1", name="ck_portfolio_stress_permission_epoch"),
        Index("ix_portfolio_stress_owner_account_created", "owner_id", "account_id", "created_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("portfolio_account.id"))
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    control_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean(create_constraint=True, name="ck_portfolio_stress_enabled"))
    permission_epoch: Mapped[int] = mapped_column(default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioStressRevision(Base):
    __tablename__ = "portfolio_stress_revision"
    __table_args__ = (
        UniqueConstraint("owner_id", "result_id", name="uq_portfolio_stress_result"),
        CheckConstraint("revision >= 1", name="ck_portfolio_stress_history_revision"),
        CheckConstraint("length(input_hash) = 64", name="ck_portfolio_stress_input_hash"),
        CheckConstraint("json_valid(payload) AND json_type(payload) = 'object'", name="ck_portfolio_stress_payload"),
        Index("ix_portfolio_stress_history_account", "owner_id", "account_id", "requested_at"),
        {"sqlite_with_rowid": False},
    )

    stress_id: Mapped[str] = mapped_column(String(36), ForeignKey("portfolio_stress_head.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[str] = mapped_column(String(36), ForeignKey("portfolio_account.id"))
    input_hash: Mapped[str] = mapped_column(String(64))
    result_id: Mapped[str] = mapped_column(String(36))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class PortfolioStudyOperation(Base):
    __tablename__ = "portfolio_study_operation"
    __table_args__ = (
        CheckConstraint("length(action) > 0", name="ck_portfolio_study_action"),
        CheckConstraint("length(request_hash) = 64", name="ck_portfolio_study_request_hash"),
        CheckConstraint("json_valid(response_payload) AND json_type(response_payload) = 'object'", name="ck_portfolio_study_response"),
        Index("ix_portfolio_study_operation_created", "owner_id", "created_at"),
        {"sqlite_with_rowid": False},
    )

    owner_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action: Mapped[str] = mapped_column(String(80))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _immutable(*args: object) -> None:
    raise ValueError("portfolio study history and receipts are immutable")


def _history_statements(table: str, identities: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    statements = tuple(
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_{operation.lower()} "
        f"BEFORE {operation} ON {table} BEGIN "
        "SELECT RAISE(ABORT, 'portfolio study history and receipts are immutable'); END"
        for operation in ("UPDATE", "DELETE")
    )
    matches = " OR ".join("(" + " AND ".join(f"{field} IS NEW.{field}" for field in identity) + ")"
                          for identity in identities)
    return (*statements,
            f"CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_insert BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {matches}) BEGIN "
            "SELECT RAISE(ABORT, 'portfolio study history and receipts are immutable'); END")


_HISTORY = (
    (PortfolioStressRevision, (("stress_id", "revision"), ("owner_id", "result_id"))),
    (PortfolioStudyOperation, (("owner_id", "operation_id"),)),
)
PORTFOLIO_STUDY_IMMUTABILITY_DDL = tuple(
    statement for model, identities in _HISTORY
    for statement in _history_statements(model.__tablename__, identities)
)
for _model, _identities in _HISTORY:
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)
    for _statement in _history_statements(_model.__tablename__, _identities):
        event.listen(_model.__table__, "after_create", DDL(_statement).execute_if(dialect="sqlite"))  # type: ignore[no-untyped-call]

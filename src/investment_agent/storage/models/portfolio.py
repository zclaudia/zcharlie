"""Append-only facts and immutable snapshots, one CAS head per account."""
from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class PortfolioAccount(Base):
    __tablename__ = "portfolio_account"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(nullable=False)
    current_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    current_valuation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    """Plain pointer (no FK): the newest valuation bound to the current revision."""
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PortfolioValuation(Base):
    """Immutable daily price/P&L projection over one ledger snapshot (plan 2.10).

    Fact versions and valuation versions are separate: appending facts advances
    the account revision and writes a new snapshot; prices only ever produce new
    valuation rows bound to a snapshot (never a rewritten history).
    """

    __tablename__ = "portfolio_valuation"
    __table_args__ = (
        UniqueConstraint("account_id", "id", name="uq_portfolio_valuation_identity"),
        Index("ix_portfolio_valuation_current", "account_id", "ledger_revision", "valuation_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account.id"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ledger_revision: Mapped[int] = mapped_column(nullable=False)
    valuation_date: Mapped[date] = mapped_column(nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PortfolioEvent(Base):
    __tablename__ = "portfolio_event"
    __table_args__ = (
        UniqueConstraint("account_id", "revision", name="uq_portfolio_event_revision"),
        UniqueConstraint("account_id", "idempotency_key", name="uq_portfolio_event_key"),
        UniqueConstraint("account_id", "batch_id", "row_id", name="uq_portfolio_import_row"),
        UniqueConstraint("account_id", "reverses_id", name="uq_portfolio_reversal"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account.id"), nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    row_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reverses_id: Mapped[str | None] = mapped_column(ForeignKey("portfolio_event.id"), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshot"
    __table_args__ = (UniqueConstraint("account_id", "revision", name="uq_portfolio_snapshot_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account.id"), nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PortfolioRequest(Base):
    """Every authenticated key binds its exact body, including import replays."""
    __tablename__ = "portfolio_request"
    account_id: Mapped[str] = mapped_column(ForeignKey("portfolio_account.id"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("portfolio_event.id"), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)

"""iFinD on-demand verification queue and budget ledger (P1-A13, migration 019).

* ``ifind_call_ledger`` - one row per ifind-proxy call (any path: verification
  jobs, the watchlist A3 collectors, the single-stock research chain). The
  verification budget counts the rows of a rolling 5-hour window.
* ``verification_request`` - a queued iFinD verification of one listing
  (trigger ``promote`` / ``conflict`` / ``missing`` / ``exit_review`` /
  ``calibration``). At most one *open* (``pending`` / ``deferred``) request
  exists per ``dedup_key`` = ``<listing>:<trigger>:<report period | latest>``
  (partial unique index); a repeated request merges into it.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin

LEDGER_OUTCOMES = frozenset({"ok", "error", "quota", "budget"})
VERIFICATION_TRIGGERS: tuple[str, ...] = (
    "promote",
    "conflict",
    "missing",
    "exit_review",
    "calibration",
)
VERIFICATION_STATUSES = frozenset(
    {"pending", "deferred", "running", "done", "failed", "superseded"}
)
OPEN_STATUSES: tuple[str, ...] = ("pending", "deferred")
_OPEN_WHERE = text("status IN ('pending', 'deferred')")


class IfindCallLedger(Base):
    __tablename__ = "ifind_call_ledger"
    __table_args__ = (Index("ix_ifind_call_ledger_at", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    ticker_count: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    """``verification`` / ``snapshot`` / ``research`` / ``cli``."""
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verification_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class VerificationRequest(Base, TimestampMixin):
    __tablename__ = "verification_request"
    __table_args__ = (
        Index(
            "uq_verification_request_open",
            "dedup_key",
            unique=True,
            sqlite_where=_OPEN_WHERE,
            postgresql_where=_OPEN_WHERE,
        ),
        Index("ix_verification_request_queue", "status", "priority", "requested_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    """Requested factor-layer fields; ``None`` = every key field (+ close)."""
    report_period: Mapped[date | None] = mapped_column(Date, nullable=True)
    """``None`` = the latest annual period known when the job runs."""
    detail: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The run that asked for the verification (e.g. an AkShare collection)."""
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The ``ifind_verify`` run that processed (or is processing) the request."""
    result: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

"""Zone events and interval state (watch-holding-plan 6.2 WP8, funnel 5.4/6/9).

A *zone event* records that one of a rule's fixed zones held (``entered``) or
stopped holding (``left``) for one listing on one ``as_of``, together with the
exact rule version that was evaluated and the inputs it was evaluated against.
Evaluation is deterministic, so the event identity is
``(listing, scope, zone, rule instance, as_of, transition, input digest)``:
re-running the same task on the same facts/prices and rule version inserts
nothing new, while revised facts or prices become a new identity instead of
rewriting the old record (plan 6.2 WP8).

``rule_instance_id`` is **not nullable**: an event without the rule version it
came from is a defect, so the database refuses to record one. Which rule
version fired is read back from this row, never re-derived from the current
active rule - history shows "当时的规则" (funnel 5.5).

``zone_interval_state`` is the enter/exit memory behind funnel 9's "notify once
on entering, again after leaving and re-entering": one row per
``(listing, scope, zone)`` holding whether the listing is currently inside the
zone under the current rule version, when it entered, and when that entry was
last notified. A new active rule version resets the interval (a new commitment
deserves its own notification).
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin

ZONE_SCOPES = ("candidate", "holding")
ZONE_TRANSITIONS = ("entered", "left")
ZONE_LEVELS = ("info", "action_required")


class ZoneEventRecord(Base):
    __tablename__ = "zone_event"
    __table_args__ = (
        UniqueConstraint(
            "listing_id",
            "scope",
            "zone",
            "rule_instance_id",
            "as_of",
            "transition",
            "input_digest",
            name="uq_zone_event_identity",
        ),
        Index("ix_zone_event_listing_scope", "listing_id", "scope", "as_of"),
        Index("ix_zone_event_scope_as_of", "scope", "as_of"),
        Index("ix_zone_event_rule", "rule_instance_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    """The date the inputs describe (candidate factors, or the valuation date)."""
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    zone: Mapped[str] = mapped_column(String(32), nullable=False)
    """``buy_zone`` / ``drop`` (candidate) or ``add`` / ``trim`` / ``exit_review``."""
    transition: Mapped[str] = mapped_column(String(8), nullable=False)
    """``entered`` (the zone holds) or ``left`` (it stopped holding)."""
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    """Notification level from funnel 9; ``left`` events are recorded, not sent."""
    rule_instance_id: Mapped[int] = mapped_column(
        ForeignKey("rule_instance.id"), nullable=False
    )
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    """The version of the rule at trigger time (funnel 5.5, 历史回看)."""
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    calibration_status: Mapped[str] = mapped_column(String(16), nullable=False)
    conditions_met: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    """The conditions that held, in the screen reason shape (factor/op/threshold/
    param/actual/quality/flags/snapshot_ref); empty for a ``left`` event."""
    inputs: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    """The facts/prices pair the evaluation read (factor refs, valuation refs)."""
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    """Holding-scope provenance: the accounts whose valuations were aggregated."""
    valuation_ids: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)
    ledger_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    knowledge_cutoff: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the entry notice for this interval was delivered (NULL for ``left``
    events, and for entries whose interval was already notified)."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ZoneIntervalStateRecord(Base, TimestampMixin):
    """The enter/exit memory of one ``(listing, scope, zone)`` (funnel 9)."""

    __tablename__ = "zone_interval_state"

    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    zone: Mapped[str] = mapped_column(String(32), primary_key=True)
    rule_instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The rule version the interval is measured under; a change resets it."""
    inside: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    entered_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Audit pointer to the newest event row of this interval."""
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When the current/last entry was notified; an entry notifies exactly once."""
    last_evaluated_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

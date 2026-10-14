"""Mutable state, cost basis and per-row trace of one ledger replay.

``ProjectionState`` is the single object every event handler mutates in place;
the handlers return nothing, so the caller observes exactly the accumulators
they wrote. The per-row trace has one writer here instead of three scattered
blocks: capture the pre-row state, note an excluded backfill, or close the row
with its applied deltas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.contracts import (
    Balance,
    CashFact,
    CorporateAction,
    DividendRecord,
    EventFact,
    EventRecord,
    RealizedPnL,
    ReconciliationComparison,
    Trade,
)


@dataclass
class CostState:
    """Remaining-cost projection state of one listing (plan 2.7)."""

    quantity: Decimal
    remaining: Decimal | None
    opened_on: Any = None
    reason: str | None = None


def fact_refs(fact: EventFact) -> tuple[ObjectRef, ...]:
    """The listings one fact names (empty for pure cash facts)."""

    if isinstance(fact, Balance):
        return tuple(holding.listing_ref for holding in fact.holdings)
    if isinstance(fact, Trade):
        return (fact.holding.listing_ref,)
    if isinstance(fact, (CashFact, CorporateAction)) and fact.listing_ref is not None:
        return (fact.listing_ref,)
    return ()


@dataclass
class ProjectionState:
    """Every accumulator of one replay; handlers mutate this object in place."""

    # The active ledger and its confirmed baseline (selected before the loop).
    active: list[EventRecord]
    opening: EventRecord | None
    baseline_key: tuple[datetime, int] | None
    # ``None`` disables tracing; a mapping keyed by event id is written per row.
    trace: dict[UUID, dict[str, Any]] | None = None
    # Accumulators (initial values match the historical projection exactly).
    excluded: list[UUID] = field(default_factory=list)
    quantities: dict[ObjectRef, Decimal] = field(default_factory=dict)
    restricted: set[ObjectRef] = field(default_factory=set)
    cash: Decimal | None = None
    cash_unknown: set[str] = field(default_factory=lambda: {"missing_confirmed_balance"})
    history: set[str] = field(default_factory=set)
    conflicts: list[str] = field(default_factory=list)
    comparisons: list[ReconciliationComparison] = field(default_factory=list)
    has_balance: bool = False
    coverage: Literal["complete", "partial"] = "partial"
    scope: str = "no active confirmed balance"
    costs: dict[ObjectRef, CostState] = field(default_factory=dict)
    cost_history: set[str] = field(default_factory=set)
    realized_records: list[RealizedPnL] = field(default_factory=list)
    dividends: list[DividendRecord] = field(default_factory=list)
    deficits: list[tuple[UUID, ObjectRef]] = field(default_factory=list)
    # Pre-row state the traced row's deltas are measured against.
    before_quantities: dict[ObjectRef, Decimal] = field(default_factory=dict)
    before_cash: Decimal | None = None

    def mark_unknown(self, issue: str) -> None:
        """Record one unknown-cash reason; ``close_row`` carries it into history."""

        self.cash_unknown.add(issue)

    def mark_conflict(self, issue: str) -> None:
        self.conflicts.append(issue)

    def mark_deficit(self, event_id: UUID, ref: ObjectRef) -> None:
        """A sell (event id, listing) whose confirmed basis was known and too small."""

        self.deficits.append((event_id, ref))

    def begin_row(self, record: EventRecord) -> None:
        """Capture the pre-row state a traced row diffs against."""

        traced = self.trace is not None and record.id in self.trace
        self.before_quantities = {}
        self.before_cash = None
        if traced:
            self.before_quantities, self.before_cash = dict(self.quantities), self.cash

    def exclude_before_baseline(self, record: EventRecord) -> None:
        """An event older than the confirmed baseline: audit only, never applied."""

        self.excluded.append(record.id)
        if self.trace is not None and record.id in self.trace:
            self.trace[record.id] = {"inclusion": "audit_prior_history"}

    def close_row(self, record: EventRecord, fact: EventFact) -> None:
        """The shared row tail: cash check, unknown history, then the trace entry."""

        if self.cash is not None and self.cash < 0:
            issue = f"negative_cash:{record.id}"
            self.mark_conflict(issue)
            self.mark_unknown(issue)
        self.history.update(self.cash_unknown)
        self.history.update(f"unknown_quantity:{ref.id}" for ref in self.restricted)
        if self.trace is not None and record.id in self.trace:
            refs = fact_refs(fact)
            self.trace[record.id] = {
                "inclusion": "applied",
                "quantity_delta": {
                    str(ref.id): self.quantities.get(ref, Decimal(0))
                    - self.before_quantities.get(ref, Decimal(0))
                    for ref in refs
                },
                "cash_delta": (
                    self.cash - self.before_cash
                    if self.cash is not None and self.before_cash is not None
                    else None
                ),
                "cost_remaining": {
                    str(ref.id): self.costs[ref].remaining
                    for ref in refs
                    if ref in self.costs and self.costs[ref].remaining is not None
                },
            }

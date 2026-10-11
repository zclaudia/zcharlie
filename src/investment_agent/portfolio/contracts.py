"""AG21 wire contracts. Decimal strings, explicit coverage and confirmation."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.investor.common import Contract, DecimalValue, ObjectRef, Text

LedgerDecimal = Annotated[DecimalValue, Field(max_digits=40, decimal_places=18)]


class Holding(Contract):
    listing_ref: ObjectRef
    quantity: Annotated[LedgerDecimal, Field(ge=0)]
    average_cost: Annotated[LedgerDecimal, Field(ge=0)] | None = None
    """Confirmed remaining average acquisition cost, fees included (plan 2.7)."""
    opened_on: date | None = None
    """Confirmed start of the current holding cycle; never after ``occurred_at``."""

    @model_validator(mode="after")
    def listing(self) -> "Holding":
        if self.listing_ref.kind != "listing" or self.listing_ref.revision != 1 or self.listing_ref.field_path is not None:
            raise ValueError("exact listing identity required")
        return self


class Fact(Contract):
    occurred_at: datetime
    currency: Literal["CNY"] = "CNY"
    source: Text
    user_confirmed: Literal[True]


class Balance(Fact):
    kind: Literal["opening_balance", "reconciliation"]
    coverage: Literal["complete", "partial"]
    asset_scope: Text
    holdings: tuple[Holding, ...]
    cash: LedgerDecimal | None
    unexplained_difference: Text | None = None
    # The confirmed balance includes all facts at or before this boundary;
    # backfilled earlier events are retained for audit but never applied twice.
    prior_history: Literal["included_in_confirmed_balance"]

    @model_validator(mode="after")
    def unique_holdings(self) -> "Balance":
        if len({h.listing_ref.id for h in self.holdings}) != len(self.holdings):
            raise ValueError("duplicate listing balance")
        if self.kind == "reconciliation" and self.unexplained_difference is None:
            raise ValueError("reconciliation requires explicit difference explanation, including none")
        for holding in self.holdings:
            if holding.opened_on is not None and holding.opened_on > self.occurred_at.astimezone(SHANGHAI_TZ).date():
                raise ValueError("holding cycle start cannot be after the balance confirmation")
        return self


class Trade(Fact):
    kind: Literal["trade"]
    holding: Holding
    direction: Literal["buy", "sell"]
    actual_price: Annotated[LedgerDecimal, Field(gt=0)]
    fee: Annotated[LedgerDecimal, Field(ge=0)] | None

    @model_validator(mode="after")
    def positive_quantity(self) -> "Trade":
        if self.holding.quantity <= 0:
            raise ValueError("trade quantity must be positive; actual odd lots are allowed")
        return self


class CashFact(Fact):
    kind: Literal["cash_transfer", "dividend"]
    amount: LedgerDecimal
    listing_ref: ObjectRef | None = None

    @model_validator(mode="after")
    def dividend_listing(self) -> "CashFact":
        if self.kind == "dividend" and self.listing_ref is None:
            raise ValueError("dividend needs listing identity")
        if self.listing_ref is not None:
            Holding(listing_ref=self.listing_ref, quantity=Decimal(0))
        return self


class CorporateAction(Fact):
    kind: Literal["corporate_action"]
    listing_ref: ObjectRef
    action: Literal["split", "pending"]
    ratio: Annotated[LedgerDecimal, Field(gt=0)] | None = None
    affects: tuple[Literal["quantity", "cash"], ...] = ("quantity", "cash")
    description: Text

    @model_validator(mode="after")
    def parameters(self) -> "CorporateAction":
        Holding(listing_ref=self.listing_ref, quantity=Decimal(0))
        if self.action == "split" and (self.ratio is None or self.affects != ("quantity",)):
            raise ValueError("split requires ratio and quantity-only scope")
        if self.action == "pending" and (self.ratio is not None or not self.affects):
            raise ValueError("pending action requires affected scope and no guessed ratio")
        return self


class Reversal(Fact):
    kind: Literal["reversal"]
    original_event_id: UUID
    reason: Text


EventFact = Annotated[Balance | Trade | CashFact | CorporateAction | Reversal, Field(discriminator="kind")]


class AppendEvent(Contract):
    account_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    idempotency_key: UUID
    batch_id: UUID | None = None
    row_id: Text | None = None
    fact: EventFact

    @model_validator(mode="after")
    def import_identity(self) -> "AppendEvent":
        if (self.batch_id is None) != (self.row_id is None):
            raise ValueError("batch_id and row_id required together")
        return self


class ApplyCorrection(Contract):
    """Atomic correction: one reversal plus optional replacement facts (plan 2.8).

    The whole set is bound to one operation UUID, the account revision the user
    previewed and this exact body; it is written in a single transaction (the
    account head only ever exposes the final state) and a retried identical
    operation returns the original result instead of replaying half a batch.
    """

    account_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    operation_id: UUID
    reason: Text
    reversals: tuple[UUID, ...]
    replacements: tuple[EventFact, ...] = ()

    @model_validator(mode="after")
    def shape(self) -> "ApplyCorrection":
        if not self.reason.strip():
            raise ValueError("a correction requires a reason")
        if not self.reversals:
            raise ValueError("a correction reverses at least one event")
        if len(set(self.reversals)) != len(self.reversals):
            raise ValueError("duplicate reversal targets")
        if any(isinstance(fact, Reversal) for fact in self.replacements):
            raise ValueError("replacements cannot themselves be reversals")
        return self


class CreateAccount(Contract):
    idempotency_key: UUID
    name: Text
    currency: Literal["CNY"] = "CNY"


class Account(Contract):
    id: UUID
    name: Text
    currency: Literal["CNY"]
    revision: int
    current_snapshot_id: UUID | None


class EventRecord(Contract):
    id: UUID
    revision: int
    recorded_at: datetime
    request: AppendEvent


class EventPage(Contract):
    items: tuple[EventRecord, ...]
    next_after_revision: int | None
    observed_revision: int


class SnapshotHolding(Contract):
    listing_ref: ObjectRef
    confirmed_quantity: DecimalValue | None
    quantity_subtotal: DecimalValue
    price: DecimalValue | None = None
    price_ref: ObjectRef | None = None
    price_date: date | None = None
    market_value: DecimalValue | None = None
    weight: DecimalValue | None = None
    remaining_cost: DecimalValue | None = None
    """Remaining total acquisition cost; ``None`` when not confirmed (plan 2.7)."""
    average_cost: DecimalValue | None = None
    opened_on: date | None = None
    """Start of the current holding cycle; ``None`` = holding days unavailable."""
    cost_unknown_reason: str | None = None


class RealizedPnL(Contract):
    """One sell's realized result; unknown amounts stay ``None`` (plan 2.7)."""

    event_id: UUID
    listing_ref: ObjectRef
    quantity: DecimalValue
    gross: DecimalValue
    fee: DecimalValue | None
    cost_carried: DecimalValue | None
    realized: DecimalValue | None
    cycle_opened_on: date | None = None


class DividendRecord(Contract):
    event_id: UUID
    listing_ref: ObjectRef | None
    amount: DecimalValue


class HoldingComparison(Contract):
    listing_ref: ObjectRef
    before: DecimalValue | None
    confirmed: DecimalValue | None
    delta: DecimalValue | None
    unknown_reason: str | None


class ReconciliationComparison(Contract):
    event_id: UUID
    occurred_at: datetime
    before_cash: DecimalValue | None
    confirmed_cash: DecimalValue | None
    cash_delta: DecimalValue | None
    cash_unknown_reason: str | None
    holdings: tuple[HoldingComparison, ...]
    user_difference_explanation: Text


class Snapshot(Contract):
    id: UUID
    account_id: UUID
    revision: int
    recorded_at: datetime
    fact_as_of: datetime
    valuation_at: datetime
    currency: Literal["CNY"] = "CNY"
    coverage: Literal["complete", "partial"]
    asset_scope: Text
    known_cash: LedgerDecimal | None
    cash_subtotal: DecimalValue | None
    holdings: tuple[SnapshotHolding, ...]
    known_subtotal: DecimalValue | None
    total_assets: DecimalValue | None
    unknowns: tuple[str, ...]
    historical_unknowns: tuple[str, ...]
    reconciliations: tuple[ReconciliationComparison, ...]
    conflicts: tuple[str, ...]
    source_event_ids: tuple[UUID, ...]
    excluded_prior_event_ids: tuple[UUID, ...]
    reference: ObjectRef
    price_coverage: Literal["complete", "partial"]
    cost_method_version: str = "weighted_average_remaining_v1"
    """Reviewed in plan 2.7; a change here is a new method version, not a patch."""
    realized: tuple[RealizedPnL, ...] = ()
    dividends: tuple[DividendRecord, ...] = ()
    cost_unknowns: tuple[str, ...] = ()
    returns: None = None


class AppendResult(Contract):
    event: EventRecord
    snapshot: Snapshot


class CorrectionResult(Contract):
    operation_id: UUID
    events: tuple[EventRecord, ...]
    snapshot: Snapshot


class ImportRow(Contract):
    """One statement line of a whole-batch import (plan 2.8 整批导入)."""

    row_id: Text
    fact: EventFact

    @model_validator(mode="after")
    def not_a_reversal(self) -> "ImportRow":
        if isinstance(self.fact, Reversal):
            raise ValueError("reversals belong to the atomic correction path, not an import batch")
        return self


class ImportBatch(Contract):
    """A frozen whole-batch import: one operation UUID, one base revision, exact rows.

    Batch identity is the operation UUID *and* the exact body: a lost receipt is
    retried with the same request, the whole batch is replayed by
    ``(occurred_at, recorded revision)`` and committed under one account CAS -
    never a sequence of independent appends (plan 2.8).
    """

    account_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    operation_id: UUID
    source: Text
    rows: Annotated[tuple[ImportRow, ...], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def unique_rows(self) -> "ImportBatch":
        if len({row.row_id for row in self.rows}) != len(self.rows):
            raise ValueError("duplicate row ids")
        return self


class ImportRowEffect(Contract):
    """What one row does when the batch commits (server-computed, never the client)."""

    row_id: Text
    occurred_at: datetime
    kind: Text
    inclusion: Literal["applied", "audit_prior_history"]
    """``audit_prior_history``: the row predates the confirmed balance boundary,
    is recorded for audit and changes no quantity, cost or cash (plan 2.6)."""
    accepted: bool
    reasons: tuple[Text, ...] = ()
    quantity_delta: dict[Text, DecimalValue] = {}
    """listing id -> applied quantity change."""
    cash_delta: DecimalValue | None = None
    cost_remaining: dict[Text, DecimalValue] = {}
    """listing id -> remaining total cost after the row (when known)."""


class ImportPreview(Contract):
    account_id: UUID
    base_revision: int
    batch_hash: Text
    ready: bool
    """``False``: the commit would be rejected; every conflict names its row."""
    rows: tuple[ImportRowEffect, ...]
    conflicts: tuple[Text, ...]
    resulting_revision: int


class ImportResult(Contract):
    operation_id: UUID
    events: tuple[EventRecord, ...]
    snapshot: Snapshot
    audit_only_row_ids: tuple[Text, ...] = ()

"""Typed contracts for account-fit inputs and immutable results.

These DTOs describe user scenarios and server-frozen evidence. They do not
confirm production trading rules or authorize orders.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_agent.investor.common import Contract, DecimalValue, ObjectRef, Text
from investment_agent.investor.contracts import Mandate
from investment_agent.portfolio.calculation_contracts import Fraction, Nonnegative, Positive
from investment_agent.portfolio.calculations import TrustedTradingRule
from investment_agent.portfolio.contracts import Snapshot
from investment_agent.portfolio.purposes import PurposeVersion
from investment_agent.portfolio.valuation import Valuation

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Two scenarios may each report up to three diagnostics per target/holding,
# twelve per purpose, and a bounded set of shared checks. Keep room for the
# complete 500-holding contract without allowing unbounded diagnostic payloads.
MAX_FIT_CHECKS = 4096


class ValuationBinding(Contract):
    state: Literal["available", "missing"]
    valuation_ref: ObjectRef | None = None
    reason: Text | None = None

    @model_validator(mode="after")
    def binding_shape(self) -> ValuationBinding:
        if self.state == "available":
            if self.valuation_ref is None or self.reason is not None:
                raise ValueError("available valuation requires an exact ref and no missing reason")
            if self.valuation_ref.kind != "portfolio_valuation" or self.valuation_ref.field_path:
                raise ValueError("exact portfolio valuation reference required")
        elif self.valuation_ref is not None or self.reason is None:
            raise ValueError("missing valuation requires a reason and no reference")
        return self


class PurposeBinding(Contract):
    state: Literal["selected", "confirmed_empty", "none_recorded", "unselected", "unknown"]
    purpose_ref: ObjectRef | None = None
    purpose_id: UUID | None = None
    confirmed_at: datetime | None = None
    reason: Text | None = None

    @model_validator(mode="after")
    def binding_shape(self) -> PurposeBinding:
        if self.state == "selected":
            if self.purpose_ref is None or self.purpose_id is None or self.confirmed_at is None:
                raise ValueError(
                    "selected purpose requires exact version, purpose id and confirmation time"
                )
            if self.purpose_ref.kind != "portfolio_purposes" or self.purpose_ref.field_path:
                raise ValueError("exact portfolio purpose version required")
        elif self.state == "confirmed_empty":
            if self.purpose_ref is None or self.purpose_id is not None or self.confirmed_at is None:
                raise ValueError(
                    "confirmed empty requires an exact empty version and confirmation time"
                )
            if self.purpose_ref.kind != "portfolio_purposes" or self.purpose_ref.field_path:
                raise ValueError("exact portfolio purpose version required")
        elif self.state == "unknown":
            if self.reason is None:
                raise ValueError("unknown purpose binding requires a reason")
            if any(v is not None for v in (self.purpose_ref, self.purpose_id, self.confirmed_at)):
                raise ValueError("unknown purpose binding cannot carry a version or purpose id")
        elif any(
            v is not None
            for v in (self.purpose_ref, self.purpose_id, self.confirmed_at, self.reason)
        ):
            raise ValueError("none_recorded and unselected carry no selected purpose identity")
        return self

    def validate_version(self, version: PurposeVersion | None) -> None:
        """Check the server-frozen purpose version against this binding."""
        if self.state in {"selected", "confirmed_empty"}:
            if version is None or version.reference != self.purpose_ref:
                raise ValueError("purpose version does not match binding")
            if version.confirmed_at != self.confirmed_at:
                raise ValueError("purpose confirmation time does not match binding")
            if self.state == "selected" and self.purpose_id not in {p.id for p in version.purposes}:
                raise ValueError("selected purpose id is absent from exact purpose version")
            if self.state == "confirmed_empty" and version.purposes:
                raise ValueError("confirmed_empty requires a purpose version with no purposes")
        elif version is not None and self.state == "none_recorded":
            raise ValueError("none_recorded cannot carry a purpose version")
        elif self.state == "unselected" and (version is None or not version.purposes):
            raise ValueError("unselected requires a non-empty frozen purpose version")


class TradeAssumptions(Contract):
    """Nullable scenario inputs; every absent amount is explicitly unknown."""

    execution_price: Positive | None = None
    fixed_fee: Nonnegative | None = None
    fee_rate: Fraction | None = None
    assumption_source: Text
    unknown_fields: tuple[Literal["execution_price", "fixed_fee", "fee_rate"], ...] = ()

    @model_validator(mode="after")
    def explicit_unknowns(self) -> TradeAssumptions:
        values = {
            "execution_price": self.execution_price,
            "fixed_fee": self.fixed_fee,
            "fee_rate": self.fee_rate,
        }
        if len(set(self.unknown_fields)) != len(self.unknown_fields):
            raise ValueError("duplicate unknown assumption field")
        for name, value in values.items():
            if (value is None) != (name in self.unknown_fields):
                raise ValueError("null assumptions must be explicitly listed as unknown")
        if self.execution_price is not None and self.execution_price <= 0:
            raise ValueError("execution price must be positive")
        if self.fixed_fee is not None and self.fixed_fee < 0:
            raise ValueError("fixed fee cannot be negative")
        return self


class FitRequestItem(Contract):
    listing_ref: ObjectRef
    action: Literal["open", "add", "hold"]
    trade_assumptions: TradeAssumptions | None = None

    @model_validator(mode="after")
    def action_inputs(self) -> FitRequestItem:
        if (
            self.listing_ref.kind != "listing"
            or self.listing_ref.revision != 1
            or self.listing_ref.field_path
        ):
            raise ValueError("exact listing reference required")
        if (self.action == "hold") != (self.trade_assumptions is None):
            raise ValueError("hold has no trade assumptions; open/add require explicit assumptions")
        return self


class HistoricalReplayBinding(Contract):
    run_ref: ObjectRef
    manifest_hash: Sha256
    input_as_of: datetime

    @model_validator(mode="after")
    def run_identity(self) -> HistoricalReplayBinding:
        if (
            self.run_ref.kind != "pipeline_run"
            or not isinstance(self.run_ref.id, int)
            or self.run_ref.revision != 1
        ):
            raise ValueError("historical replay requires an exact existing pipeline run reference")
        if any(c not in "0123456789abcdef" for c in self.manifest_hash):
            raise ValueError("manifest hash must be lowercase SHA-256 hex")
        return self


class AccountAssessmentRequest(Contract):
    """Shared immutable account/snapshot bindings used by account assessments."""

    operation_id: UUID
    account_id: UUID
    snapshot_ref: ObjectRef
    mandate_ref: ObjectRef
    valuation_binding: ValuationBinding
    purpose_binding: PurposeBinding
    mode: Literal["live", "historical_replay"] = "live"
    historical_replay: HistoricalReplayBinding | None = None

    @model_validator(mode="after")
    def assessment_identity(self) -> AccountAssessmentRequest:
        if self.snapshot_ref.kind != "portfolio_snapshot" or self.snapshot_ref.field_path:
            raise ValueError("exact portfolio snapshot required")
        if self.mandate_ref.kind != "mandate" or self.mandate_ref.field_path:
            raise ValueError("exact mandate version required")
        if (self.mode == "historical_replay") != (self.historical_replay is not None):
            raise ValueError("historical replay mode requires exactly one frozen replay binding")
        return self


class AccountFitRequest(AccountAssessmentRequest):
    items: Annotated[tuple[FitRequestItem, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_listings(self) -> AccountFitRequest:
        if len({item.listing_ref for item in self.items}) != len(self.items):
            raise ValueError("duplicate listing in account-fit request")
        return self


class FrozenPrice(Contract):
    listing_ref: ObjectRef
    price: DecimalValue | None
    price_ref: ObjectRef | None
    price_date: date | None
    provider: Text | None
    captured_at: datetime | None
    available_at: datetime | None
    acquired_at: datetime | None
    unknown_reason: Text | None = None

    @model_validator(mode="after")
    def price_shape(self) -> FrozenPrice:
        if self.price is None:
            if self.unknown_reason is None:
                raise ValueError("missing price requires a reason")
        elif self.price <= 0 or self.price_ref is None or self.price_ref.kind != "market_snapshot":
            raise ValueError(
                "available price requires a positive value and market snapshot reference"
            )
        return self


class FrozenSector(Contract):
    listing_ref: ObjectRef
    system: Literal["CSRC"] = "CSRC"
    row_id: int | None
    code: str | None
    name: Text | None
    valid_from: date | None
    valid_to: date | None
    source: Text | None
    captured_at: datetime | None
    available_at: datetime | None
    acquired_at: datetime | None
    unknown_reason: Text | None = None

    @model_validator(mode="after")
    def sector_shape(self) -> FrozenSector:
        if self.name is None and self.unknown_reason is None:
            raise ValueError("missing sector classification requires a reason")
        if self.name is not None and (self.source is None or self.captured_at is None):
            raise ValueError("classified sector requires its source and capture time")
        return self


class FrozenListingSource(Contract):
    listing_ref: ObjectRef
    market_code: Text
    name: Text
    ticker: Text
    listing_source_ref: ObjectRef
    price: FrozenPrice
    sector: FrozenSector
    trusted_rule: TrustedTradingRule | None
    rule_unknown_reason: Text | None = None

    @model_validator(mode="after")
    def source_identity(self) -> FrozenListingSource:
        if any(v.listing_ref != self.listing_ref for v in (self.price, self.sector)):
            raise ValueError("listing source components must share the listing identity")
        if (self.trusted_rule is None) != (self.rule_unknown_reason is not None):
            raise ValueError(
                "missing trading rule requires a reason; available rule cannot have one"
            )
        if self.trusted_rule is not None and self.trusted_rule.rule.listing_ref != self.listing_ref:
            raise ValueError("trading rule listing identity mismatch")
        return self


class FrozenAccountInputs(Contract):
    account_id: UUID
    snapshot: Snapshot
    valuation: Valuation | None
    valuation_binding: ValuationBinding
    mandate: Mandate
    purpose_version: PurposeVersion | None
    purpose_binding: PurposeBinding
    listing_sources: Annotated[tuple[FrozenListingSource, ...], Field(max_length=2500)]
    assessment_as_of: datetime
    price_basis_at: datetime | None
    mode: Literal["live", "historical_replay"]
    manifest_hash: Sha256
    source_fingerprint: Sha256

    @model_validator(mode="after")
    def exact_sources(self) -> FrozenAccountInputs:
        if self.snapshot.account_id != self.account_id:
            raise ValueError("snapshot account identity mismatch")
        if (
            self.purpose_binding.confirmed_at is not None
            and self.purpose_binding.confirmed_at > self.assessment_as_of
        ):
            raise ValueError("purpose confirmation cannot be after assessment_as_of")
        if self.valuation_binding.state == "available":
            if (
                self.valuation is None
                or self.valuation.reference != self.valuation_binding.valuation_ref
            ):
                raise ValueError("frozen valuation does not match available binding")
        elif self.valuation is not None:
            raise ValueError("missing valuation binding cannot embed an available valuation")
        self.purpose_binding.validate_version(self.purpose_version)
        if self.purpose_version is not None:
            if (
                self.purpose_version.reference.kind != "portfolio_purposes"
                or not isinstance(self.purpose_version.reference.id, UUID)
                or self.purpose_version.reference.field_path
                or self.purpose_version.snapshot_ref.kind != "portfolio_snapshot"
                or self.purpose_version.snapshot_ref.field_path
            ):
                raise ValueError("purpose version requires exact purpose and snapshot references")
            if self.purpose_version.account_id != self.account_id:
                raise ValueError("purpose version must belong to the frozen account")
        if len({item.listing_ref for item in self.listing_sources}) != len(self.listing_sources):
            raise ValueError("duplicate listing in frozen inputs")
        if self.valuation is None:
            if self.price_basis_at is not None:
                raise ValueError("missing valuation cannot define a price basis")
        elif (
            self.valuation.account_id != self.account_id
            or self.price_basis_at != self.valuation.knowledge_cutoff
        ):
            raise ValueError("valuation account or price basis mismatch")
        elif (
            self.valuation.snapshot_id != self.snapshot.id
            or self.valuation.ledger_revision != self.snapshot.revision
        ):
            raise ValueError("valuation must be bound to the frozen account snapshot")
        if self.price_basis_at is not None and self.price_basis_at > self.assessment_as_of:
            raise ValueError("price basis cannot be after assessment_as_of")
        source_times = [self.assessment_as_of]
        for source in self.listing_sources:
            source_times.extend(
                value
                for value in (
                    source.price.captured_at,
                    source.price.available_at,
                    source.price.acquired_at,
                    source.sector.captured_at,
                    source.sector.available_at,
                    source.sector.acquired_at,
                    source.trusted_rule.available_from if source.trusted_rule else None,
                )
                if value is not None
            )
        if any(value > self.assessment_as_of for value in source_times[1:]):
            raise ValueError("frozen source knowledge cannot be later than assessment_as_of")
        return self


class FitCheck(Contract):
    code: Text
    state: Literal["pass", "blocked", "unknown", "not_configured"]
    scope: Literal["baseline", "scenario"]
    reference: ObjectRef | None = None
    reason_codes: tuple[Text, ...] = ()


class AccountFitAssessment(Contract):
    id: UUID
    reference: ObjectRef
    operation_id: UUID
    input_hash: Sha256
    account_id: UUID
    listing_ref: ObjectRef
    action: Literal["open", "add", "hold"]
    request_item: FitRequestItem
    purpose_binding_key: Sha256
    calculation_basis_key: Sha256
    assessment_as_of: datetime
    price_basis_at: datetime | None
    expires_at: datetime
    frozen_inputs: FrozenAccountInputs
    financial_status: Literal["pass", "warning", "blocked", "unknown"]
    checks: Annotated[tuple[FitCheck, ...], Field(max_length=MAX_FIT_CHECKS)]
    calculation_refs: Annotated[tuple[ObjectRef, ...], Field(max_length=20)] = ()
    supplemental_binding: Literal["not_integrated"] = "not_integrated"
    research_status: Literal["not_assessed"] = "not_assessed"
    company_alignment: Literal["not_assessed"] = "not_assessed"
    ranking_bucket: Literal["incomplete", "blocked"]
    coverage: Literal["complete", "partial"]

    @model_validator(mode="after")
    def identities(self) -> AccountFitAssessment:
        if (
            self.reference.kind != "account_fit"
            or self.reference.id != self.id
            or self.reference.revision != 1
            or self.reference.field_path is not None
        ):
            raise ValueError("account-fit result requires its exact UUID reference at revision 1")
        if self.frozen_inputs.account_id != self.account_id:
            raise ValueError("assessment account must match frozen account inputs")
        if self.frozen_inputs.assessment_as_of != self.assessment_as_of:
            raise ValueError("assessment time must match frozen account inputs")
        if self.expires_at <= self.assessment_as_of:
            raise ValueError("assessment expiry must follow its frozen as-of time")
        if (
            self.listing_ref.kind != "listing"
            or not isinstance(self.listing_ref.id, int)
            or self.listing_ref.revision != 1
            or self.listing_ref.field_path is not None
        ):
            raise ValueError("exact listing reference required")
        if self.listing_ref not in {
            item.listing_ref for item in self.frozen_inputs.listing_sources
        }:
            raise ValueError("assessment listing must be present in frozen listing sources")
        if (
            self.request_item.listing_ref != self.listing_ref
            or self.request_item.action != self.action
        ):
            raise ValueError("request item identity must match assessment")
        if any(ref.kind != "calculation" for ref in self.calculation_refs):
            raise ValueError("calculation references must use the calculation kind")
        if any(
            check.reference is not None and check.reference.kind != "calculation"
            for check in self.checks
        ):
            raise ValueError("fit check references must use calculation references")
        if self.financial_status == "blocked" and self.ranking_bucket != "blocked":
            raise ValueError("blocked financial result must be in the blocked bucket")
        if self.ranking_bucket == "blocked" and self.financial_status != "blocked":
            raise ValueError("blocked bucket requires a proven financial block")
        return self


__all__ = [
    "AccountFitAssessment",
    "AccountFitRequest",
    "FitCheck",
    "FitRequestItem",
    "FrozenAccountInputs",
    "FrozenListingSource",
    "FrozenPrice",
    "FrozenSector",
    "MAX_FIT_CHECKS",
    "HistoricalReplayBinding",
    "PurposeBinding",
    "TradeAssumptions",
    "ValuationBinding",
]

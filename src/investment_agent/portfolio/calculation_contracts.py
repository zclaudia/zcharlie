"""AG22 scenarios are assumptions, never user mandate amendments or orders."""
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_agent.investor.common import Contract, DecimalValue, ObjectRef, Text
from investment_agent.portfolio.contracts import Holding, LedgerDecimal

Nonnegative = Annotated[LedgerDecimal, Field(ge=0)]
Positive = Annotated[LedgerDecimal, Field(gt=0)]
Fraction = Annotated[DecimalValue, Field(ge=0, le=1, max_digits=40, decimal_places=18)]


class ScenarioConstraints(Contract):
    nature: Literal["user_scenario", "agent_scenario"]
    source: Text
    max_single_weight: Fraction | None = None
    max_sector_weight: Fraction | None = None
    minimum_cash: Nonnegative | None = None
    minimum_cash_weight: Fraction | None = None


class TradingRule(Contract):
    listing_ref: ObjectRef
    source: Text
    version: Text
    nature: Literal["user_scenario", "agent_scenario"]
    buy_minimum: Positive
    buy_increment: Positive
    sell_minimum: Positive
    sell_increment: Positive
    full_odd_lot_sell: bool

    @model_validator(mode="after")
    def identity(self) -> "TradingRule":
        Holding(listing_ref=self.listing_ref, quantity=Decimal(0))
        return self


class ScenarioAction(Contract):
    listing_ref: ObjectRef
    direction: Literal["buy", "sell"]
    quantity: Positive | None = None
    # Maximum total buy outlay, including fixed and proportional fee.
    budget: Positive | None = None
    execution_price: Positive | None = None
    fixed_fee: Nonnegative | None = None
    fee_rate: Fraction | None = None
    purpose_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    assumption_source: Text
    nature: Literal["user_scenario", "agent_scenario"] = "agent_scenario"

    @model_validator(mode="after")
    def parameters(self) -> "ScenarioAction":
        Holding(listing_ref=self.listing_ref, quantity=Decimal(0))
        if (self.quantity is None) == (self.budget is None):
            raise ValueError("exactly one quantity or budget required")
        if self.budget is not None and self.direction != "buy":
            raise ValueError("budget is a buy outlay ceiling")
        return self


class Scenario(Contract):
    name: Text
    actions: Annotated[tuple[ScenarioAction, ...], Field(max_length=100)]


class PriceSelection(Contract):
    listing_ref: ObjectRef
    price_ref: ObjectRef

    @model_validator(mode="after")
    def identities(self) -> "PriceSelection":
        Holding(listing_ref=self.listing_ref, quantity=Decimal(0))
        if self.price_ref.kind != "market_snapshot" or self.price_ref.field_path is not None:
            raise ValueError("exact unadjusted market snapshot required")
        return self


class PortfolioScenarioInput(Contract):
    account_id: UUID
    snapshot_ref: ObjectRef
    valuation_ref: ObjectRef | None = Field(default=None, exclude_if=lambda value: value is None)
    """Exact daily valuation of the same snapshot (plan 2.5): its frozen prices
    and knowledge cutoff become the price basis of the calculation. Without it
    the calculation keeps the legacy ag22-v1 contract (prices frozen into the
    snapshot) - a new valuation is never substituted silently."""
    mandate_ref: ObjectRef
    purpose_ref: ObjectRef | None = Field(default=None, exclude_if=lambda value: value is None)
    constraints: ScenarioConstraints | None = None
    scenarios: Annotated[tuple[Scenario, ...], Field(min_length=1, max_length=20)]
    rules: Annotated[tuple[TradingRule, ...], Field(max_length=100)] = ()
    # Only new holdings need this. Existing holdings retain frozen snapshot prices.
    prices: Annotated[tuple[PriceSelection, ...], Field(max_length=100)] = ()
    max_price_age_days: Annotated[int, Field(strict=True, ge=0, le=30)] = 7
    industry_system: Literal["CSRC"] = "CSRC"

    @model_validator(mode="after")
    def references(self) -> "PortfolioScenarioInput":
        if self.snapshot_ref.kind != "portfolio_snapshot" or self.snapshot_ref.field_path is not None:
            raise ValueError("exact portfolio snapshot required")
        if self.valuation_ref is not None and (
            self.valuation_ref.kind != "portfolio_valuation"
            or self.valuation_ref.revision != 1
            or self.valuation_ref.field_path is not None
        ):
            raise ValueError("exact portfolio valuation required")
        if self.mandate_ref.kind != "mandate" or self.mandate_ref.field_path is not None:
            raise ValueError("exact mandate version required")
        if self.purpose_ref is not None and (self.purpose_ref.kind != "portfolio_purposes" or self.purpose_ref.field_path):
            raise ValueError("exact purpose version required")
        names = [s.name for s in self.scenarios]
        if len(set(names)) != len(names) or "no_action" in names:
            raise ValueError("unique scenario names; no_action is reserved")
        for values in (self.rules, self.prices):
            if len({v.listing_ref for v in values}) != len(values):
                raise ValueError("duplicate listing inputs")
        return self


class PortfolioCalculationRequest(PortfolioScenarioInput):
    operation_id: UUID

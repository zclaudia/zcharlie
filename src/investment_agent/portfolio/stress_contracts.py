"""S1 static stress contracts. Assumptions never constitute trade authority.

Complete paths require a complete asset scope, known cash and quantities, and
usable prices for nonzero holdings. Every reported usable holding price must
match its exact frozen source copy, including version and value. Zero quantity
requires no price. Undefined ratios alone do not make known amounts partial.
Source ownership and payload authenticity still require the PT-006/007 readers.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BeforeValidator, Field, StrictBool, model_validator

from investment_agent.investor.common import Contract, ObjectRef, Quantity, Revision
from investment_agent.portfolio.stress_canonical import input_decimal

MAX_PATHS = 4
MAX_SHOCKS = 20
MAX_SECURITIES = 50
STRESS_METHOD_VERSION: Literal["portfolio-stress-v1"] = "portfolio-stress-v1"
NUMERIC_RULES_VERSION: Literal["decimal-40-18-dynamic128-ratio40-half-even-cent-v1"] = (
    "decimal-40-18-dynamic128-ratio40-half-even-cent-v1"
)

InputDecimal = Annotated[Decimal, BeforeValidator(input_decimal)]
Nonnegative = Annotated[InputDecimal, Field(ge=0)]
Positive = Annotated[InputDecimal, Field(gt=0)]
Fraction = Annotated[InputDecimal, Field(ge=0, le=1)]
ShockRatio = Annotated[InputDecimal, Field(ge=-1, le=10)]
Name = Annotated[str, Field(min_length=1, max_length=100)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RunId = Annotated[int, Field(strict=True, ge=1)]
CheckState = Literal["pass", "fail", "unknown", "not_configured"]


def exact(ref: ObjectRef, kind: str, *, immutable: bool = False) -> None:
    if ref.kind != kind or ref.field_path is not None or (immutable and ref.revision != 1):
        raise ValueError(f"An exact whole {kind} reference is required")


class IndustryShock(Contract):
    system: Literal["csrc"] = "csrc"
    code: Name
    shock: ShockRatio


class ListingShock(Contract):
    listing_ref: ObjectRef
    shock: ShockRatio

    @model_validator(mode="after")
    def identity(self) -> Self:
        exact(self.listing_ref, "listing", immutable=True)
        return self


class StressShock(Contract):
    name: Name
    global_shock: ShockRatio | None = Field(default=None, alias="global")
    industries: Annotated[tuple[IndustryShock, ...], Field(max_length=MAX_SECURITIES)] = ()
    listings: Annotated[tuple[ListingShock, ...], Field(max_length=MAX_SECURITIES)] = ()
    provenance: Literal["user_scenario"] = "user_scenario"

    @model_validator(mode="after")
    def targets(self) -> Self:
        if len({(item.system, item.code) for item in self.industries}) != len(self.industries):
            raise ValueError("Duplicate industry targets within one shock")
        if len({item.listing_ref for item in self.listings}) != len(self.listings):
            raise ValueError("Duplicate listing targets within one shock")
        return self


class CalculationPath(Contract):
    calculation_ref: ObjectRef
    scenario_index: Annotated[int, Field(strict=True, ge=0, le=20)]

    @model_validator(mode="after")
    def identity(self) -> Self:
        exact(self.calculation_ref, "calculation", immutable=True)
        return self


class PlanOptionPath(CalculationPath):
    plan_ref: ObjectRef
    option_id: UUID
    scenario_index: Annotated[int, Field(strict=True, ge=1, le=20)]

    @model_validator(mode="after")
    def plan_identity(self) -> Self:
        exact(self.plan_ref, "portfolio_plan", immutable=True)
        return self


AlternativePath = PlanOptionPath | CalculationPath


class StressThresholds(Contract):
    maximum_loss_cny: Nonnegative | None = None
    maximum_loss_ratio: Fraction | None = None


class StressStudyRequest(Contract):
    operation_id: UUID
    account_id: UUID
    expected_account_revision: Revision
    snapshot_ref: ObjectRef
    valuation_ref: ObjectRef
    basis_mode: Literal["current", "historical"] = "current"
    alternatives: Annotated[tuple[AlternativePath, ...], Field(max_length=MAX_PATHS - 1)] = ()
    shocks: Annotated[tuple[StressShock, ...], Field(min_length=1, max_length=MAX_SHOCKS)]
    thresholds: StressThresholds = StressThresholds()
    industry_system: Literal["csrc"] = "csrc"

    @model_validator(mode="after")
    def identities_and_capacity(self) -> Self:
        exact(self.snapshot_ref, "portfolio_snapshot")
        exact(self.valuation_ref, "portfolio_valuation", immutable=True)
        if len({shock.name for shock in self.shocks}) != len(self.shocks):
            raise ValueError("Shock names must be unique")
        keys = {(item.calculation_ref, item.scenario_index) for item in self.alternatives}
        if len(keys) != len(self.alternatives):
            raise ValueError("Alternative paths must reference distinct saved scenarios")
        listings = {item.listing_ref for shock in self.shocks for item in shock.listings}
        if len(listings) > MAX_SECURITIES:
            raise ValueError("At most 50 distinct listing targets per study")
        return self


class StressRevisionRequest(StressStudyRequest):
    expected_revision: Revision


class StressCancelRequest(Contract):
    operation_id: UUID
    expected_control_revision: Revision


class StressRef(Contract):
    """A domain address until PT-005 registers the formal ObjectRef kind."""

    kind: Literal["portfolio_stress"] = "portfolio_stress"
    id: UUID
    revision: Revision


class StressCreated(Contract):
    study_ref: StressRef
    run_id: RunId
    result_id: UUID
    input_hash: Hash


class VersionedInput(Contract):
    ref: ObjectRef
    payload_hash: Hash


class FrozenPrice(Contract):
    price_ref: ObjectRef
    listing_ref: ObjectRef
    price: Positive
    trade_date: date
    captured_at: datetime
    provider: Name
    adjust: Literal["none"] = "none"
    trade_status: str | None = None

    @model_validator(mode="after")
    def identities(self) -> Self:
        exact(self.price_ref, "market_snapshot")
        exact(self.listing_ref, "listing", immutable=True)
        return self


class FrozenIndustry(Contract):
    """A copied source row, not an invented immutable ObjectRef."""

    row_id: RunId
    listing_ref: ObjectRef
    system: Literal["csrc"] = "csrc"
    code: Name
    source: Name
    valid_from: date
    valid_to: date | None
    captured_at: datetime

    @model_validator(mode="after")
    def identity_and_interval(self) -> Self:
        exact(self.listing_ref, "listing", immutable=True)
        if self.valid_to is not None and self.valid_from >= self.valid_to:
            raise ValueError("Industry validity uses a nonempty half-open interval")
        return self


class FrozenHolding(Contract):
    listing_ref: ObjectRef
    quantity: Nonnegative | None
    price: Positive | None
    price_ref: ObjectRef | None
    quantity_unknown_reason: str | None = None
    price_unknown_reason: str | None = None

    @model_validator(mode="after")
    def identities_and_unknowns(self) -> Self:
        exact(self.listing_ref, "listing", immutable=True)
        if (self.quantity is None) != bool(self.quantity_unknown_reason):
            raise ValueError("Unknown quantity requires its reason; known quantity has none")
        if self.price_ref is not None:
            exact(self.price_ref, "market_snapshot")
        if self.price is not None and (self.price_ref is None or self.price_unknown_reason):
            raise ValueError("A usable price requires its exact source and no unknown reason")
        if self.price is None and self.quantity != 0 and not self.price_unknown_reason:
            raise ValueError("Missing nonzero-holding price requires a reason")
        return self


class FrozenPath(Contract):
    name: Name
    source: Literal["actual", "calculation", "plan_option"]
    alternative: AlternativePath | None = None
    cash: InputDecimal | None
    cash_unknown_reason: str | None = None
    coverage: Literal["complete", "partial"]
    holdings: Annotated[tuple[FrozenHolding, ...], Field(max_length=MAX_SECURITIES)]
    fees_already_in_cash: Literal[True] = True
    # None is retained only for a legacy actual path using the common copy.
    # An explicit empty tuple is unknown membership and must never inherit a
    # different path's classification. New freezes populate every path.
    industries: Annotated[tuple[FrozenIndustry, ...], Field(max_length=MAX_SECURITIES)] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.cash is None) != bool(self.cash_unknown_reason):
            raise ValueError("Unknown cash requires its reason; known cash has none")
        if len({holding.listing_ref for holding in self.holdings}) != len(self.holdings):
            raise ValueError("Duplicate path holding")
        if (self.source == "actual") != (self.alternative is None):
            raise ValueError("Only actual baseline has no saved alternative reference")
        if self.source == "plan_option" and not isinstance(self.alternative, PlanOptionPath):
            raise ValueError("Plan option path requires its exact plan and option")
        if self.source == "calculation" and isinstance(self.alternative, PlanOptionPath):
            raise ValueError("Calculation path cannot conceal a plan option")
        if self.industries is None and self.source != "actual":
            raise ValueError("Saved alternatives require an explicit per-path industry tuple")
        if self.industries is not None:
            if len({item.listing_ref for item in self.industries}) != len(self.industries):
                raise ValueError("Each path must have unambiguous industry sources")
            if any(item.listing_ref not in {holding.listing_ref for holding in self.holdings}
                   for item in self.industries):
                raise ValueError("Path industry must refer to a holding in that path")
        if self.coverage == "complete" and (
            self.cash is None
            or any(
                holding.quantity is None or holding.quantity != 0 and holding.price is None
                for holding in self.holdings
            )
        ):
            raise ValueError(
                "Complete asset coverage requires known cash, quantities and nonzero-holding prices"
            )
        return self


class FrozenStressInputs(Contract):
    """Server-created bounded inputs; never an HTTP request or execution grant."""

    account_id: UUID
    snapshot_ref: ObjectRef
    valuation_ref: ObjectRef
    mandate_ref: ObjectRef | None
    valuation_date: date
    knowledge_cutoff: datetime
    paths: Annotated[tuple[FrozenPath, ...], Field(min_length=1, max_length=MAX_PATHS)]
    prices: Annotated[tuple[FrozenPrice, ...], Field(max_length=MAX_SECURITIES)]
    industries: Annotated[tuple[FrozenIndustry, ...], Field(max_length=MAX_SECURITIES)]
    versions: Annotated[tuple[VersionedInput, ...], Field(min_length=2, max_length=100)]
    method_version: Literal["portfolio-stress-v1"] = STRESS_METHOD_VERSION
    numeric_rules_version: Literal["decimal-40-18-dynamic128-ratio40-half-even-cent-v1"] = (
        NUMERIC_RULES_VERSION
    )

    @model_validator(mode="after")
    def bounds_and_times(self) -> Self:
        exact(self.snapshot_ref, "portfolio_snapshot")
        exact(self.valuation_ref, "portfolio_valuation", immutable=True)
        if self.mandate_ref is not None:
            exact(self.mandate_ref, "mandate")
        if self.paths[0].source != "actual" or any(
            path.source == "actual" for path in self.paths[1:]
        ):
            raise ValueError("Exactly one real baseline must be the first path")
        listings = {h.listing_ref for path in self.paths for h in path.holdings}
        if len(listings) > MAX_SECURITIES or len({path.name for path in self.paths}) != len(
            self.paths
        ):
            raise ValueError("At most 50 securities and unique path names")
        if len({price.listing_ref for price in self.prices}) != len(self.prices):
            raise ValueError("A listing cannot have ambiguous frozen prices")
        for path in self.paths:
            for holding in path.holdings:
                if holding.price is not None and not any(
                    price.listing_ref == holding.listing_ref
                    and price.price_ref == holding.price_ref
                    and price.price == holding.price
                    for price in self.prices
                ):
                    raise ValueError(
                        "Every usable holding price must match its exact frozen source copy"
                    )
        if len({industry.listing_ref for industry in self.industries}) != len(self.industries):
            raise ValueError("Ambiguous industry sources must remain unknown")
        for path in self.paths:
            if path.industries is None and path.source != "actual":
                raise ValueError("Saved alternatives cannot inherit the common industry map")
            for industry in path.industries or ():
                if (industry.captured_at > self.knowledge_cutoff
                        or industry.valid_from > self.valuation_date
                        or industry.valid_to is not None and industry.valid_to <= self.valuation_date):
                    raise ValueError("Per-path industry is outside its knowledge/half-open boundary")
        versions = {version.ref for version in self.versions}
        required = {self.snapshot_ref, self.valuation_ref}
        if self.mandate_ref is not None:
            required.add(self.mandate_ref)
        for path in self.paths:
            if path.alternative is not None:
                required.add(path.alternative.calculation_ref)
                if isinstance(path.alternative, PlanOptionPath):
                    required.add(path.alternative.plan_ref)
        if len(versions) != len(self.versions) or not required <= versions:
            raise ValueError("Exact baseline and alternative payload hashes are required once")
        if any(
            price.captured_at > self.knowledge_cutoff or price.trade_date > self.valuation_date
            for price in self.prices
        ):
            raise ValueError("Frozen price postdates its knowledge or valuation boundary")
        if any(
            industry.captured_at > self.knowledge_cutoff
            or industry.valid_from > self.valuation_date
            or industry.valid_to is not None
            and industry.valid_to <= self.valuation_date
            for industry in self.industries
        ):
            raise ValueError("Industry source is unavailable or outside its half-open interval")
        return self


class StressStudyView(Contract):
    """Saved domain read model, not a new CalculationRecord status taxonomy."""

    state: Literal["queued", "running", "complete", "partial", "failed", "cancelled"]
    study_ref: StressRef
    run_id: RunId
    calculation_ref: ObjectRef | None = None
    results: dict[str, Quantity] = {}
    quality: tuple[str, ...] = ()
    version_refs: Annotated[tuple[ObjectRef, ...], Field(min_length=1, max_length=100)]
    current_basis_changed: StrictBool
    reason_code: str | None = None

    @model_validator(mode="after")
    def result_state(self) -> Self:
        if self.calculation_ref is not None:
            exact(self.calculation_ref, "calculation", immutable=True)
        if self.state in {"complete", "partial"} and self.calculation_ref is None:
            raise ValueError("Completed arithmetic requires its exact saved calculation")
        if self.state in {"queued", "running", "failed", "cancelled"} and self.results:
            raise ValueError("Unfinished or failed studies cannot contain successful results")
        if self.state == "partial" and not self.quality:
            raise ValueError("Partial result requires explicit quality reasons")
        if "cash_missing" in self.quality and any(
            self.results[key].value is not None
            for key in ("total_loss", "total_assets", "loss_ratio")
            if key in self.results
        ):
            raise ValueError("Missing cash cannot produce complete totals or loss ratios")
        if self.state == "complete" and (
            "cash_missing" in self.quality
            or any(
                self.results[key].value is None
                for key in (
                    "total_loss",
                    "total_assets",
                    "total_assets_before",
                    "total_assets_after",
                    "nav_before",
                    "nav_after",
                )
                if key in self.results
            )
        ):
            raise ValueError(
                "Complete result cannot retain unknown full-account amounts or missing cash"
            )
        return self

"""Frozen daily-forward simulation contracts, isolated from confirmed portfolios.

Parsing is not authorization. Services bind scope, compare revisions, enforce
calendar/security rules and persist ``model_dump(mode='json')``. Decimal inputs
never accept floats; JSON uses exact strings. Containers are immutable tuples.
"""

from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from investment_agent.investor.common import Contract, DecimalValue, Text

Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^\S+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Money = Annotated[DecimalValue, Field(max_digits=40, decimal_places=18)]
NonnegativeMoney = Annotated[Money, Field(ge=0)]
PositiveMoney = Annotated[Money, Field(gt=0)]
Quantity = Annotated[Money, Field(gt=0)]
Ratio = Annotated[Money, Field(ge=0, le=1)]
ExperimentStatus = Literal["draft", "running", "paused", "completed", "blocked", "cancelled"]
RoundStatus = Literal[
    "waiting_data",
    "collecting_decisions",
    "locked",
    "awaiting_execution_data",
    "settled",
    "skipped",
    "blocked",
    "cancelled",
]
DecisionStatus = Literal[
    "draft",
    "submitted",
    "locked",
    "invalid",
    "timeout",
    "failed",
    "unknown",
    "cancelled",
]
OrderStatus = Literal["locked", "filled", "partially_filled", "rejected", "expired", "cancelled"]


class ConfigEntry(Contract):
    key: Identifier
    value: str

    @model_validator(mode="after")
    def no_credentials(self) -> Self:
        normalized = "".join(character for character in self.key.casefold() if character.isalnum())
        if any(
            word in normalized
            for word in (
                "apikey",
                "secret",
                "password",
                "credential",
                "authorization",
                "accesstoken",
                "refreshtoken",
                "bearertoken",
                "privatekey",
            )
        ):
            raise ValueError("credentials must not enter frozen simulation configuration")
        return self


class BudgetConfig(Contract):
    currency: Literal["USD"] = "USD"
    input_price_per_million: NonnegativeMoney | None = None
    output_price_per_million: NonnegativeMoney | None = None
    conservative_call_reservation: PositiveMoney = Decimal("1")
    round_cost_limit: PositiveMoney = Decimal("3")
    season_cost_limit: PositiveMoney = Decimal("100")
    max_calls_per_round: PositiveInt = 8
    format_repair_attempts: Annotated[int, Field(strict=True, ge=0, le=5)] = 1
    timeout_seconds: PositiveInt = 300
    unknown_call_policy: Literal["record_and_skip"] = "record_and_skip"

    def spending_envelope(self) -> tuple[object, ...]:
        """Fair limits exclude model-specific tariff metadata."""
        return (
            self.currency,
            self.round_cost_limit,
            self.season_cost_limit,
            self.max_calls_per_round,
            self.format_repair_attempts,
            self.timeout_seconds,
            self.unknown_call_policy,
        )

    @model_validator(mode="after")
    def season_covers_round(self) -> Self:
        if (self.input_price_per_million is None) != (self.output_price_per_million is None):
            raise ValueError("input and output prices must be configured together")
        if self.conservative_call_reservation > self.round_cost_limit:
            raise ValueError("call reservation exceeds round budget")
        if self.season_cost_limit < self.round_cost_limit:
            raise ValueError("season cost limit must cover a round")
        return self


class FeeSchedule(Contract):
    version: Identifier
    commission_rate: Ratio
    minimum_commission: NonnegativeMoney
    sell_tax_rate: Ratio
    transfer_rate: Ratio = Decimal("0")


class SlippageConfig(Contract):
    version: Identifier
    adverse_rate: Ratio = Decimal("0")


class RulePackage(Contract):
    version: Identifier
    content_hash: Digest
    fee_schedule: FeeSchedule
    slippage: SlippageConfig
    execution_model: Literal["daily_open_v1"] = "daily_open_v1"
    currency: Literal["CNY"] = "CNY"
    security_scope: Literal["cn_mainboard_common_a"] = "cn_mainboard_common_a"
    max_position_weight: Ratio = Decimal("1")
    max_historical_turnover_ratio: Ratio = Decimal("0.01")
    allow_same_session_sale_funding: Literal[False] = False
    partial_fills: Literal[False] = False
    corporate_action_policy: Literal["supported_events_or_block"] = "supported_events_or_block"
    calendar_version: Identifier
    valuation_version: Identifier = "nav_v1"
    metrics_version: Identifier = "metrics_v1"


class ParticipantConfig(Contract):
    name: Text
    kind: Literal["agent", "cash", "equal_weight"]
    provider_ref: Identifier | None = None
    model_ref: Identifier | None = None
    model_snapshot: tuple[ConfigEntry, ...] = ()
    method_ref: Identifier | None = None
    method_hash: Digest | None = None
    prompt_version: Identifier | None = None
    prompt_hash: Digest | None = None
    tools: tuple[Identifier, ...] = ()
    budget: BudgetConfig = BudgetConfig()
    memory_policy: Literal["none", "matured_own_reviews"] = "none"
    baseline_version: Identifier | None = None

    @model_validator(mode="after")
    def valid_participant(self) -> Self:
        if len({item.key for item in self.model_snapshot}) != len(self.model_snapshot):
            raise ValueError("duplicate model configuration keys")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("duplicate tools")
        agent_fields = (
            self.provider_ref,
            self.model_ref,
            self.method_ref,
            self.method_hash,
            self.prompt_version,
            self.prompt_hash,
        )
        if self.kind == "agent":
            if any(value is None for value in agent_fields):
                raise ValueError("agents require frozen model, method and prompt references")
            if self.baseline_version is not None:
                raise ValueError("agents cannot carry a baseline version")
        elif (
            any(value is not None for value in agent_fields)
            or self.tools
            or self.model_snapshot
            or self.memory_policy != "none"
        ):
            raise ValueError("deterministic baselines cannot carry agent configuration")
        elif self.baseline_version is None:
            raise ValueError("deterministic baselines require a version")
        return self


class ExperimentConfig(Contract):
    name: Text
    mode: Literal["model_comparison", "method_comparison", "combined"] = "combined"
    run_mode: Literal["daily_forward"] = "daily_forward"
    universe: Annotated[tuple[PositiveInt, ...], Field(min_length=1)]
    initial_cash: PositiveMoney = Decimal("1000000")
    start_date: date
    end_date: date
    rebalance_frequency: Literal["daily", "weekly"] = "weekly"
    rules: RulePackage
    participants: Annotated[tuple[ParticipantConfig, ...], Field(min_length=2)]

    @model_validator(mode="after")
    def valid_experiment(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("experiment end precedes start")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("duplicate universe listing")
        if len({p.name for p in self.participants}) != len(self.participants):
            raise ValueError("participant names must be unique")
        kinds = [p.kind for p in self.participants]
        if kinds.count("cash") != 1 or kinds.count("equal_weight") != 1:
            raise ValueError("exactly one cash and one equal_weight baseline required")
        agents = [p for p in self.participants if p.kind == "agent"]
        if self.mode == "model_comparison" and agents:
            frozen = {
                (
                    p.method_ref,
                    p.method_hash,
                    p.prompt_version,
                    p.prompt_hash,
                    p.tools,
                    p.budget.spending_envelope(),
                    p.memory_policy,
                )
                for p in agents
            }
            if len(frozen) != 1:
                raise ValueError(
                    "model comparison must freeze method, prompt, tools, budget and memory"
                )
        if (
            self.mode == "method_comparison"
            and agents
            and len({
                (
                    p.provider_ref,
                    p.model_ref,
                    tuple(entry for entry in p.model_snapshot
                          if entry.key not in {"method_text", "prompt_text"}),
                    p.budget,
                )
                for p in agents
            })
            != 1
        ):
            raise ValueError("method comparison must freeze model and budget")
        return self


class Experiment(Contract):
    id: Identifier
    revision: Revision = 0
    status: ExperimentStatus = "draft"
    config: ExperimentConfig
    config_hash: Digest
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None


class Participant(Contract):
    id: Identifier
    experiment_id: Identifier
    account_id: Identifier
    version: PositiveInt = 1
    config: ParticipantConfig
    config_hash: Digest


class AccountScope(Contract):
    experiment_id: Identifier
    participant_id: Identifier
    account_id: Identifier


class SimulationAccount(AccountScope):
    revision: Revision = 0
    initial_cash: PositiveMoney
    state: "AccountState"


class EvidenceRef(Contract):
    id: Identifier
    content_hash: Digest
    published_at: datetime
    acquired_at: datetime
    available_at: datetime
    source: Text

    @model_validator(mode="after")
    def conservative_availability(self) -> Self:
        if self.available_at < max(self.published_at, self.acquired_at):
            raise ValueError("availability cannot precede publication or acquisition")
        return self


class DataManifest(Contract):
    version: Identifier
    content_hash: Digest
    cutoff: datetime
    universe: Annotated[tuple[PositiveInt, ...], Field(min_length=1)]
    evidence: tuple[EvidenceRef, ...] = ()
    artifact_path: Text
    complete: bool
    missing_reasons: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def point_in_time(self) -> Self:
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("duplicate manifest listing")
        if len({ref.id for ref in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate evidence reference")
        if any(ref.available_at > self.cutoff for ref in self.evidence):
            raise ValueError("evidence is later than common cutoff")
        if self.complete == bool(self.missing_reasons):
            raise ValueError("incomplete manifests require missing reasons")
        return self


class SimulationRound(Contract):
    id: Identifier
    experiment_id: Identifier
    revision: Revision = 0
    observation_date: date
    cutoff: datetime
    decision_deadline: datetime
    execution_date: date
    execution_at: datetime
    status: RoundStatus = "waiting_data"
    manifest: DataManifest

    @model_validator(mode="after")
    def valid_clock(self) -> Self:
        if not self.cutoff < self.decision_deadline < self.execution_at:
            raise ValueError("cutoff < decision deadline < execution event required")
        if self.execution_date <= self.observation_date:
            raise ValueError("execution must follow observation date")
        if self.manifest.cutoff != self.cutoff:
            raise ValueError("manifest cutoff must equal round cutoff")
        if self.cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date() < self.observation_date:
            raise ValueError("cutoff precedes observation day")
        if self.execution_at.astimezone(ZoneInfo("Asia/Shanghai")).date() != self.execution_date:
            raise ValueError("execution event must fall on execution date in exchange timezone")
        if (
            self.status not in {"waiting_data", "blocked", "skipped", "cancelled"}
            and not self.manifest.complete
        ):
            raise ValueError("incomplete common data blocks all decisions")
        return self


class OrderIntent(Contract):
    listing_id: PositiveInt
    side: Literal["buy", "sell"]
    quantity: Quantity
    protection_price: PositiveMoney


class Decision(AccountScope):
    id: Identifier
    round_id: Identifier
    revision: Revision = 0
    account_revision: Revision
    status: DecisionStatus
    action: Literal["hold", "trade", "no_submission"]
    orders: tuple[OrderIntent, ...] = ()
    rationale: Text
    counter_evidence: tuple[Text, ...] = ()
    evidence_refs: tuple[Identifier, ...] = ()
    expected_holding_days: PositiveInt | None = None
    review_conditions: tuple[Text, ...] = ()
    submitted_at: datetime | None = None
    failure_code: Identifier | None = None
    idempotency_key: Identifier

    @model_validator(mode="after")
    def explicit_action(self) -> Self:
        if (self.action == "trade") != bool(self.orders):
            raise ValueError("only trade actions contain orders; trade requires orders")
        if len({order.listing_id for order in self.orders}) != len(self.orders):
            raise ValueError("only one final direction per listing per round")
        failure_status = self.status in {"invalid", "timeout", "failed", "unknown", "cancelled"}
        if failure_status != (self.failure_code is not None):
            raise ValueError("failure statuses require an explicit failure code")
        if self.action == "no_submission" and not failure_status:
            raise ValueError("no_submission must retain a failure status")
        if self.status in {"submitted", "locked"} and self.submitted_at is None:
            raise ValueError("submitted decisions require submission time")
        return self


class SimulationOrder(AccountScope):
    id: Identifier
    round_id: Identifier
    decision_id: Identifier
    sequence: PositiveInt
    account_revision: Revision
    listing_id: PositiveInt
    side: Literal["buy", "sell"]
    quantity: Quantity
    protection_price: PositiveMoney
    execution_date: date
    valid_from: datetime
    valid_until: datetime
    reserved_cash: NonnegativeMoney = Decimal("0")
    reserved_quantity: NonnegativeMoney = Decimal("0")
    status: OrderStatus = "locked"
    rule_version: Identifier
    idempotency_key: Identifier
    rejection_reason: Text | None = None

    @model_validator(mode="after")
    def protected_reservation(self) -> Self:
        if self.valid_until < self.valid_from:
            raise ValueError("order validity ends before it starts")
        exchange = ZoneInfo("Asia/Shanghai")
        if any(
            instant.astimezone(exchange).date() != self.execution_date
            for instant in (self.valid_from, self.valid_until)
        ):
            raise ValueError("daily order validity must fall on its execution date")
        if self.side == "buy":
            # Each operand has up to 40 digits; ambient Decimal precision is
            # commonly only 28 and must not silently forgive an under-reserve.
            with localcontext() as context:
                context.prec = 80
                notional = self.quantity * self.protection_price
            if self.reserved_cash < notional or self.reserved_quantity:
                raise ValueError("buy must reserve protected notional and no shares")
        elif self.reserved_quantity != self.quantity or self.reserved_cash:
            raise ValueError("sell must reserve exact shares and no cash")
        if self.status == "rejected" and self.rejection_reason is None:
            raise ValueError("rejected order requires reason")
        return self


class SimulationFill(AccountScope):
    id: Identifier
    round_id: Identifier
    order_id: Identifier
    execution_key: Identifier
    sequence: PositiveInt = 1
    listing_id: PositiveInt
    side: Literal["buy", "sell"]
    quantity: Quantity
    price: PositiveMoney
    fee: NonnegativeMoney
    economic_at: datetime
    confirmed_at: datetime
    market_data_ref: Identifier
    rule_version: Identifier

    @model_validator(mode="after")
    def dual_clock(self) -> Self:
        if self.confirmed_at < self.economic_at:
            raise ValueError("fill confirmation cannot precede economic event")
        return self


class PositionLot(Contract):
    id: Identifier
    listing_id: PositiveInt
    quantity: Quantity
    available_on: date
    unit_cost: NonnegativeMoney
    acquired_on: date

    @model_validator(mode="after")
    def sellability(self) -> Self:
        if self.available_on < self.acquired_on:
            raise ValueError("sellability cannot precede acquisition")
        return self


class AccountState(Contract):
    cash: NonnegativeMoney
    reserved_cash: NonnegativeMoney = Decimal("0")
    receivables: NonnegativeMoney = Decimal("0")
    payables: NonnegativeMoney = Decimal("0")
    lots: tuple[PositionLot, ...] = ()
    reserved_positions: tuple[OrderIntent, ...] = ()

    @model_validator(mode="after")
    def reservation_bounds(self) -> Self:
        if self.reserved_cash > self.cash:
            raise ValueError("reserved cash exceeds cash")
        if len({lot.id for lot in self.lots}) != len(self.lots):
            raise ValueError("duplicate lot")
        if any(item.side != "sell" for item in self.reserved_positions):
            raise ValueError("position reservations must be sells")
        for listing_id in {item.listing_id for item in self.reserved_positions}:
            # Summation also needs headroom for the number of rows.
            with localcontext() as context:
                context.prec = 40 + len(str(len(self.lots) + len(self.reserved_positions))) + 1
                reserved = sum(
                    item.quantity
                    for item in self.reserved_positions
                    if item.listing_id == listing_id
                )
                held = sum(lot.quantity for lot in self.lots if lot.listing_id == listing_id)
            if reserved > held:
                raise ValueError("reserved quantity exceeds holdings")
        return self


SimulationAccount.model_rebuild()


class LedgerEvent(AccountScope):
    id: Identifier
    sequence: PositiveInt
    account_revision: PositiveInt
    idempotency_key: Identifier
    kind: Literal[
        "initial_cash",
        "fill",
        "fee",
        "reserve",
        "release",
        "corporate_action",
        "settlement",
        "correction",
    ]
    economic_at: datetime
    confirmed_at: datetime
    cash_delta: Money = Decimal("0")
    quantity_delta: Money = Decimal("0")
    receivable_delta: Money = Decimal("0")
    payable_delta: Money = Decimal("0")
    listing_id: PositiveInt | None = None
    fill_id: Identifier | None = None
    reverses_id: Identifier | None = None
    source_ref: Identifier
    state_after: AccountState

    @model_validator(mode="after")
    def valid_event(self) -> Self:
        if self.confirmed_at < self.economic_at:
            raise ValueError("event confirmation cannot precede economic event")
        if self.quantity_delta and self.listing_id is None:
            raise ValueError("quantity delta requires listing")
        if self.kind == "initial_cash" and self.cash_delta <= 0:
            raise ValueError("initial funding must be positive")
        if self.kind == "fill" and self.fill_id is None:
            raise ValueError("fill event requires fill reference")
        if self.kind == "correction" and self.reverses_id is None:
            raise ValueError("correction must reference original event")
        return self


class NavSnapshot(AccountScope):
    id: Identifier
    account_revision: Revision
    valuation_date: date
    version: PositiveInt = 1
    cash: NonnegativeMoney
    receivables: NonnegativeMoney = Decimal("0")
    position_value: NonnegativeMoney | None
    payables: NonnegativeMoney = Decimal("0")
    equity: Money | None
    quality: Literal["complete", "stale", "incomplete"]
    quality_reasons: tuple[Text, ...] = ()
    valuation_version: Identifier
    metrics_version: Identifier
    input_hash: Digest
    price_refs: tuple[Identifier, ...] = ()
    cumulative_return: Money | None = None
    max_drawdown: Ratio | None = None
    created_at: datetime

    @model_validator(mode="after")
    def equity_reconciles(self) -> Self:
        with localcontext() as context:
            context.prec = 42
            expected_equity = (
                None
                if self.position_value is None
                else self.cash + self.receivables + self.position_value - self.payables
            )
        if self.quality == "incomplete":
            if (
                self.equity is not None
                or self.cumulative_return is not None
                or self.max_drawdown is not None
            ):
                raise ValueError("incomplete valuation cannot publish formal equity or metrics")
        elif self.position_value is None or self.equity != expected_equity:
            raise ValueError("equity must reconcile cash, receivables, positions and payables")
        if (self.quality != "complete") != bool(self.quality_reasons):
            raise ValueError("non-complete valuations require quality reasons")
        return self


class SimulationReview(AccountScope):
    id: Identifier
    decision_id: Identifier
    observation_start: date
    observation_end: date
    available_at: datetime
    status: Literal["observing", "matured", "failed"]
    evidence_refs: tuple[Identifier, ...] = ()
    nav_refs: tuple[Identifier, ...] = ()
    findings: Text
    improvement_proposals: tuple[Text, ...] = ()
    memory_eligible: bool = False
    version: PositiveInt = 1

    @model_validator(mode="after")
    def maturity(self) -> Self:
        if self.observation_end < self.observation_start:
            raise ValueError("review window ends before it starts")
        if self.available_at.astimezone(ZoneInfo("Asia/Shanghai")).date() < self.observation_end:
            raise ValueError("review cannot become available before observation end")
        if self.memory_eligible and self.status != "matured":
            raise ValueError("only matured reviews may enter memory")
        return self


class CostRecord(AccountScope):
    id: Identifier
    round_id: Identifier
    call_key: Identifier
    model_ref: Identifier
    returned_model: Identifier | None = None
    status: Literal["reserved", "settled", "failed", "unknown"]
    amount: NonnegativeMoney
    estimated: bool = False
    currency: Literal["USD", "CNY"]
    input_tokens: Annotated[int, Field(strict=True, ge=0)] = 0
    output_tokens: Annotated[int, Field(strict=True, ge=0)] = 0
    calls: PositiveInt = 1
    occurred_at: datetime

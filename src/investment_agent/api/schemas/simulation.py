"""Human-facing simulation inputs. Internal hashes and identities are server-owned."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_agent.investor.common import Contract
from investment_agent.simulation.contracts import (
    BudgetConfig,
    EvidenceRef,
    FeeSchedule,
    NonnegativeMoney,
    PositiveMoney,
    Ratio,
    RulePackage,
    SimulationAccount,
)
from investment_agent.simulation.data import ExecutionEvidenceInput

HttpID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")]
Name = Annotated[str, Field(min_length=1, max_length=100)]


class AgentCreate(Contract):
    name: Name
    provider_ref: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")]
    model_ref: Annotated[str, Field(min_length=1, max_length=201, pattern=r"^[A-Za-z0-9._/-]+$")]
    budget: BudgetConfig = BudgetConfig()


class ExperimentalFees(Contract):
    commission_rate: Ratio = Decimal("0.0003")
    minimum_commission: Annotated[NonnegativeMoney, Field(le=10000)] = Decimal("5")
    sell_tax_rate: Ratio = Decimal("0.0005")
    transfer_rate: Ratio = Decimal("0")

    def schedule(self) -> FeeSchedule:
        return FeeSchedule(version="experimental-fees-v1", **self.model_dump())


class CreateSimulation(Contract):
    name: Name
    universe: Annotated[
        tuple[Annotated[int, Field(strict=True, gt=0)], ...], Field(min_length=1, max_length=100)
    ]
    start_date: date
    end_date: date
    initial_cash: Annotated[PositiveMoney, Field(le=Decimal("1000000000000"))] = Decimal("1000000")
    rebalance_frequency: Literal["daily", "weekly"] = "weekly"
    mode: Literal["model_comparison", "combined"] = "combined"
    agents: Annotated[tuple[AgentCreate, ...], Field(max_length=8)] = ()
    fees: ExperimentalFees = ExperimentalFees()
    slippage_rate: Annotated[Ratio, Field(le=Decimal("0.1"))] = Decimal("0.001")
    max_position_weight: Ratio = Decimal("1")
    max_historical_turnover_ratio: Annotated[Ratio, Field(le=Decimal("0.1"))] = Decimal("0.01")
    idempotency_key: HttpID | None = None

    @model_validator(mode="after")
    def bounded_season(self) -> Self:
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 730:
            raise ValueError("season must be ordered and at most 730 days")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("duplicate listing IDs")
        if len({a.name for a in self.agents}) != len(self.agents):
            raise ValueError("duplicate participant names")
        return self


class RevisionWrite(Contract):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    idempotency_key: HttpID | None = None


class EvidenceImport(Contract):
    evidence: ExecutionEvidenceInput
    source_ref: Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")]
    idempotency_key: HttpID | None = None


class CorporateActionData(Contract):
    id: HttpID
    listing_id: Annotated[int, Field(strict=True, gt=0)]
    kind: Literal["cash_entitlement", "cash_payment", "split", "unsupported"]
    economic_at: datetime
    source_ref: HttpID
    verified: bool
    amount: NonnegativeMoney = Decimal("0")
    split_ratio: PositiveMoney | None = None
    entitlement_ref: HttpID | None = None


class CorporateActionImport(RevisionWrite):
    action: CorporateActionData
    entitlement_date: date | None = None
    cash_per_share: PositiveMoney | None = None


class PublicDTO(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ParticipantConfigOut(PublicDTO):
    name: str
    kind: str
    provider_ref: str | None = None
    model_ref: str | None = None
    method_ref: str | None = None
    method_hash: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    budget: BudgetConfig
    memory_policy: str
    baseline_version: str | None = None


class ExperimentConfigOut(PublicDTO):
    name: str
    mode: str
    run_mode: str
    universe: tuple[int, ...]
    initial_cash: PositiveMoney
    start_date: date
    end_date: date
    rebalance_frequency: str
    rules: RulePackage
    participants: tuple[ParticipantConfigOut, ...]


class ExperimentOut(PublicDTO):
    id: str
    revision: int
    status: str
    config: ExperimentConfigOut
    config_hash: str
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None


class ParticipantOut(PublicDTO):
    id: str
    experiment_id: str
    account_id: str
    version: int
    config: ParticipantConfigOut
    config_hash: str


class ManifestOut(PublicDTO):
    version: str
    content_hash: str
    cutoff: datetime
    universe: tuple[int, ...]
    evidence: tuple[EvidenceRef, ...]
    complete: bool
    missing_reasons: tuple[str, ...]


class RoundOut(PublicDTO):
    id: str
    experiment_id: str
    revision: int
    observation_date: date
    cutoff: datetime
    decision_deadline: datetime
    execution_date: date
    execution_at: datetime
    status: str
    manifest: ManifestOut


class DetailOut(PublicDTO):
    experiment: ExperimentOut
    participants: tuple[ParticipantOut, ...]
    accounts: tuple[SimulationAccount, ...]
    rounds: tuple[RoundOut, ...]
    leaderboard: dict[str, object]
    data_gaps: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class QueuedOut(PublicDTO):
    id: str
    run_id: int
    created: bool
    status: Literal["queued"]

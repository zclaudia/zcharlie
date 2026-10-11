"""Model business inputs are references and scenarios; qualifications are server dependencies."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import CalculationAssumption, EvidenceRecord
from investment_agent.investor.common import Contract, ObjectRef, OwnerId


class ReverseDCFRequest(Contract):
    as_of: datetime
    equity_value_ref: ObjectRef
    annual_fcfe_refs: tuple[ObjectRef, ...] = Field(max_length=3)
    main_rate: CalculationAssumption | None = None
    sensitivity_rates: tuple[CalculationAssumption, ...] = Field(default=(), max_length=6)
    terminal_growth: CalculationAssumption | None = None


class PercentileRequest(Contract):
    ticker: str
    as_of: datetime
    metric: Literal["pe", "pb"]


@dataclass(frozen=True)
class ValuationQualification:
    """AG11 contract: deterministic source mapping must attest each exact evidence version.

    No numeric values live here. Values/periods/identity are read from EvidenceService.
    Resolver must inspect all available newer materials and mapping/accounting diagnostics;
    older favorable inputs cannot bypass the latest_complete_year or invalidation reasons.
    Missing qualification fails closed. This type is never a model tool parameter.
    """

    equity_value_ref: ObjectRef
    annual_fcfe_refs: tuple[ObjectRef, ...]
    derivation_version: str
    accounting_basis: str
    latest_complete_year: int
    ordinary_equity: bool
    non_financial: bool
    same_currency: bool
    consistent_scope: bool
    known_net_borrowing: bool
    known_cash_interest: bool
    interest_classification_checked: bool
    no_overlapping_financing: bool
    no_unhandled_material_changes: bool
    non_operating_assets_consistent: bool
    report_overdue: bool = False
    cashflow_superseded: bool = False
    reasons: tuple[str, ...] = ()


class QualificationResolver(Protocol):
    async def resolve(self, session: AsyncSession, owner: OwnerId,
                      request: ReverseDCFRequest,
                      evidence: tuple[EvidenceRecord, ...]) -> ValuationQualification | None: ...


class UnavailableQualification:
    """Production default until AG11 validates and enables a real FCFE mapping."""

    async def resolve(self, session: AsyncSession, owner: OwnerId,
                      request: ReverseDCFRequest,
                      evidence: tuple[EvidenceRecord, ...]) -> None:
        return None

"""Read saved AG22-v2 outputs without rerunning trades or charging fees again."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.calculation_contracts import PortfolioScenarioInput
from investment_agent.portfolio.plan_contracts import PortfolioPlan
from investment_agent.portfolio.service import digest
from investment_agent.portfolio.stress_canonical import canonical_hash, input_decimal
from investment_agent.portfolio.stress_contracts import (
    MAX_SECURITIES,
    AlternativePath,
    PlanOptionPath,
    StressStudyRequest,
    VersionedInput,
    exact,
)
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.portfolio import PortfolioAccount
from investment_agent.storage.models.portfolio_plan import PortfolioPlanArtifact

if TYPE_CHECKING:
    from investment_agent.portfolio.stress_inputs import ResolvedStressBasis


def _require(value: bool) -> None:
    if not value:
        raise InvestorError("invalid_reference", "Saved scenario does not match its exact stress basis")


def _number(value: Any) -> Decimal | None:
    return None if value is None else input_decimal(value)


@dataclass(frozen=True)
class SavedScenarioHolding:
    listing_ref: ObjectRef
    quantity: Decimal | None
    market_value: Decimal | None


@dataclass(frozen=True)
class ResolvedAlternative:
    alternative: AlternativePath
    name: str
    cash: Decimal | None
    coverage: str
    holdings: tuple[SavedScenarioHolding, ...]
    sources: tuple[dict[str, Any], ...]
    unknowns: tuple[str, ...]
    versions: tuple[VersionedInput, ...]


@dataclass(frozen=True)
class ResolvedAlternatives:
    paths: tuple[ResolvedAlternative, ...]
    versions: tuple[VersionedInput, ...]


class StressBasisAdapter:
    def __init__(self, session: AsyncSession, owner: OwnerId = "local_owner") -> None:
        self.session, self.owner = session, owner

    async def _account(self, actual: UUID, expected: UUID) -> None:
        row = await self.session.scalar(select(PortfolioAccount).where(
            PortfolioAccount.id == str(actual), PortfolioAccount.owner_id == self.owner,
        ).execution_options(populate_existing=True, autoflush=False))
        if row is None:
            raise InvestorError("not_found", "Saved scenario account is unavailable")
        _require(actual == expected)

    async def resolve_alternatives(self, request: StressStudyRequest, basis: "ResolvedStressBasis",
                                   *, now: datetime | None = None) -> ResolvedAlternatives:
        at = utc_timestamp(now or datetime.now(UTC))
        paths: list[ResolvedAlternative] = []
        versions: dict[ObjectRef, VersionedInput] = {}
        with self.session.no_autoflush:
            await self._account(request.account_id, basis.account_id)
            for index, alternative in enumerate(request.alternatives):
                row = await self.session.get(SavedCalculation,
                    (str(alternative.calculation_ref.id), alternative.calculation_ref.revision), populate_existing=True)
                if row is None or row.owner_id != self.owner:
                    raise InvestorError("not_found", "Saved scenario is unavailable")
                try:
                    record = CalculationRecord.model_validate(row.payload)
                    _require(record.id == alternative.calculation_ref.id
                             and record.revision == row.revision == alternative.calculation_ref.revision
                             and row.id == str(record.id))
                    if record.method != "portfolio_scenarios" or record.method_version != "ag22-v2":
                        raise InvestorError("unsupported_basis", "Rebuild this scenario with an explicit valuation")
                    details = record.details
                    saved = PortfolioScenarioInput.model_validate(details["request"])
                    await self._account(saved.account_id, request.account_id)
                    _require(saved.snapshot_ref == request.snapshot_ref == basis.snapshot.reference
                             and saved.valuation_ref == request.valuation_ref == basis.valuation.reference
                             and record.status == "ok" and record.created_at <= at
                             and record.as_of == basis.valuation.knowledge_cutoff)
                    _require(canonical_hash(details["snapshot"]) == canonical_hash(basis.snapshot))
                    valuation = details["valuation"]
                    _require(ObjectRef.model_validate(valuation["ref"]) == basis.valuation.reference
                             and valuation["valuation_date"] == basis.valuation.valuation_date.isoformat()
                             and utc_timestamp(datetime.fromisoformat(valuation["knowledge_cutoff"]))
                             == basis.valuation.knowledge_cutoff
                             and valuation["algorithm_version"] == basis.valuation.algorithm_version
                             and valuation["input_digest"] == basis.valuation.input_digest
                             and utc_timestamp(datetime.fromisoformat(details["price_basis_at"]))
                             == basis.valuation.knowledge_cutoff)
                    hashed = {key: details[key] for key in ("request", "snapshot", "mandate", "sources", "trusted_rules")}
                    hashed.update({key: details[key] for key in ("valuation", "purposes") if key in details})
                    _require(digest(hashed) == record.input_hash)
                    scenarios = details["scenarios"]
                    _require(isinstance(scenarios, list) and len(scenarios) == len(saved.scenarios) + 1
                             and alternative.scenario_index < len(scenarios))
                    scenario = scenarios[alternative.scenario_index]
                    expected_name = "no_action" if alternative.scenario_index == 0 else saved.scenarios[alternative.scenario_index - 1].name
                    _require(scenario["name"] == expected_name)
                    holdings = tuple(SavedScenarioHolding(ObjectRef.model_validate(item["listing_ref"]),
                        _number(item["quantity"]), _number(item["market_value"])) for item in scenario["holdings"])
                    _require(len(holdings) <= MAX_SECURITIES
                             and len({item.listing_ref for item in holdings}) == len(holdings))
                    for holding in holdings:
                        exact(holding.listing_ref, "listing", immutable=True)
                        _require(holding.quantity is None or holding.quantity >= 0)
                    sources = tuple(deepcopy(details["sources"]))
                    source_refs = [ObjectRef.model_validate(item["listing_ref"]) for item in sources]
                    _require(len(sources) <= MAX_SECURITIES and len(set(source_refs)) == len(source_refs))
                    for ref in source_refs:
                        exact(ref, "listing", immutable=True)
                    current_versions = [VersionedInput(ref=alternative.calculation_ref, payload_hash=canonical_hash(record))]
                    if isinstance(alternative, PlanOptionPath):
                        plan_row = await self.session.get(PortfolioPlanArtifact, str(alternative.plan_ref.id), populate_existing=True)
                        if plan_row is None or plan_row.owner_id != self.owner:
                            raise InvestorError("not_found", "Saved plan option is unavailable")
                        plan = PortfolioPlan.model_validate(plan_row.payload)
                        await self._account(UUID(plan_row.account_id), request.account_id)
                        options = [option for option in plan.options if option.id == alternative.option_id]
                        _require(plan_row.kind == "portfolio_plan" and str(plan.id) == plan_row.id
                                 and plan.id == alternative.plan_ref.id and plan.revision == alternative.plan_ref.revision
                                 and plan.account_id == request.account_id and plan.created_at <= at
                                 and plan.snapshot_ref == saved.snapshot_ref and plan.valuation_ref == saved.valuation_ref
                                 and plan.calculation_ref == alternative.calculation_ref
                                 and plan.mandate_ref == saved.mandate_ref
                                 and plan.price_basis_at == basis.valuation.knowledge_cutoff
                                 and len(options) == 1 and options[0].scenario_index == alternative.scenario_index)
                        current_versions.append(VersionedInput(ref=alternative.plan_ref, payload_hash=canonical_hash(plan)))
                    paths.append(ResolvedAlternative(alternative, f"alternative_{index + 1}:{expected_name}"[:100],
                        _number(scenario["cash"]), basis.snapshot.coverage, holdings, sources,
                        tuple(scenario["unknowns"]), tuple(current_versions)))
                    for version in current_versions:
                        versions[version.ref] = version
                except (ValidationError, ValueError, TypeError, KeyError, IndexError) as exc:
                    raise InvestorError("invalid_reference", "Saved scenario inputs are invalid") from exc
        return ResolvedAlternatives(tuple(paths), tuple(versions.values()))

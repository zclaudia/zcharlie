"""Immutable account-fit application service and narrow numeric resolver."""

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import localcontext
from typing import Literal
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.investor.common import Contract, ObjectRef, Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.publication_numbers import format_quantity
from investment_agent.portfolio.calculations import TradingRuleResolver
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.suitability_calculations import (
    METHOD,
    METHOD_VERSION,
    evaluate_fit,
    fit_input_hash,
    fit_inputs,
)
from investment_agent.portfolio.suitability_contracts import (
    AccountFitAssessment,
    FitRequestItem,
    FrozenAccountInputs,
    FrozenListingSource,
)
from investment_agent.portfolio.suitability_inputs import FitContext, SuitabilityInputService
from investment_agent.portfolio.valuation import trading_days_after
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)


class CurrentLimits(Contract):
    current_status: Literal["valid", "stale"]
    reasons: tuple[str, ...]
    refresh_required: bool


class AccountFitView(Contract):
    status: Literal["available", "missing"]
    payload: AccountFitAssessment | None
    current_limits: CurrentLimits | None


def source_semantic(source: FrozenListingSource) -> dict[str, object]:
    value = source.model_dump(mode="json")
    value["price"].pop("acquired_at", None)
    value["sector"].pop("acquired_at", None)
    return value


class AccountFitService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner", rules: TradingRuleResolver | None = None, calendar: Callable[[date], bool | None] | None = None) -> None:
        if owner != "local_owner":
            raise InvestorError("not_found", "账户不存在")
        self.session, self.owner = session, owner
        self.inputs = SuitabilityInputService(session, owner, rules)
        self.repository = InvestmentAssessmentRepository(session)
        self.calendar = calendar if calendar is not None else self.inputs.calendar

    async def save(self, inputs: FrozenAccountInputs, item: FitRequestItem, *, operation_id: UUID) -> AccountFitAssessment:
        reference = ObjectRef(kind="account_fit", id=uuid5(operation_id, f"fit:{item.listing_ref.id}"), revision=1)
        saved = await self.repository.get_exact("local_owner", reference, expected_kind="account_fit")
        if isinstance(saved, AccountFitAssessment):
            if saved.operation_id != operation_id or saved.request_item != item or saved.input_hash != fit_input_hash(fit_inputs(inputs, item), item):
                raise InvestorError("idempotency_conflict", "适配标识已绑定另一不可变输入")
            return saved
        assessment, calculation = evaluate_fit(inputs, item, operation_id=operation_id)
        if calculation is not None:
            prior = await self.session.get(SavedCalculation, (str(calculation.id), 1))
            if prior is not None:
                if prior.owner_id != self.owner or prior.payload != calculation.model_dump(mode="json"):
                    raise InvestorError("idempotency_conflict", "计算标识已绑定另一不可变结果")
            else:
                self.session.add(SavedCalculation(id=str(calculation.id), revision=1, owner_id=self.owner, payload=calculation.model_dump(mode="json")))
                await self.session.flush()
        return await self.repository.save_assessment("local_owner", assessment)

    async def get(self, account_id: UUID, reference: ObjectRef, *, context: FitContext | None = None) -> AccountFitView:
        await PortfolioService(self.session, self.owner)._account(account_id)
        value = await self.repository.get_exact("local_owner", reference, expected_kind="account_fit")
        if not isinstance(value, AccountFitAssessment) or value.account_id != account_id:
            raise InvestorError("not_found", "该账户检查不存在")
        return AccountFitView(status="available", payload=value, current_limits=await self.limits(value, context=context))

    async def current(self, account_id: UUID, listing_ref: ObjectRef, *, action: str, purpose_binding_key: str, calculation_basis_key: str) -> AccountFitView:
        await PortfolioService(self.session, self.owner)._account(account_id)
        if listing_ref.kind != "listing" or listing_ref.revision != 1 or listing_ref.field_path is not None:
            raise InvestorError("invalid_reference", "当前查询需要精确证券身份")
        if len(purpose_binding_key) != 64 or len(calculation_basis_key) != 64:
            raise InvestorError("invalid_reference", "当前查询需要完整用途与情景标识")
        row = await self.session.scalar(select(InvestmentAssessment).where(InvestmentAssessment.owner_id == self.owner, InvestmentAssessment.account_id == str(account_id), InvestmentAssessment.kind == "account_fit", InvestmentAssessment.listing_id == int(listing_ref.id), InvestmentAssessment.payload["action"].as_string() == action, InvestmentAssessment.payload["purpose_binding_key"].as_string() == purpose_binding_key, InvestmentAssessment.payload["calculation_basis_key"].as_string() == calculation_basis_key, InvestmentAssessment.payload["frozen_inputs"]["mode"].as_string() == "live").order_by(InvestmentAssessment.as_of.desc(), InvestmentAssessment.id.desc()).limit(1))
        if row is None:
            return AccountFitView(status="missing", payload=None, current_limits=None)
        return await self.get(account_id, ObjectRef(kind="account_fit", id=UUID(row.id), revision=row.revision))

    async def limits(self, assessment: AccountFitAssessment, *, context: FitContext | None = None, current_sources: Sequence[FrozenListingSource] | None = None, now: datetime | None = None) -> CurrentLimits:
        return await self.input_limits(assessment.frozen_inputs, expires_at=assessment.expires_at, context=context, current_sources=current_sources, now=now)

    async def input_limits(self, frozen: FrozenAccountInputs, *, expires_at: datetime, context: FitContext | None = None, current_sources: Sequence[FrozenListingSource] | None = None, now: datetime | None = None) -> CurrentLimits:
        at = now or datetime.now(UTC)
        shared = context or await self.inputs.context(frozen.account_id, as_of=at)
        reasons = []
        if frozen.mode != "live":
            reasons.append("historical_replay")
        if at >= expires_at:
            reasons.append("assessment_expired")
        if shared.snapshot is None or shared.snapshot.reference != frozen.snapshot.reference:
            reasons.append("snapshot_changed")
        if (shared.valuation_binding.state, shared.valuation_binding.valuation_ref) != (frozen.valuation_binding.state, frozen.valuation_binding.valuation_ref):
            reasons.append("valuation_changed")
        if shared.mandate is None or shared.mandate.portfolio_constraints != frozen.mandate.portfolio_constraints or shared.mandate.hard_constraints != frozen.mandate.hard_constraints:
            reasons.append("mandate_changed")
        old_purpose, new_purpose = frozen.purpose_version, shared.purpose_version
        if (old_purpose is None) != (new_purpose is None) or (old_purpose is not None and new_purpose is not None and old_purpose.semantic_hash != new_purpose.semantic_hash):
            reasons.append("purposes_changed")
        # A caller may supply its explicit current form selection. The default
        # read context is unselected and does not invalidate a saved selection.
        if shared.purpose_binding.state == "selected" and shared.purpose_binding != frozen.purpose_binding:
            reasons.append("purpose_binding_changed")
        source_unavailable = False
        if current_sources is None:
            try:
                current_sources = await self.inputs.listing_sources(tuple(s.listing_ref for s in frozen.listing_sources), valuation=shared.valuation, as_of=at, rule_refs={s.listing_ref for s in frozen.listing_sources if s.rule_unknown_reason != "not_applicable"}, holding_refs={h.listing_ref for h in frozen.snapshot.holdings})
            except InvestorError as exc:
                if exc.detail.code != "invalid_reference":
                    raise
                source_unavailable = True
                reasons.append("listing_sources_unavailable")
                current_sources = ()
        by_ref = {source.listing_ref: source for source in current_sources}
        if not source_unavailable:
            for source in frozen.listing_sources:
                current = by_ref.get(source.listing_ref)
                if current is None or source_semantic(source) != source_semantic(current):
                    reasons.append("listing_sources_changed")
                if source.price.price_date is not None:
                    days = trading_days_after(source.price.price_date, at.astimezone(SHANGHAI_TZ).date(), self.calendar)
                    if self.calendar is None or days is None:
                        reasons.append("price_calendar_unavailable")
                    elif days > 5:
                        reasons.append("price_stale")
        unique = tuple(sorted(set(reasons)))
        return CurrentLimits(current_status="stale" if unique else "valid", reasons=unique, refresh_required=bool(unique))


class AccountFitNumericResolver:
    """Resolve display values, with diagnostics; never authorize plan targets.

    Known values in violated/needs_data scenarios remain useful for explaining
    a constraint. ``resolve`` always returns that diagnostic alongside them.
    """

    def __init__(self, record: CalculationRecord, account_id: UUID, listing_ref: ObjectRef) -> None:
        self.record, self.account_id, self.listing_ref = record, account_id, listing_ref

    def quantity(self, ref: ObjectRef, *, scenario: int, metric: str) -> Quantity:
        record = self.record
        if record.method != METHOD or record.method_version != METHOD_VERSION or record.details.get("account_id") != str(self.account_id) or record.details.get("listing_ref") != self.listing_ref.model_dump(mode="json"):
            raise InvestorError("invalid_reference", "账户数值方法、账户或标的不匹配")
        frozen = FrozenAccountInputs.model_validate(record.details.get("frozen_inputs"))
        required_refs = {frozen.snapshot.reference, ObjectRef(kind="mandate", id=frozen.mandate.id, revision=frozen.mandate.revision)}
        if frozen.valuation is not None:
            required_refs.add(frozen.valuation.reference)
        if frozen.purpose_version is not None:
            required_refs.add(frozen.purpose_version.reference)
        if record.status != CalculationStatus.OK or not record.applicable or record.origin_ref != frozen.snapshot.reference or not required_refs.issubset(set(record.fact_refs)) or frozen.account_id != self.account_id:
            raise InvestorError("invalid_reference", "计算状态或账户事实绑定不可用")
        if ref.kind != "calculation" or ref.id != record.id or ref.revision != record.revision or not (ref.field_path or "").startswith("results."):
            raise InvestorError("invalid_reference", "需要精确账户计算路径")
        matches = [path for path in record.details.get("result_paths", ()) if path["field_path"] == ref.field_path and path["scenario_index"] == scenario and path["metric"] == metric]
        if len(matches) != 1 or (matches[0].get("listing_ref") is not None and matches[0]["listing_ref"] != self.listing_ref.model_dump(mode="json")):
            raise InvestorError("invalid_reference", "数值路径、情景、指标或主体不匹配")
        scenarios = record.details.get("scenarios", ())
        if scenario < 0 or scenario >= len(scenarios) or matches[0].get("scenario_status") != scenarios[scenario]["status"] or matches[0].get("publication_ready") is not False:
            raise InvestorError("invalid_reference", "数值诊断不一致或冒充完整发布许可")
        result = record.results.get((ref.field_path or "")[8:])
        expected_unit = "ratio" if metric in {"weight", "sector_weight"} else "shares" if metric == "quantity" else "CNY"
        if result is None or result.value is None or result.unit != expected_unit or result.currency not in {None, "CNY"}:
            raise InvestorError("invalid_reference", "指标值或单位不可用")
        return result

    def resolve(self, ref: ObjectRef, *, scenario: int, metric: str) -> "ResolvedFitNumber":
        value = self.quantity(ref, scenario=scenario, metric=metric)
        diagnostics = self.record.details["scenarios"][scenario]
        return ResolvedFitNumber(quantity=value, scenario_status=diagnostics["status"], violations=tuple(diagnostics["violations"]), unknowns=tuple(diagnostics["unknowns"]))

    def render(self, ref: ObjectRef, *, scenario: int, metric: str, display: str = "decimal") -> str:
        quantity = self.quantity(ref, scenario=scenario, metric=metric)
        if display == "percent" and metric in {"weight", "sector_weight"} and quantity.unit == "ratio":
            with localcontext() as context:
                context.prec = 128
                quantity = quantity.model_copy(update={"unit": "fraction"})
                return format_quantity(quantity, display)[0]
        return format_quantity(quantity, display)[0]


class ResolvedFitNumber(Contract):
    quantity: Quantity
    scenario_status: Literal["ok", "needs_data", "violated"]
    violations: tuple[str, ...]
    unknowns: tuple[str, ...]
    authority: Literal["display_only_not_plan_authorization"] = "display_only_not_plan_authorization"

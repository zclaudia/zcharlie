"""Native research tools; exact trusted snapshot grant, durable ToolAction ledger."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant
from investment_agent.agent.tasks import ActionResult, fingerprint
from investment_agent.agent.tools import ToolRegistration
from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.calculation_contracts import (
    PortfolioCalculationRequest,
    PortfolioScenarioInput,
)
from investment_agent.portfolio.calculations import PortfolioCalculationService
from investment_agent.portfolio.plan_contracts import (
    PlanInput,
    PlanSemanticInput,
    PlanSemanticReview,
)
from investment_agent.portfolio.plans import PortfolioPlanService
from investment_agent.portfolio.service import PortfolioService
from investment_agent.storage.models.agent import ExecutionGrantRow, ToolAction


class PortfolioRead(Contract):
    snapshot_ref: ObjectRef
    account_id: UUID


async def grant_for(session: AsyncSession, operation: UUID, account_id: UUID, snapshot_ref: ObjectRef) -> ExecutionGrant:
    action = await session.get(ToolAction, str(operation))
    assert action is not None
    row = await session.get(ExecutionGrantRow, action.task_id)
    assert row is not None
    grant = ExecutionGrant.model_validate(row.payload)
    if grant.research_kind != "portfolio_action" or grant.portfolio_account_id != account_id or grant.portfolio_snapshot_ref != snapshot_ref:
        raise ExecutionDeniedError("exact_portfolio_grant_required")
    current = await PortfolioService(session).snapshot(account_id)
    if current.reference != snapshot_ref:
        raise ExecutionDeniedError("actual_snapshot_changed_research_restart_required")
    from investment_agent.agent.tasks import TaskService
    from investment_agent.portfolio.purposes import PurposeService
    purposes = await PurposeService(session).current(account_id)
    task = await TaskService(session).get_task(UUID(action.task_id))
    if purposes is not None and purposes.version.confirmed_at > task.binding.as_of:
        raise ExecutionDeniedError("purpose_confirmed_after_task_cutoff_fresh_message_required")
    return grant


async def read_scope(session: AsyncSession, business: PortfolioRead) -> tuple[ObjectRef, ...]:
    del session
    return (business.snapshot_ref,)


async def read(session: AsyncSession, operation: UUID, business: PortfolioRead) -> ActionResult:
    grant = await grant_for(session, operation, business.account_id, business.snapshot_ref)
    snapshot = await PortfolioService(session).snapshot(business.account_id)
    current = await PortfolioPlanService(session).current(business.account_id)
    from investment_agent.portfolio.purposes import PurposeService
    from investment_agent.portfolio.valuation import ValuationService

    purposes = await PurposeService(session).current(business.account_id)
    # The calculation must bind the displayed price basis (plan 2.5): live
    # research binds the account's current valuation, a historical task the
    # newest one its research cutoff could know. Without a usable valuation the
    # legacy frozen-snapshot contract stays available.
    valuations = ValuationService(session)
    valuation = await valuations.current(business.account_id)
    if not grant.current_research:
        from investment_agent.agent.observation import effective_cutoff

        action = await session.get(ToolAction, str(operation))
        assert action is not None
        cutoff = await effective_cutoff(session, UUID(action.task_id))
        valuation = await valuations.latest_at(business.account_id, cutoff=cutoff)
    if valuation is not None and valuation.ledger_revision != snapshot.revision:
        valuation = None
    return ActionResult({"purposes": purposes.model_dump(mode="json") if purposes else None, "status": "ok", "snapshot_ref": snapshot.reference.model_dump(mode="json"), "snapshot": snapshot.model_dump(mode="json"), "valuation_ref": valuation.reference.model_dump(mode="json") if valuation else None, "valuation": valuation.model_dump(mode="json") if valuation else None, "current_plan": current.model_dump(mode="json") if current else None})


async def calc_scope(session: AsyncSession, business: PortfolioScenarioInput) -> tuple[ObjectRef, ...]:
    del session
    return (business.snapshot_ref, *(a.listing_ref for s in business.scenarios for a in s.actions))


async def _require_usable_valuation(
    session: AsyncSession,
    operation: UUID,
    grant: ExecutionGrant,
    business: PortfolioScenarioInput,
) -> None:
    """The bound valuation must belong to the snapshot and respect PIT (2.5).

    Live research binds exactly the account's current valuation (no silent
    price substitution: refresh the context and re-bind); a historical task
    binds a valuation its research cutoff could know.
    """

    from uuid import UUID as _UUID

    from investment_agent.agent.observation import effective_cutoff
    from investment_agent.portfolio.valuation import ValuationService

    assert business.valuation_ref is not None
    valuations = ValuationService(session)
    valuation = await valuations.get(business.account_id, _UUID(str(business.valuation_ref.id)))
    if (
        valuation.snapshot_id != _UUID(str(business.snapshot_ref.id))
        or valuation.ledger_revision != business.snapshot_ref.revision
    ):
        raise InvestorError("invalid_reference", "估值不属于该事实版本；按当前事实重新读取上下文")
    action = await session.get(ToolAction, str(operation))
    assert action is not None
    if grant.current_research:
        current = await valuations.current(business.account_id)
        if current is None or current.reference != business.valuation_ref:
            raise ExecutionDeniedError("valuation_updated_reload_portfolio_context")
    else:
        cutoff = await effective_cutoff(session, _UUID(action.task_id))
        if valuation.knowledge_cutoff > cutoff:
            raise ExecutionDeniedError("valuation_after_research_cutoff")


async def calculate(session: AsyncSession, operation: UUID, business: PortfolioScenarioInput) -> ActionResult:
    grant = await grant_for(session, operation, business.account_id, business.snapshot_ref)
    from investment_agent.portfolio.purposes import PurposeService
    if business.valuation_ref is not None:
        await _require_usable_valuation(session, operation, grant, business)
    purposes = await PurposeService(session).current(business.account_id)
    if purposes is not None and business.purpose_ref != purposes.version.reference:
        raise InvestorError("invalid_reference", "必须绑定该账户当前确认用途版本")
    record = await PortfolioCalculationService(session).save(PortfolioCalculationRequest(**business.model_dump(), operation_id=operation))
    ref = ObjectRef(kind="calculation", id=record.id, revision=record.revision)
    return ActionResult({"status": "ok", "calculation": record.model_dump(mode="json"), "calculation_ref": ref.model_dump(mode="json")}, (ref,))


async def prepare_scope(session: AsyncSession, business: PlanInput) -> tuple[ObjectRef, ...]:
    del session
    return (business.snapshot_ref, *(a.listing_ref for o in business.options for a in o.actions))


async def admitted(session: AsyncSession, task_id: UUID) -> bool:
    row = await session.get(ExecutionGrantRow, str(task_id))
    return row is not None and ExecutionGrant.model_validate(row.payload).research_kind == "portfolio_action"


async def ready(session: AsyncSession, task_id: UUID) -> bool:
    if not await admitted(session, task_id):
        return False
    rows = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task_id), ToolAction.state == "committed"))).all()
    return any(row.parameters.get("tool") == "save_research_progress" and row.result and row.result.get("progress", {}).get("kind") == "portfolio_action" for row in rows)


async def prepare(session: AsyncSession, operation: UUID, business: PlanInput) -> ActionResult:
    await grant_for(session, operation, business.account_id, business.snapshot_ref)
    action = await session.get(ToolAction, str(operation))
    assert action is not None
    if not await ready(session, UUID(action.task_id)):
        raise ExecutionDeniedError("persisted_portfolio_research_plan_required")
    record = await PortfolioPlanService(session).verified_calculation(business)
    ref = ObjectRef(kind="publication_preparation", id=operation, revision=1)
    return ActionResult({"preparation_ref": ref.model_dump(mode="json"), "candidate": business.model_dump(mode="json"), "candidate_hash": fingerprint(business.model_dump(mode="json")), "calculation": record.model_dump(mode="json"), "review_required": "Review every action's exact holding, company view or typed mandate exception, opposing material and residual unknowns before completion"}, (ref,))


async def reviewed(session: AsyncSession, task_id: UUID, review: PlanSemanticInput) -> tuple[PlanInput, PlanSemanticReview]:
    ref = review.preparation_ref
    row = await session.get(ToolAction, str(ref.id))
    if ref.kind != "publication_preparation" or ref.revision != 1 or ref.field_path or row is None or row.task_id != str(task_id) or row.state != "committed" or row.parameters.get("tool") != "prepare_portfolio_plan" or not row.result:
        raise ExecutionDeniedError("exact_portfolio_preparation_required")
    return PlanInput.model_validate(row.result["candidate"]), PlanSemanticReview(candidate_hash=row.result["candidate_hash"], assessments=review.assessments, checked_sources=review.checked_sources, unresolved_questions=review.unresolved_questions, conclusion=review.conclusion)


def registrations() -> tuple[ToolRegistration, ...]:
    return (
        ToolRegistration("get_portfolio_context", PortfolioRead, read_scope, database=read, ready=admitted),  # type: ignore[arg-type]
        ToolRegistration("calculate_portfolio_options", PortfolioScenarioInput, calc_scope, database=calculate, ready=admitted),  # type: ignore[arg-type]
        ToolRegistration("prepare_portfolio_plan", PlanInput, prepare_scope, database=prepare, ready=ready),  # type: ignore[arg-type]
    )


async def progress_calculation(session: AsyncSession, grant: ExecutionGrant, ref: ObjectRef) -> CalculationRecord:
    """Only this admitted portfolio branch can resolve account calculations."""
    if grant.research_kind != "portfolio_action" or grant.portfolio_account_id is None or grant.portfolio_snapshot_ref is None or ref.kind != "calculation":
        raise ExecutionDeniedError("exact_portfolio_calculation_scope_required")
    service = PortfolioPlanService(session)
    record = await service.calculations.get(grant.portfolio_account_id, UUID(str(ref.id)))
    request = PortfolioCalculationRequest.model_validate(record.details["request"])
    if ref.revision != record.revision or request.snapshot_ref != grant.portfolio_snapshot_ref or request.mandate_ref.id != grant.mandate_id:
        raise ExecutionDeniedError("portfolio_calculation_context_differs")
    rebuilt = await service.calculations.rebuild(request)
    if rebuilt.input_hash != record.input_hash or rebuilt.results != record.results or rebuilt.details != record.details:
        raise ExecutionDeniedError("portfolio_calculation_rebuild_differs")
    return record

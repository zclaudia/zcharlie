"""Continue an exact question from its public validated plan, not model history."""
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import ExecutionGrant, ResearchValidationError
from investment_agent.agent.tasks import ActionResult, TaskClaim, TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.enums import DimensionStatus
from investment_agent.storage.models.agent import ExecutionGrantRow, MessageRow, ToolAction


async def reuse_question_plan(session: AsyncSession, claim: TaskClaim, grant: ExecutionGrant,
                             message: MessageRow, method_ref: ObjectRef, *, base: Settings) -> dict[str, Any] | None:
    from investment_agent.agent.research import ResearchProgress, save_progress, validate_progress
    from investment_agent.investor.methods import task_method
    service = TaskService(session)
    current = await service.get_task(claim.task_id)
    prior_messages = (await session.scalars(select(MessageRow).where(
        MessageRow.conversation_id == message.conversation_id, MessageRow.sequence < message.sequence
    ).order_by(MessageRow.sequence.desc()).limit(10))).all()
    for previous in prior_messages:
        payload = previous.payload
        if payload.get("role") != "user" or payload.get("text") != grant.prompt or not payload.get("task_ref"):
            continue
        task_id = UUID(payload["task_ref"]["id"])
        task = await service.get_task(task_id)
        if (task.status not in {"partial", "failed"} or task.stop_reason not in {"budget_exhausted", "no_progress", "evidence_blocked"}
                or current.created_at - task.created_at > timedelta(days=1)
                or current.binding.as_of < task.binding.as_of):
            continue
        saved = await session.get(ExecutionGrantRow, str(task_id))
        if saved is None:
            continue
        old = ExecutionGrant.model_validate(saved.payload)
        if (old.owner != grant.owner or old.mode != grant.mode or old.research_kind != grant.research_kind
                or old.current_research != grant.current_research
                or set(old.scope_refs) != set(grant.scope_refs)
                or (await task_method(session, task_id)).method_ref != method_ref):
            continue
        actions = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task_id),
            ToolAction.state == "committed"))).all()
        planned = next((a for a in actions if a.parameters.get("tool") == "save_research_progress" and a.result), None)
        if planned is None or planned.result is None:
            continue
        progress = ResearchProgress.model_validate(planned.result["progress"])
        if progress.answered:
            continue
        # Only public planning is carried over. No previous answer, prepared
        # opinion or private reasoning becomes a current finding.
        plan = progress.model_copy(update={"answered": False, "content": (), "completed_steps": (),
            "fact_refs": (), "counter_refs": (), "inferences": (), "assumptions": (),
            "gaps": (), "limitations": (), "stopped_because": None,
            "dimensions": tuple(d.model_copy(update={"status": DimensionStatus.MISSING, "refs": (),
                "reason": "本次尚待核查，沿用规划不继承取证结论" if d.applicable else d.reason}) for d in progress.dimensions)})
        try:
            await validate_progress(session, claim.task_id, plan)
        except ResearchValidationError:
            continue
        parameters = {"tool": "save_research_progress", "business": plan.model_dump(mode="json"),
                      "phase": "research:v1", "reused_plan_from": planned.operation_id}
        action = await service.prepare_action(claim, intent_key="continuation:public-plan", parameters=parameters, effect="database")
        async def save(owned: AsyncSession, operation: UUID, plan: ResearchProgress = plan, source_operation: str = planned.operation_id) -> ActionResult:
            result = await save_progress(owned, operation, plan)
            return ActionResult({**result.output, "reused_plan_from": source_operation}, artifact_refs=result.artifact_refs)
        result = await service.settle_action(claim, UUID(action.operation_id), save)
        reused = {"artifact_ref": result["artifact_ref"], "source_task_id": str(task_id), "question": plan.question,
            "hypothesis_questions": [h.question for h in plan.hypotheses],
            "completion_criteria": list(plan.completion_criteria), "stop_conditions": list(plan.stop_conditions),
            "policy": "The same-question public plan was copied into this task through the normal fenced save_progress transaction, revalidated under this grant/method/cutoff. No old conclusions or private reasoning were imported. Do not save another plan: prepare a concise view from current reusable source windows, then return final progress and semantic review. Final plan wording may be concise; all required fields and seven dimensions remain mandatory."}
        preparations = [r for r in task.artifact_refs if r.kind == "publication_preparation"]
        if preparations:
            previous_preparation = await session.get(ToolAction, str(preparations[-1].id))
            if (previous_preparation and previous_preparation.state == "committed" and previous_preparation.result
                    and not previous_preparation.result.get("material_review_blocked")):
                from investment_agent.agent.budget import BudgetService
                from investment_agent.agent.execution import check_boundary
                from investment_agent.agent.research_publication import CompanyViewInput, prepare
                from investment_agent.investor.foundations import InvestorService
                business = CompanyViewInput.model_validate(previous_preparation.parameters["business"])
                case = await InvestorService(session).get_case(grant.owner, UUID(str(business.case_ref.id)))
                business = business.model_copy(update={"case_ref": ObjectRef(kind="case", id=case.id, revision=case.revision)})
                await check_boundary(session, claim, base, tool="prepare_company_view", required_refs=(business.case_ref,))
                await BudgetService(session, base).check(claim)
                params = {"tool": "prepare_company_view", "business": business.model_dump(mode="json"),
                          "phase": "research:v1", "reused_preparation_from": previous_preparation.operation_id}
                prepared = await service.prepare_action(claim, intent_key="continuation:public-candidate", parameters=params, effect="database")
                try:
                    async def rebuild(owned: AsyncSession, operation: UUID, business: CompanyViewInput = business) -> ActionResult:
                        return await prepare(owned, operation, business)
                    reused["prepared_review"] = await service.settle_action(claim, UUID(prepared.operation_id), rebuild)
                    reused["policy"] = "Public planning and an unpublished candidate were revalidated and prepared under this task's current authorization/cutoff/method. Only the new model's final semantic review and answer remain. No old private reasoning or semantic approval was reused. Do not repeat planning or preparation; publication still checks every gate."
                except ResearchValidationError:
                    pass  # Changed premises need a real new model revision.
        return reused
    return None

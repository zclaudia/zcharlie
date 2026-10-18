"""Real worker executor using one native Agent run per persisted attempt.

No investment research strategy or public message API lives here. Default runs
save an unpublished draft and remain partial; AG13 supplies a trusted completion
policy, and AG09 consumes saved material through its message/event service.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic_ai import CancellationToken, DeferredToolRequests, DeferredToolResults
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.budget import BudgetExceededError, BudgetService
from investment_agent.agent.contracts import WaitRequirement
from investment_agent.agent.execution import (
    ExecutionDeniedError,
    ExecutionGrant,
    NoProgressError,
    ResearchValidationError,
    check_boundary,
)
from investment_agent.agent.job import SessionFactory
from investment_agent.agent.model import AgentModelConfig
from investment_agent.agent.runtime import AgentRunError, PrivateHistory, RunOutcome, run_agent
from investment_agent.agent.tasks import (
    CompletedTaskStatus,
    ReconciliationRequiredError,
    TaskBoundaryError,
    TaskClaim,
    TaskService,
)
from investment_agent.agent.tools import ExecutionContext, ToolRegistration, TrustedTools
from investment_agent.config import Settings
from investment_agent.investor.enums import StopReason, TaskStatus
from investment_agent.jobs.base import JobContext, JobOutcome
from investment_agent.research.retry_policy import ResearchRetryableError
from investment_agent.settings.loader import load_agent_model_config
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentModelRun, AgentTask, BudgetEntry

DEFERRED = TypeAdapter(DeferredToolRequests)

# Structured stop classification: exception types and safe codes only, never
# error text. The safe code spellings cover boundary errors that carry their
# code as the message (e.g. "time_budget_exhausted").
BUDGET_STOP_CODES = frozenset({"BudgetExceededError", "UsageLimitExceeded",
                               "TimeBudgetExceededError", "TimeoutError"})
RESEARCH_STOP_CODE_PREFIXES = ("research_", "comparison_")


def _stop_reason(error: BaseException, code: str) -> StopReason:
    if isinstance(error, NoProgressError) or code == "NoProgressError" or "no_progress" in code:
        return StopReason.NO_PROGRESS
    if isinstance(error, BudgetExceededError) or code in BUDGET_STOP_CODES or "budget" in code:
        return StopReason.BUDGET_EXHAUSTED
    if isinstance(error, ResearchValidationError) or code.startswith(RESEARCH_STOP_CODE_PREFIXES):
        return StopReason.EVIDENCE_BLOCKED
    return StopReason.FAILED


class UnpublishedDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str


@dataclass(frozen=True)
class TaskDisposition:
    """Certified business stop, distinct from successful native model output."""

    terminal: CompletedTaskStatus
    reason: StopReason


CompletionPolicy = Callable[[AsyncSession, TaskClaim, RunOutcome], Awaitable[TaskDisposition]]
ContextBuilder = Callable[[AsyncSession, TaskClaim, Settings, dict[str, Any]], Awaitable[tuple[str, Any, bool]]]


@dataclass(frozen=True)
class RunSpecification:
    """Server-owned strategy seam; never constructed from model output."""

    output_type: type[BaseModel] = UnpublishedDraft
    instructions: str = "Answer the question with an unpublished JSON draft. Use authorized tools for facts."
    phase: str = "draft:v1"
    completion: CompletionPolicy | None = None
    context_builder: ContextBuilder | None = None
    output_check: Callable[[AsyncSession, TaskClaim, BaseModel], Awaitable[None]] | None = None


class _RequestBudget:
    def __init__(self, context: ExecutionContext, config: AgentModelConfig) -> None:
        self.ctx, self.config = context, config
        self.reservations: list[UUID] = []
        self.output_repair_pending = False
        self.native_validation_retries = 0
        self.native_validation_retries_by_scope: dict[str, int] = {}

    def validation_retry(self, tool_name: str | None = None) -> None:
        # Native schema validation happens before our business/output callback.
        # Give its bounded corrections the same stagnation treatment as output
        # repair; all request/token/time/tool/permission checks still run.
        # Native retries belong to each tool/output validator. A preparation
        # correction must not consume the final-output validator's allowance.
        # Framework retries and the shared task/day ledger remain authoritative.
        scope = tool_name or "<output>"
        used = self.native_validation_retries_by_scope.get(scope, 0)
        if used < 2:
            self.native_validation_retries_by_scope[scope] = used + 1
            self.native_validation_retries += 1
            self.output_repair_pending = True

    async def guidance(self) -> str:
        if self.ctx.phase != "research:v1":
            return ""
        async with self.ctx.sessions() as session:
            grant, _ = await check_boundary(session, self.ctx.claim, self.ctx.base)
            if grant.research_kind not in {"company_research", "review", "local_check"}:
                return ""
            state = await BudgetService(session, self.ctx.base).research_headroom(self.ctx.claim)
        planning = ("非收尾阶段先实际调用保存计划和取证工具。" if grant.research_kind in {"company_research", "review"}
                    else "局部核查与解释只读取直接相关材料，不扩展为完整研究；已有材料足够形成有界回答时立即收尾。")
        return ("Trusted remaining research resources: " + str(state) +
                ". Preserve conclusion reserve. finish_only hides gathering/save-progress tools; full research "
                "may still prepare_company_view under the existing plan/publication gates, then return final "
                "progress plus semantic_review. The controller saves the final answer. Reuse inspected sources, "
                "keep candidate quality and exact refs; explicit gaps may support an undetermined view. "
                "Text fields are qualitative only: numbers/dates/tickers require numeric_claim/entity_ref. "
                "Omit optional empty fields, never required plan fields. If incomplete, explain stopped_because. " + planning)

    async def reserve(self, estimated_input: int) -> UUID:
        output = self.config.values["agent_max_tokens"]
        if output is None:
            raise ExecutionDeniedError("finite_output_token_bound_required")
        async with self.ctx.sessions() as session:
            if self.ctx.phase == "research:v1" and not self.output_repair_pending:
                from investment_agent.agent.research import check_progress_round
                await check_progress_round(session, self.ctx.claim)
            self.output_repair_pending = False
            current = await load_agent_model_config(session, base=self.ctx.base)
            if current.version != self.config.version:
                raise ExecutionDeniedError("model_configuration_changed")
            reservation = await BudgetService(session, self.ctx.base).reserve(self.ctx.claim,
                estimated_tokens=estimated_input + output, estimated_input=estimated_input, output_bound=output)
            self.reservations.append(reservation)
            return reservation

    async def sent(self, reservation: UUID) -> None:
        async with self.ctx.sessions() as session:
            await BudgetService(session, self.ctx.base).sent(self.ctx.claim, reservation)
            current = await load_agent_model_config(session, base=self.ctx.base)
            if current.version != self.config.version:
                raise ExecutionDeniedError("model_configuration_changed")

    async def observe_tools(self, count: int) -> None:
        async with self.ctx.sessions() as session:
            await BudgetService(session, self.ctx.base).observe_tools(self.ctx.claim, count)
        async with self.ctx.sessions() as session:
            await BudgetService(session, self.ctx.base).check(self.ctx.claim)

    async def settle(self, reservation: UUID, usage: dict[str, int] | None, *, sent: bool) -> None:
        async with self.ctx.sessions() as session:
            await BudgetService(session, self.ctx.base).settle(reservation, usage, sent=sent)


class AgentExecutor:
    def __init__(self, base: Settings, *, sessions: SessionFactory = session_scope,
                 registrations: tuple[ToolRegistration, ...] = (),
                 specification: RunSpecification | None = None,
                 model: Model | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base, self.sessions, self.registrations = base, sessions, registrations
        self.specification, self.model, self.transport = specification or RunSpecification(), model, transport

    async def __call__(self, claim: TaskClaim, ctx: JobContext) -> JobOutcome:
        del ctx  # worker owns heartbeat/retry; executor owns task checkpoint boundaries.
        execution = ExecutionContext(claim, self.sessions, self.base, self.specification.phase)
        try:
            async with self.sessions() as session:
                grant, settings = await check_boundary(session, claim, self.base)
                await BudgetService(session, self.base).recover(claim)
                await BudgetService(session, self.base).check(claim)
                task = await TaskService(session).get_task(claim.task_id)
                remaining = settings.investor_agent_task_seconds - (datetime.now(UTC) - task.created_at).total_seconds()
                config = await load_agent_model_config(session, base=self.base)
                envelope, _ = await TaskService(session).get_checkpoint(claim.task_id)
                checkpoint = envelope.get("data", {})
                instructions = self.specification.instructions
                context_stamp = None
                history_allowed = True
                if self.specification.context_builder is not None:
                    instructions, context_stamp, history_allowed = await self.specification.context_builder(session, claim, self.base, checkpoint)
                await TaskService(session).append_event(claim.task_id, claim.attempt_id, "step_started",
                    {"step_id": str(uuid4()), "summary": "Executing authorized agent phase"})
            # Read/reconcile durable effects before constructing the native run.
            from investment_agent.agent.context import reusable_context_sources
            execution = replace(execution, reuse_sources=bool(reusable_context_sources(instructions)))
            tools = TrustedTools(execution, self.registrations)
            await tools.recover()
            if checkpoint.get("history") and checkpoint.get("memory_revision", 0) != grant.memory_revision:
                raise ExecutionDeniedError("memory_context_changed_rebuild_required")
            if checkpoint.get("history") and not history_allowed:
                raise ExecutionDeniedError("memory_context_expired_rebuild_required")
            history = PrivateHistory.model_validate(checkpoint["history"]) if checkpoint.get("history") else None
            results = DeferredToolResults(calls=checkpoint["deferred_results"]) if checkpoint.get("deferred_results") else None
            if checkpoint.get("deferred"):
                deferred = DEFERRED.validate_python(checkpoint["deferred"])
                if deferred.approvals:
                    raise ExecutionDeniedError("trusted_approval_handler_required")
                if results is None or set(results.calls) != {call.tool_call_id for call in deferred.calls}:
                    raise ExecutionDeniedError("deferred_results_incomplete")
            error: BaseException | None = None
            retryable = False
            outcome: RunOutcome | None = None
            token = CancellationToken()
            lifecycle = _RequestBudget(execution, config)
            rejected_output: tuple[ResearchValidationError, BaseModel] | None = None
            async def validate_output(run_context: Any, output: Any) -> Any:
                nonlocal rejected_output
                from pydantic_ai import ModelRetry
                if isinstance(output, DeferredToolRequests):
                    return output
                assert self.specification.output_check is not None
                try:
                    async with self.sessions() as session:
                        await check_boundary(session, claim, self.base)
                        await self.specification.output_check(session, claim, output)
                except ResearchValidationError as exc:
                    rejected_output = (exc, output)
                    if exc.repairable and run_context.retry < config.values["agent_retries"]:
                        lifecycle.output_repair_pending = True
                        raise ModelRetry(exc.repair_message()) from exc
                    if exc.repairable and config.values["agent_retries"] == 0:
                        # The model was never offered a correction round: defer
                        # the disposition to completion (which re-checks before
                        # any write) instead of failing it outright.
                        return output
                    raise
                return output
            output_type = self.specification.output_type
            if self.specification.phase == "research:v1" and grant.research_kind in {"company_research", "review"}:
                from investment_agent.agent.research import CompanyResearchOutput, ResearchOutput
                if output_type is ResearchOutput:
                    output_type = CompanyResearchOutput
            elif self.specification.phase == "research:v1" and grant.research_kind == "local_check":
                from investment_agent.agent.research import LocalResearchOutput, ResearchOutput
                if output_type is ResearchOutput:
                    output_type = LocalResearchOutput
            # An audit-only session performs no read/write until run_agent's
            # final flush; it never holds a transaction across model/tool I/O.
            async with self.sessions() as audit:
                try:
                    outcome = await run_agent(config, session=audit,
                        output_type=output_type, toolsets=[tools.toolset(grant.tool_names, validation_retries=config.values["agent_retries"])],
                        instructions=instructions, prompt=None if history else grant.prompt,
                        deps=execution, history=history, deferred_tool_results=results,
                        usage_limits=UsageLimits(request_limit=settings.investor_agent_task_requests,
                            tool_calls_limit=settings.investor_agent_task_tool_calls,
                            input_tokens_limit=settings.investor_agent_task_tokens,
                            output_tokens_limit=settings.investor_agent_task_tokens,
                            total_tokens_limit=settings.investor_agent_task_tokens),
                        cancellation_token=token, pipeline_run_id=claim.lease.run_id,
                        task_id=claim.task_id, attempt_id=claim.attempt_id,
                        model=self.model, transport=self.transport,
                        request_lifecycle=lifecycle, timeout_seconds=remaining,
                        # The validator always runs so output_check gates every
                        # completion; with zero retries its ModelRetry branch
                        # raises directly instead of silently skipping.
                        output_validator=validate_output if self.specification.output_check is not None else None)
                except BaseException as exc:
                    error = exc  # commit audit before propagating cancellation/failure
                call_id = outcome.call_id if outcome else error.call_id if isinstance(error, AgentRunError) else None
                if call_id is not None:
                    audit_row = await audit.get(AgentModelRun, str(call_id))
                    assert audit_row is not None
                    if isinstance(error, AgentRunError):
                        # Connection/timeout-class failures retry through the
                        # worker; HTTP status retries additionally require the
                        # failed response to have settled known usage.
                        retryable = error.transient or (error.code == "ModelHTTPError" and
                            audit_row.usage is not None and bool(audit_row.http_responses) and
                            (audit_row.http_responses[-1]["status"] in {408, 429} or audit_row.http_responses[-1]["status"] >= 500))
                    receipts = (await audit.scalars(select(BudgetEntry).where(
                        BudgetEntry.id.in_(list(map(str, lifecycle.reservations))), BudgetEntry.state != "released"
                    ))).all()
                    if receipts and all(r.cost_usd is not None for r in receipts):
                        audit_row.cost_usd = sum((r.cost_usd for r in receipts if r.cost_usd is not None), Decimal(0))
                        audit_row.cost_unknown_reason = None
                    else:
                        audit_row.cost_unknown_reason = "price_or_usage_unknown"
            if error is not None:
                if rejected_output is not None:
                    rejected, rejected_draft = rejected_output
                    async with self.sessions() as session:
                        await check_boundary(session, claim, self.base)
                        service = TaskService(session)
                        envelope, _ = await service.get_checkpoint(claim.task_id)
                        data = envelope.get("data", {})
                        data["rejected_completion"] = {"call_id": str(call_id), "code": rejected.code,
                            "field_errors": [e.model_dump(mode="json") for e in rejected.field_errors],
                            "draft": rejected_draft.model_dump(mode="json")}
                        await service.checkpoint(claim, checkpoint=data)
                if retryable:
                    # The shared worker performs bounded retry/backoff. A run
                    # with persisted unknown usage is never retried here (the
                    # budget check below fails closed), and the same task's
                    # durable request/token/cost allowance is checked first.
                    async with self.sessions() as session:
                        await BudgetService(session, self.base).check(claim)
                    raise ResearchRetryableError(run_id=claim.lease.run_id,
                        reason="Agent provider failed with known settled usage; retry within existing task allowance")
                raise error
            assert outcome is not None
            if context_stamp is not None:
                checkpoint["context_stamp"] = context_stamp.model_dump(mode="json")
            try:
                await self._save_outcome(execution, outcome, grant, checkpoint)
            except ResearchValidationError as rejected:
                # Completion rolls back all proposed business writes. Keep a
                # separate private diagnostic checkpoint, never public model text.
                async with self.sessions() as session:
                    await check_boundary(session, claim, self.base)
                    service = TaskService(session)
                    envelope, _ = await service.get_checkpoint(claim.task_id)
                    data = envelope.get("data", {})
                    data["rejected_completion"] = {"call_id": str(outcome.call_id),
                        "code": rejected.code,
                        "field_errors": [e.model_dump(mode="json") for e in rejected.field_errors],
                        "draft": outcome.output.model_dump(mode="json")}
                    await service.checkpoint(claim, checkpoint=data)
                raise
        except asyncio.CancelledError:
            async with self.sessions() as session:
                task = await TaskService(session).get_task(claim.task_id)
                if task.cancel_requested:
                    await TaskService(session).ack_cancel(claim)
                    return "completed"
            raise  # worker shutdown: stale recovery owns next attempt
        except (AgentRunError, ExecutionDeniedError, TaskBoundaryError, ReconciliationRequiredError) as exc:
            await self._stop(claim, exc)
        return "completed"

    async def _save_outcome(self, ctx: ExecutionContext, outcome: RunOutcome,
                            grant: ExecutionGrant, prior: dict[str, Any]) -> None:
        async with self.sessions() as session:
            _, effective = await check_boundary(session, ctx.claim, self.base)
            await BudgetService(session, self.base).check(ctx.claim)
            service = TaskService(session)
            persisted, _ = await service.get_checkpoint(ctx.claim.task_id)
            data: dict[str, Any] = {**persisted.get("data", {}), **{k: v for k, v in prior.items() if k in {"context_stamp", "observation_cutoffs"}}, "history": outcome.history.model_dump(mode="json"),
                                    "call_id": str(outcome.call_id), "phase": ctx.phase, "memory_revision": grant.memory_revision, "memory_valid_until": grant.memory_valid_until.isoformat() if grant.memory_valid_until else None}
            if isinstance(outcome.output, DeferredToolRequests):
                if outcome.output.approvals:
                    raise ExecutionDeniedError("trusted_approval_handler_required")
                dependencies = tuple(UUID(outcome.output.metadata[c.tool_call_id]["dependency_id"])
                                     for c in outcome.output.calls)
                if not dependencies:
                    raise ExecutionDeniedError("deferred_dependencies_required")
                data["deferred"] = DEFERRED.dump_python(outcome.output, mode="json")
                data["deferred_results"] = {}
                await service.checkpoint(ctx.claim, checkpoint=data,
                    waiting=WaitRequirement(required_input="Waiting for authorized tool dependencies",
                        dependency_ids=tuple(dict.fromkeys(dependencies)),
                        deadline=(await service.get_task(ctx.claim.task_id)).created_at + timedelta(seconds=effective.investor_agent_task_seconds)))
            else:
                # Save an unpublished artifact. Native run success is not proof
                # that AG13's research completion/publication gates were met.
                data["draft"] = outcome.output.model_dump(mode="json")
                await service.append_event(ctx.claim.task_id, ctx.claim.attempt_id, "progress",
                    {"summary": "模型输出已保存，正在核验研究与发布条件"})
                task = await service.get_task(ctx.claim.task_id)
                disposition = TaskDisposition("partial" if task.artifact_refs else "failed", StopReason.EVIDENCE_BLOCKED)
                if self.specification.completion is not None:
                    # AG13 certifies existing saved artifacts against its
                    # completion criteria; no model can select this callable.
                    disposition = await self.specification.completion(session, ctx.claim, outcome)
                    await check_boundary(session, ctx.claim, self.base)
                    await BudgetService(session, self.base).check(ctx.claim)
                await service.checkpoint(ctx.claim, checkpoint=data,
                    terminal=disposition.terminal, stop_reason=disposition.reason)

    async def _stop(self, claim: TaskClaim, error: BaseException) -> None:
        async with self.sessions() as session:
            service = TaskService(session)
            task = await service.get_task(claim.task_id)
            if task.status != TaskStatus.RUNNING:
                return
            if task.cancel_requested:
                await service.ack_cancel(claim)
                return
            if isinstance(error, AgentRunError | ResearchValidationError):
                code = error.code
            elif isinstance(error, TaskBoundaryError):
                code = str(error)  # boundary errors carry their safe code as the message
            else:
                code = type(error).__name__
            field_errors = error.field_errors if isinstance(error, AgentRunError | ResearchValidationError) else ()
            reason = _stop_reason(error, code)
            await service.append_event(claim.task_id, claim.attempt_id, "error",
                {"code": code, "message": "Memory context changed; start a new task with rebuilt trusted context" if code.startswith("memory_") else "Execution stopped at a trusted boundary", "retryable": False, "field_errors": [e.model_dump(mode="json") for e in field_errors]})
            if field_errors and task.message_ref is not None:
                from investment_agent.agent.conversations import ConversationService
                await ConversationService(session, self.base).save_assistant(claim,
                    text="研究尚未完成，候选内容没有发布。\n" + "\n".join(f"{item.field}：{item.message}" for item in field_errors) + "\n下一步：按来源引用与授权范围修复研究进展；证据不足时明确保留缺口。",
                    artifact_refs=task.artifact_refs)
            envelope, _ = await service.get_checkpoint(claim.task_id)
            checkpoint = envelope.get("data", {})
            if field_errors and isinstance(error, AgentRunError):
                checkpoint["rejected_tool_validation"] = {"call_id": str(error.call_id), "code": code, "field_errors": [item.model_dump(mode="json") for item in field_errors]}
            await service.checkpoint(claim, checkpoint=checkpoint,
                terminal="partial" if task.artifact_refs else "failed", stop_reason=reason)


async def deliver_deferred_result(
    session: AsyncSession, task_id: UUID, dependency_id: UUID, *, wait_token: UUID,
    completion_event_id: UUID, result: dict[str, Any],
) -> bool:
    """Trusted dependency consumer seam, atomic private result + generation-fenced resume.

    AG09/collector consumers authenticate the completion source before calling;
    no model can grant approvals, invent a wait token or submit this control API.
    """
    service = TaskService(session)
    # Serialize read-modify-write of the entire private result map before any
    # read; concurrent independent dependencies must not overwrite each other.
    await session.execute(update(AgentTask).where(AgentTask.id == str(task_id))
                          .values(revision=AgentTask.revision))
    row = await service._row(task_id)
    if row.status != "waiting_data" or row.wait_token != str(wait_token):
        return False
    envelope, _ = await service.get_checkpoint(task_id)
    checkpoint = dict(envelope.get("data", {}))
    deferred = DEFERRED.validate_python(checkpoint["deferred"])
    matched = [call.tool_call_id for call in deferred.calls
               if deferred.metadata[call.tool_call_id]["dependency_id"] == str(dependency_id)]
    if not matched:
        return False
    results = dict(checkpoint.get("deferred_results", {}))
    if any(call in results for call in matched):
        return False
    results.update({call: result for call in matched})
    checkpoint["deferred_results"] = results
    row.checkpoint = {"schema_version": 1, "data": checkpoint}
    return await service.resume(task_id, dependency_id, wait_token=wait_token,
                                completion_event_id=completion_event_id)

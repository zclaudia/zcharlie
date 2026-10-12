"""Native typed tools with trusted scope resolution and durable action receipts."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel
from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.budget import BudgetService
from investment_agent.agent.context import without_schema_titles
from investment_agent.agent.execution import ExecutionDeniedError, NoProgressError, check_boundary
from investment_agent.agent.job import SessionFactory
from investment_agent.agent.tasks import (
    ActionResult,
    ReconciliationRequiredError,
    TaskClaim,
    TaskService,
    fingerprint,
)
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef

FORBIDDEN = {"actor", "user_id", "owner", "owner_id", "operation_id", "authorization", "authorized_scope",
             "approved", "approval", "lease", "task_id", "attempt_id", "intent_key", "wait_token"}
ScopeResolver = Callable[[AsyncSession, BaseModel], Awaitable[tuple[ObjectRef, ...]]]
DatabaseTool = Callable[[AsyncSession, UUID, BaseModel], Awaitable[ActionResult | dict[str, Any]]]


class ExternalTool(Protocol):
    async def send(self, operation_id: UUID, parameters: BaseModel) -> dict[str, Any]: ...
    async def reconcile(self, operation_id: UUID, parameters: BaseModel) -> tuple[Literal["committed", "not_applied", "unknown"], dict[str, Any] | None]: ...


@dataclass(frozen=True)
class ToolRegistration:
    name: str
    parameters: type[BaseModel]
    scope: ScopeResolver
    database: DatabaseTool | None = None
    external: ExternalTool | None = None
    whole_watchlist: bool = False
    description: str | None = None
    ready: Callable[[AsyncSession, UUID], Awaitable[bool]] | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name) or self.name == "final_result":
            raise ValueError("invalid or framework-reserved tool name")
        if (self.database is None) == (self.external is None):
            raise ValueError("register exactly one database or external adapter")
        if self.parameters.model_config.get("extra") != "forbid":
            raise ValueError("tool business parameters must forbid extra fields")
        schema = self.parameters.model_json_schema()

        def validate(value: Any) -> None:
            if isinstance(value, dict):
                if FORBIDDEN.intersection(value.get("properties", {})):
                    raise ValueError("control fields cannot be model parameters")
                for child in value.values():
                    validate(child)
            elif isinstance(value, list):
                for child in value:
                    validate(child)
        validate(schema)


@dataclass(frozen=True)
class ExecutionContext:
    claim: TaskClaim
    sessions: SessionFactory
    base: Settings
    phase: str = "run:v1"  # AG13 may supply a trusted stable phase/version.


class TrustedTools:
    def __init__(self, context: ExecutionContext, registrations: tuple[ToolRegistration, ...]) -> None:
        self.context = context
        from investment_agent.agent.disclosure_tools import DisclosureExternal
        from investment_agent.agent.document_tools import DocumentExternal
        self.registrations = {r.name: replace(r, external=r.external.bind(context)) if isinstance(r.external, DisclosureExternal | DocumentExternal) else r for r in registrations}
        self.returned: set[str] = set()
        if len(self.registrations) != len(registrations):
            raise ValueError("duplicate tool registration")

    async def _check(self, session: AsyncSession, spec: ToolRegistration, business: BaseModel) -> None:
        """The single full authorization gate for one tool call (P2: once, not 3-5x)."""
        as_of = getattr(business, "as_of", None)
        if isinstance(as_of, datetime | date):
            task = await TaskService(session).get_task(self.context.claim.task_id)
            from investment_agent.agent.observation import effective_cutoff
            cutoff = await effective_cutoff(session, task.id)
            if isinstance(as_of, datetime):
                from investment_agent.investor.common import utc_timestamp
                too_late = utc_timestamp(as_of) > cutoff
            else:
                from zoneinfo import ZoneInfo
                too_late = as_of > cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
            if too_late:
                raise ExecutionDeniedError("tool_as_of_outside_task_binding")
        if spec.name in {"discover_disclosures", "fetch_disclosure_original"}:
            from investment_agent.agent.disclosure_tools import check_permission
            await check_permission(session, self.context, spec.name, business)
        if spec.name in {"parse_disclosure_original", "read_disclosure_original"}:
            from investment_agent.agent.document_tools import permission
            from investment_agent.evidence.documents import OriginalRequest
            assert isinstance(business, OriginalRequest)
            await permission(session, self.context, spec.name, business)
        refs = await spec.scope(session, business)
        await check_boundary(session, self.context.claim, self.context.base,
                             tool=spec.name, required_refs=refs, watchlist=spec.whole_watchlist)
        await BudgetService(session, self.context.base).check(self.context.claim)

    async def call(self, spec: ToolRegistration, business: BaseModel) -> dict[str, Any]:
        ctx = self.context
        parameters = {"tool": spec.name, "business": business.model_dump(mode="json"), "phase": ctx.phase}
        key = fingerprint(parameters)
        if ctx.phase == "research:v1" and key in self.returned:
            raise NoProgressError("no_progress_repeated_tool_parameters")
        # One full boundary check per tool call: authorization, scope, budget
        # and lease are validated once here. The settlement transactions still
        # fence the lease (cancellation/staleness) via CAS, so a revoked or
        # lost claim can never commit, without rescanning the whole policy.
        async with ctx.sessions() as session:
            await self._check(session, spec, business)
            action = await TaskService(session).prepare_action(ctx.claim,
                intent_key="tool:" + fingerprint(parameters), parameters=parameters,
                effect="external" if spec.external else "database")
            operation, state, saved = UUID(action.operation_id), action.state, action.result
        if state == "committed":
            assert saved is not None
            self.returned.add(key)
            return self._defer_if_pending(operation, saved)
        if state == "uncertain":
            raise ReconciliationRequiredError("external effect requires trusted provider reconciliation")
        async with ctx.sessions() as session:
            service = TaskService(session)
            await service.append_event(ctx.claim.task_id, ctx.claim.attempt_id, "tool_state",
                {"tool_call_id": str(operation), "tool_name": spec.name, "state": "started"})
            if spec.database is not None:
                async def write(owned: AsyncSession, op: UUID) -> ActionResult | dict[str, Any]:
                    assert spec.database is not None
                    return await spec.database(owned, op, business)
                result = await service.settle_action(ctx.claim, operation, write)
            else:
                action = await service._action(ctx.claim.task_id, operation)
                generation = await service.start_external(ctx.claim, operation, expected_revision=action.revision)
                result = None
        if result is None:
            assert spec.external is not None
            try:
                result = await spec.external.send(operation, business)
            except BaseException:
                async with ctx.sessions() as session:
                    await TaskService(session).reconcile_external(ctx.claim.task_id, operation,
                        expected_revision=generation, outcome="unknown")
                raise
            async with ctx.sessions() as session:
                await TaskService(session).reconcile_external(ctx.claim.task_id, operation,
                    expected_revision=generation, outcome="committed", result=result)
        async with ctx.sessions() as session:
            await TaskService(session).append_event(ctx.claim.task_id, ctx.claim.attempt_id, "tool_state",
                {"tool_call_id": str(operation), "tool_name": spec.name,
                 "state": "waiting" if result.get("status") == "pending" else "completed"})
        self.returned.add(key)
        return self._defer_if_pending(operation, result)

    @staticmethod
    def _defer_if_pending(operation: UUID, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("status") == "pending":
            # Dependency identity is application generated and persisted in the
            # action ledger, not copied from a provider tool_call_id.
            raise CallDeferred(metadata={"dependency_id": str(operation), "operation_id": str(operation),
                                         "provider_dependency": result.get("dependency_id")})
        return result

    async def recover(self) -> None:
        """Always read ledger first. Never blind replay an uncertain effect."""
        ctx = self.context
        async with ctx.sessions() as session:
            actions = await TaskService(session).recovery_actions(ctx.claim.task_id)
        for action in actions:
            if action.state != "uncertain":
                continue
            spec = self.registrations.get(action.parameters.get("tool", ""))
            if spec is None or spec.external is None:
                raise ReconciliationRequiredError("external adapter unavailable")
            business = spec.parameters.model_validate(action.parameters["business"])
            outcome, result = await spec.external.reconcile(UUID(action.operation_id), business)
            async with ctx.sessions() as session:
                await TaskService(session).reconcile_external(ctx.claim.task_id, UUID(action.operation_id),
                    expected_revision=action.revision, outcome=outcome, result=result)
            if outcome == "unknown":
                raise ReconciliationRequiredError("external outcome remains unknown")

    def toolset(self, allowed: tuple[str, ...], *, validation_retries: int = 0) -> FunctionToolset[ExecutionContext]:
        toolset: FunctionToolset[ExecutionContext] = FunctionToolset(max_retries=2)
        for name in allowed:
            spec = self.registrations.get(name)
            if self.context.phase != "research:v1" and name in {"get_portfolio_context", "calculate_portfolio_options", "prepare_portfolio_plan"}:
                continue
            if spec is None:
                if name in {"publish_recommendation", "extend_recommendation", "prepare_user_operation", "publish_portfolio_plan"}:
                    continue  # Trusted completion capabilities, never model tools.
                if self.context.phase != "research:v1" and name in {"save_research_progress", "prepare_company_view"}:
                    continue  # Preserve explicitly injected non-research handlers.
                raise ExecutionDeniedError("unregistered_tool")
            def build(spec: ToolRegistration) -> Callable[..., Awaitable[dict[str, Any]]]:
                async def call(ctx: RunContext[ExecutionContext], business: BaseModel) -> dict[str, Any]:
                    if ctx.deps is not self.context:
                        raise ExecutionDeniedError("invalid_trusted_context")
                    from pydantic_ai import ModelRetry

                    from investment_agent.agent.execution import ResearchValidationError
                    try:
                        result = await self.call(spec, business)
                        if spec.name == "save_research_progress" and "progress" in result:
                            # The model already supplied the full progress in
                            # its tool call. Do not echo it and its rendered copy
                            # into every subsequent request. The durable receipt
                            # remains complete for UI, review and recovery.
                            return {"status": "saved", "artifact_ref": result["artifact_ref"], "refs": result["refs"]}
                        return result
                    except ResearchValidationError as exc:
                        if exc.repairable and ctx.retry < min(validation_retries, 2):
                            raise ModelRetry(exc.repair_message()) from exc
                        raise
                call.__annotations__["business"] = spec.parameters
                return call
            def prepare(spec: ToolRegistration) -> Callable[[RunContext[ExecutionContext], ToolDefinition], Awaitable[ToolDefinition | None]]:
                async def available(ctx: RunContext[ExecutionContext], definition: ToolDefinition) -> ToolDefinition | None:
                    if ctx.deps is not self.context:
                        raise ExecutionDeniedError("invalid_trusted_context")
                    if self.context.phase == "research:v1":
                        async with self.context.sessions() as session:
                            headroom = await BudgetService(session, self.context.base).research_headroom(self.context.claim)
                        if headroom["finish_only"]:
                            return None
                    if spec.name in {"discover_disclosures", "fetch_disclosure_original", "parse_disclosure_original", "read_disclosure_original", "get_industry_peer_context"}:
                        from investment_agent.settings.loader import load_settings
                        async with self.context.sessions() as session:
                            selected = await load_settings(session, base=self.context.base)
                            if spec.name == "get_industry_peer_context":
                                if not selected.industry_peer_context_enabled:
                                    return None
                            elif not selected.disclosure_originals_enabled:
                                return None
                    if spec.ready is not None:
                        async with self.context.sessions() as session:
                            if not await spec.ready(session, self.context.claim.task_id):
                                return None
                    return replace(definition, parameters_json_schema=without_schema_titles(definition.parameters_json_schema))
                return available
            toolset.add_function(build(spec), name=name, description=spec.description, prepare=prepare(spec), sequential=True)
        return toolset

"""Trusted admission and live execution boundaries; model arguments are business data.

AG09 calls admit() after authenticating the message. AG13 provides a trusted
run specification/completion policy. Neither is inferred from model text.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.contracts import ContextBinding, Task
from investment_agent.agent.tasks import TaskBoundaryError, TaskClaim, TaskService
from investment_agent.config import Settings
from investment_agent.evidence.disclosures import DisclosurePermission
from investment_agent.investor.common import ObjectRef, utc_timestamp
from investment_agent.settings.loader import load_settings
from investment_agent.storage.models.agent import AgentTask, ExecutionGrantRow
from investment_agent.storage.models.investor import InvestorCase, MandateHead, MandateRevision


class ExecutionDeniedError(TaskBoundaryError):
    """Safe classification for a stopped execution boundary."""


class ResearchValidationError(ExecutionDeniedError):
    """Safe code/path only; never includes model text, reasoning or provider data."""
    def __init__(self, code: str, field: str, message: str) -> None:
        from investment_agent.investor.common import FieldError
        self.code = code
        self.field_errors = (FieldError(field=field, message=message),)
        super().__init__(code)

    @property
    def repairable(self) -> bool:
        # Only presentation/reference mistakes may reach the model as feedback.
        # Scope, permission, stale claims and budget failures remain terminal.
        return self.code in {
            "research_numeric_text_requires_reference",
            "research_reference_unavailable",
            "research_reference_validation_failed",
            "research_plan_is_not_final_answer",
        }

    def repair_message(self) -> str:
        return self.code + ": " + "; ".join(f"{e.field}: {e.message}" for e in self.field_errors)


class TimeBudgetExceededError(ExecutionDeniedError):
    pass


class NoProgressError(ExecutionDeniedError):
    pass


class ExecutionGrant(BaseModel):
    """Server-created, immutable task admission, never a model tool schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    owner: Literal["local_owner"] = "local_owner"
    mode: Literal["interactive", "background"]
    source: Literal["authenticated_message", "authorized_background"]
    prompt: str = Field(min_length=1)
    tool_names: tuple[str, ...] = ()
    scope_refs: tuple[ObjectRef, ...] = ()
    watchlist: bool = False
    memory_revision: int = Field(default=0, ge=0)
    memory_valid_until: datetime | None = None
    mandate_id: UUID | None = None
    expires_at: datetime | None = None
    task_cost_usd: Decimal | None = Field(default=None, ge=0)
    daily_cost_usd: Decimal | None = Field(default=None, ge=0)
    purpose: Literal["interactive", "review", "exploration"] | None = None
    current_research: bool = False
    research_kind: Literal["explanation", "local_check", "company_research", "comparison", "review", "portfolio_action"] = "local_check"
    disclosure_permissions: tuple["DisclosurePermission", ...] = ()
    portfolio_account_id: UUID | None = None
    portfolio_snapshot_ref: ObjectRef | None = None
    portfolio_valuation_ref: ObjectRef | None = None
    """The daily price basis this admission was bound to (plan 2.5). A
    calculation re-binds the current valuation explicitly - never silently."""
    peer_context: dict[str, Any] | None = None
    """Trusted industry-peer binding (target, cohort_ref, batch_run_id, year,
    cutoff, metric_calculation_refs) fixed at task admission; default null so
    grants serialized before the feature keep validating (WP1). WP11 owns the
    typed shape and the enforcement; never a model tool schema."""

    @field_validator("expires_at", "memory_valid_until")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        return utc_timestamp(value) if value is not None else None


def budget_purpose(grant: ExecutionGrant) -> str:
    # Existing immutable review admissions retain their trusted classification.
    return grant.purpose or ("review" if grant.research_kind == "review" else "interactive")


async def purpose_limit(session: AsyncSession, grant: ExecutionGrant) -> Decimal | None:
    purpose = budget_purpose(grant)
    if purpose == "interactive":
        return None
    if grant.mandate_id is None:
        if purpose == "exploration":
            raise ExecutionDeniedError("exploration_budget_unavailable")
        return None
    from investment_agent.investor.contracts import Mandate
    head = await session.get(MandateHead, grant.owner, populate_existing=True)
    if head is None or head.mandate_id != str(grant.mandate_id):
        raise ExecutionDeniedError("mandate_unavailable")
    row = await session.get(MandateRevision, (head.mandate_id, head.revision))
    assert row is not None
    auth = Mandate.model_validate(row.payload).authorization
    quantity = auth.exploration_daily_budget if purpose == "exploration" else auth.review_daily_budget
    # Legacy review remains within the existing total. Once exploration is
    # configured, review requires its own explicit allowance and cannot borrow.
    if quantity is None:
        if purpose == "exploration" or auth.exploration_daily_budget is not None:
            raise ExecutionDeniedError(purpose + "_budget_unavailable")
        return None
    if quantity.value is None:
        raise ExecutionDeniedError(purpose + "_budget_unavailable")
    currency = quantity.currency if quantity.unit == "currency" else quantity.unit
    if currency != "USD" or quantity.unit not in {"currency", "USD"} or quantity.currency not in {None, "USD"} or quantity.value < 0:
        raise ExecutionDeniedError("purpose_budget_requires_known_USD")
    return quantity.value


async def live_policy(
    session: AsyncSession, grant: ExecutionGrant, base: Settings
) -> Settings:
    settings = await load_settings(session, base=base)
    if not settings.investor_agent_enabled:
        raise ExecutionDeniedError("feature_disabled")
    if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
        raise ExecutionDeniedError("authorization_expired")
    if (grant.mode == "interactive") != (grant.source == "authenticated_message"):
        raise ExecutionDeniedError("invalid_authorization_source")
    if grant.mode == "background" and not settings.investor_agent_background_enabled:
        raise ExecutionDeniedError("background_disabled")
    if grant.mandate_id is not None or grant.mode == "background":
        head = await session.get(MandateHead, grant.owner, populate_existing=True)
        if head is None or head.mandate_id != str(grant.mandate_id):
            raise ExecutionDeniedError("mandate_unavailable")
        revision = await session.get(MandateRevision, (head.mandate_id, head.revision))
        if revision is None:
            raise ExecutionDeniedError("mandate_unavailable")
        from investment_agent.investor.contracts import Mandate

        mandate = Mandate.model_validate(revision.payload)
        scope = mandate.authorization.exploration_scope_refs if grant.mode == "background" else mandate.scope_refs
        # Compare identities, not old revisions; current permission is authoritative.
        allowed = await scope_identities(session, scope, grant.owner)
        requested = await scope_identities(session, grant.scope_refs, grant.owner)
        if not {identity for identity in requested if identity[0] not in {"case", "portfolio_snapshot", "portfolio_valuation"}} <= allowed:
            raise ExecutionDeniedError("scope_revoked")
        if grant.mode == "background" and not mandate.authorization.background_enabled:
            raise ExecutionDeniedError("background_disabled")
    if grant.research_kind == "portfolio_action":
        if not settings.portfolio_ledger_enabled or grant.portfolio_account_id is None or grant.portfolio_snapshot_ref is None:
            raise ExecutionDeniedError("portfolio_authorization_unavailable")
        from investment_agent.portfolio.service import PortfolioService
        snapshot = await PortfolioService(session, grant.owner).snapshot(grant.portfolio_account_id)
        if snapshot.reference != grant.portfolio_snapshot_ref:
            raise ExecutionDeniedError("actual_snapshot_changed_research_restart_required")
        if grant.portfolio_valuation_ref is not None:
            # The admission's frozen price basis must still belong to the
            # granted fact version; prices themselves may move on (a newer
            # valuation is a limitation marker, not a scope break).
            from investment_agent.portfolio.valuation import ValuationService
            valuation = await ValuationService(session, grant.owner).get(grant.portfolio_account_id, UUID(str(grant.portfolio_valuation_ref.id)))
            if valuation.snapshot_id != UUID(str(grant.portfolio_snapshot_ref.id)):
                raise ExecutionDeniedError("portfolio_valuation_snapshot_mismatch")
    scopes = await scope_identities(session, grant.scope_refs, grant.owner)
    listing_ids = {int(identity) for kind, identity in scopes if kind == "listing"}
    if grant.watchlist:
        from investment_agent.storage.models.user_domain import WatchlistItem
        members = (await session.scalars(select(WatchlistItem.listing_id).where(WatchlistItem.enabled.is_(True)))).all()
        if not set(members) <= listing_ids:
            raise ExecutionDeniedError("whole_watchlist_scope_changed")
    cases = (await session.scalars(select(InvestorCase).where(
        InvestorCase.owner_id == grant.owner, InvestorCase.listing_id.in_(listing_ids)
    ))).all()
    portfolio_risk_holdings = set()
    if grant.research_kind == "portfolio_action":
        portfolio_risk_holdings = {h.listing_ref.id for h in snapshot.holdings if h.confirmed_quantity is not None and h.confirmed_quantity > 0}
    for case in cases:
        if grant.mode == "interactive" and case.listing_id in portfolio_risk_holdings:
            continue  # Explicit held-account analysis; archived company views stay unusable.
        if case.payload["status"] == "archived":
            raise ExecutionDeniedError("scope_archived")
        if grant.mode == "background" and case.payload.get("background_paused"):
            raise ExecutionDeniedError("background_paused")
    await purpose_limit(session, grant)
    return settings


async def scope_identities(session: AsyncSession, refs: tuple[ObjectRef, ...], owner: str) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = {(r.kind, str(r.id)) for r in refs}
    for ref in refs:
        if ref.kind == "case":
            case = await session.get(InvestorCase, str(ref.id), populate_existing=True)
            if case is None or case.owner_id != owner:
                raise ExecutionDeniedError("scope_unavailable")
            identities.add(("listing", str(case.listing_id)))
    return identities


async def cost_limits(session: AsyncSession, grant: ExecutionGrant) -> tuple[Decimal | None, Decimal | None]:
    task, daily = grant.task_cost_usd, grant.daily_cost_usd
    if grant.mandate_id is not None:
        head = await session.get(MandateHead, grant.owner, populate_existing=True)
        assert head is not None
        revision = await session.get(MandateRevision, (head.mandate_id, head.revision))
        assert revision is not None
        from investment_agent.investor.contracts import Mandate
        auth = Mandate.model_validate(revision.payload).authorization
        caps = []
        for quantity in (auth.per_task_budget, auth.daily_budget):
            currency = quantity.currency
            if quantity.unit in {"USD", "CNY", "HKD", "EUR"}:
                if currency is not None and currency != quantity.unit:
                    raise ExecutionDeniedError("authorization_budget_currency_invalid")
                currency = quantity.unit
            elif quantity.unit != "currency":
                raise ExecutionDeniedError("authorization_budget_unit_invalid")
            if quantity.value is not None and currency != "USD":
                raise ExecutionDeniedError("verified_exchange_rate_unavailable")
            if quantity.value is not None and quantity.value < 0:
                raise ExecutionDeniedError("authorization_budget_invalid")
            caps.append(quantity.value)
        if grant.mode == "background" and any(cap is None for cap in caps):
            raise ExecutionDeniedError("background_budget_unavailable")
        task = min(cap for cap in (task, caps[0]) if cap is not None) if task is not None or caps[0] is not None else None
        daily = min(cap for cap in (daily, caps[1]) if cap is not None) if daily is not None or caps[1] is not None else None
    return task, daily



async def admit(
    session: AsyncSession, *, base: Settings, input_key: str, binding: ContextBinding,
    grant: ExecutionGrant, kind: str = "agent_turn", parent_task_id: UUID | None = None,
    message_ref: ObjectRef | None = None,
) -> Task:
    """Authenticated caller owns transaction with message + task + immutable grant.

    No public API accepts a grant, actor or operation id. AG09 must construct it
    from the authenticated source and currently authorized scope.
    """
    from investment_agent.investor.controls import memory_deadline, memory_revision
    grant = grant.model_copy(update={"purpose": budget_purpose(grant), "memory_revision": await memory_revision(session, grant.owner), "memory_valid_until": await memory_deadline(session, grant.owner)})
    await live_policy(session, grant, base)
    from investment_agent.agent.operations import admission
    existing_input = await session.scalar(select(AgentTask.id).where(AgentTask.owner_id == grant.owner, AgentTask.input_key == input_key))
    if existing_input is None and not await admission(session, "interactive" if grant.mode == "interactive" else "exploration" if budget_purpose(grant) == "exploration" else "review"):
        raise ExecutionDeniedError("queue_capacity")
    task = await TaskService(session, grant.owner).create(
        input_key=input_key, binding=binding, kind=kind, parent_task_id=parent_task_id,
        message_ref=message_ref, queue_class="interactive" if grant.mode == "interactive" else "exploration" if budget_purpose(grant) == "exploration" else "review"
    )
    existing = await session.get(ExecutionGrantRow, str(task.id))
    if existing is not None:
        previous = ExecutionGrant.model_validate(existing.payload)
        previous = previous.model_copy(update={"purpose": budget_purpose(previous)})
        if previous.model_dump(mode="json") != grant.model_dump(mode="json"):
            from investment_agent.storage.repositories.pipeline_run import (
                IdempotencyPayloadMismatchError,
            )
            raise IdempotencyPayloadMismatchError("task admission reused with different authorization")
    else:
        from investment_agent.investor.methods import MethodService
        await MethodService(session, grant.owner).freeze_task(task.id, grant.scope_refs, grant.research_kind)
        session.add(ExecutionGrantRow(task_id=str(task.id), payload=grant.model_dump(mode="json")))
        await session.flush()
    return task


async def check_boundary(
    session: AsyncSession, claim: TaskClaim, base: Settings, *, tool: str | None = None,
    required_refs: tuple[ObjectRef, ...] = (), watchlist: bool = False,
) -> tuple[ExecutionGrant, Settings]:
    row = await TaskService(session).fence(claim)
    saved = await session.get(ExecutionGrantRow, str(claim.task_id))
    if saved is None:
        raise ExecutionDeniedError("trusted_admission_required")
    grant = ExecutionGrant.model_validate(saved.payload)
    from investment_agent.investor.controls import UserControlService, memory_revision
    if grant.memory_revision != await memory_revision(session, grant.owner):
        raise ExecutionDeniedError("memory_context_changed_rebuild_required")
    if grant.memory_valid_until is not None and grant.memory_valid_until <= datetime.now(UTC):
        raise ExecutionDeniedError("memory_context_expired_rebuild_required")
    task = Task.model_validate(row.payload)
    # Observations saved within this run can enter tool-return history after
    # admission. Fence their natural expiry even though the producing task's
    # immutable grant predates those records.
    from investment_agent.investor.contracts import Memory
    from investment_agent.storage.models.investor import UserObject
    observations = await session.scalars(select(UserObject).where(UserObject.owner_id == grant.owner, UserObject.kind == "memory"))
    if any(memory.status == "active" and memory.actor == "agent" and memory.created_at >= task.created_at
        and memory.valid_until is not None and memory.valid_until <= datetime.now(UTC)
        for item in observations if (memory := Memory.model_validate(item.payload))):
        raise ExecutionDeniedError("memory_context_expired_rebuild_required")
    if task.message_ref is not None and await UserControlService(session)._blocked_source(task.message_ref):
        raise ExecutionDeniedError("memory_source_revoked")
    settings = await live_policy(session, grant, base)
    if tool is not None and tool not in grant.tool_names:
        raise ExecutionDeniedError("tool_not_authorized")
    identities = await scope_identities(session, grant.scope_refs, grant.owner)
    required = await scope_identities(session, required_refs, grant.owner)
    if not {identity for identity in required if identity[0] != "case"} <= identities:
        raise ExecutionDeniedError("tool_scope_not_authorized")
    if watchlist and not grant.watchlist:
        raise ExecutionDeniedError("whole_watchlist_authorization_required")
    created = Task.model_validate(row.payload).created_at
    if (datetime.now(UTC) - created).total_seconds() >= settings.investor_agent_task_seconds:
        raise TimeBudgetExceededError("time_budget_exhausted")
    return grant, settings

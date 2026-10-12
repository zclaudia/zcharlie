"""Atomic reservations and single settlement; RunUsage never charges this ledger.

Every method owns no commit. Call in independent short sessions. Fencing takes
SQLite's writer lock before summing owner/day and lifetime task receipts, so
separate connections cannot both spend the same remaining allowance.
"""

from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.contracts import Usage
from investment_agent.agent.execution import (
    ExecutionDeniedError,
    budget_purpose,
    check_boundary,
    cost_limits,
    purpose_limit,
)
from investment_agent.agent.tasks import TaskClaim, TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import Quantity
from investment_agent.storage.models.agent import BudgetEntry


class BudgetExceededError(ExecutionDeniedError):
    pass


class BudgetService:
    def __init__(self, session: AsyncSession, base: Settings) -> None:
        self.session = session
        self.base = base

    @staticmethod
    def day(now: datetime | None = None) -> str:
        return (now or datetime.now(UTC)).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()

    async def check(self, claim: TaskClaim, *, extra_tokens: int = 0, reserve: bool = False,
                    now: datetime | None = None, extra_cost: Decimal | None = None, extra_tools: int = 0) -> None:
        grant, settings = await check_boundary(self.session, claim, self.base)
        day = self.day(now)
        rows = (await self.session.scalars(select(BudgetEntry).where(
            BudgetEntry.owner_id == "local_owner",
            or_(BudgetEntry.task_id == str(claim.task_id), BudgetEntry.day == day),
            BudgetEntry.state != "released",
        ))).all()
        task = [r for r in rows if r.task_id == str(claim.task_id)]
        daily = [r for r in rows if r.day == day]
        if any(r.state == "unknown" for r in rows):
            raise BudgetExceededError("usage_unknown_reconciliation_required")

        def tokens(group: list[BudgetEntry]) -> int:
            return sum(r.estimated_tokens if r.state in {"reserved", "sent"} else
                       (r.input_tokens or 0) + (r.output_tokens or 0) for r in group)

        if (sum(r.calls for r in task if r.kind == "model") + int(reserve) > settings.investor_agent_task_requests or
            sum(r.calls for r in daily if r.kind == "model") + int(reserve) > settings.investor_agent_daily_requests or
            sum(r.calls for r in task if r.kind == "tool") + extra_tools > settings.investor_agent_task_tool_calls or
            sum(r.calls for r in daily if r.kind == "tool") + extra_tools > settings.investor_agent_daily_tool_calls or
            tokens(task) + extra_tokens > settings.investor_agent_task_tokens or
            tokens(daily) + extra_tokens > settings.investor_agent_daily_tokens):
            raise BudgetExceededError("budget_exhausted")

        task_cap, daily_cap = await cost_limits(self.session, grant)
        bucket = [r for r in daily if r.purpose == budget_purpose(grant)]
        bucket_cap = await purpose_limit(self.session, grant)
        for group, cap in ((task, task_cap), (daily, daily_cap), (bucket, bucket_cap)):
            if cap is None:
                continue
            costs = [r.estimated_cost_usd if r.state in {"reserved", "sent"} else r.cost_usd for r in group]
            if any(cost is None for cost in costs) or reserve and extra_cost is None:
                raise BudgetExceededError("cost_unknown_reconciliation_required")
            total = sum((cost for cost in costs if cost is not None), Decimal(0))
            if total + (extra_cost or Decimal(0)) > cap:
                raise BudgetExceededError("budget_exhausted")

    @staticmethod
    def headroom(research_kind: str, task: list[BudgetEntry], daily: list[BudgetEntry],
                 settings: Settings) -> dict[str, int | bool]:
        """Pure planning envelope shared by execution and the progress projection.

        Keep two conservative request allowances for synthesis and one correction.
        The estimate is the largest request already reserved, never tokens/byte
        guesses. Daily and task allowances both apply; no allowance is added or
        reset. Callers own row selection: lifetime task entries plus every
        Shanghai-day entry (reserved/sent/settled/unknown), released excluded.
        """
        def spent(group: list[BudgetEntry]) -> int:
            return sum(r.estimated_tokens if r.state != "settled" else
                       (r.input_tokens or 0) + (r.output_tokens or 0) for r in group)
        remaining_tokens = min(settings.investor_agent_task_tokens - spent(task),
                               settings.investor_agent_daily_tokens - spent(daily))
        remaining_requests = min(settings.investor_agent_task_requests - sum(r.calls for r in task if r.kind == "model"),
                                 settings.investor_agent_daily_requests - sum(r.calls for r in daily if r.kind == "model"))
        request_bound = max((r.estimated_tokens for r in task if r.kind == "model"), default=0)
        return {"remaining_tokens": remaining_tokens, "remaining_requests": remaining_requests,
                "conclusion_reserve": 2 * request_bound,
                "finish_only": research_kind in {"company_research", "review"} and (remaining_requests <= 2 or bool(request_bound and remaining_tokens < 2 * request_bound))}

    async def research_headroom(self, claim: TaskClaim) -> dict[str, int | bool]:
        """Planning envelope only. Atomic reserve/check remain the spending authority."""
        grant, settings = await check_boundary(self.session, claim, self.base)
        rows = (await self.session.scalars(select(BudgetEntry).where(
            BudgetEntry.owner_id == "local_owner", BudgetEntry.state != "released",
            or_(BudgetEntry.task_id == str(claim.task_id), BudgetEntry.day == self.day()),
        ))).all()
        return self.headroom(grant.research_kind,
            [r for r in rows if r.task_id == str(claim.task_id)],
            [r for r in rows if r.day == self.day()], settings)

    @staticmethod
    def prices(settings: Settings) -> tuple[Decimal | None, Decimal | None]:
        from investment_agent.agent.model import AgentModelConfig

        if (settings.investor_agent_price_version and settings.investor_agent_price_model == settings.agent_model and
            settings.investor_agent_price_config_version == AgentModelConfig.from_settings(settings, 0).version and
            settings.investor_agent_input_usd_per_million is not None and
            settings.investor_agent_output_usd_per_million is not None):
            return settings.investor_agent_input_usd_per_million, settings.investor_agent_output_usd_per_million
        return None, None

    async def reserve(self, claim: TaskClaim, *, estimated_tokens: int,
                      now: datetime | None = None, estimated_input: int | None = None, output_bound: int | None = None) -> UUID:
        if type(estimated_tokens) is not int or estimated_tokens <= 0:
            raise ValueError("positive input estimate plus finite output bound required")
        if (estimated_input is not None or output_bound is not None) and (
            type(estimated_input) is not int or type(output_bound) is not int or
            estimated_input < 0 or output_bound <= 0 or estimated_input + output_bound != estimated_tokens
        ):
            raise ValueError("input/output reservation split must match the total estimate")
        grant, settings = await check_boundary(self.session, claim, self.base)
        input_price, output_price = self.prices(settings)
        estimated_cost = None
        if input_price is not None and output_price is not None:
            # Direct callers may only have a total estimate: price it at the
            # more expensive side rather than guessing a split.
            estimated_cost = (Decimal(estimated_tokens) * max(input_price, output_price) if estimated_input is None or output_bound is None else
                Decimal(estimated_input) * input_price + Decimal(output_bound) * output_price) / Decimal(1000000)
        if estimated_cost is not None:
            estimated_cost = estimated_cost.quantize(Decimal(".000000000001"), rounding=ROUND_CEILING)
        await self.check(claim, extra_tokens=estimated_tokens, extra_cost=estimated_cost, reserve=True, now=now)
        reservation = BudgetEntry(id=str(uuid4()), task_id=str(claim.task_id),
            attempt_id=str(claim.attempt_id), owner_id="local_owner", day=self.day(now),
            state="reserved", kind="model", purpose=budget_purpose(grant), calls=1, estimated_tokens=estimated_tokens, estimated_cost_usd=estimated_cost,
            price_version=settings.investor_agent_price_version if estimated_cost is not None else None,
            input_price=input_price, output_price=output_price,
            input_tokens=None, output_tokens=None, cost_usd=None, unknown_reason=None)
        self.session.add(reservation)
        await self.session.flush()
        return UUID(reservation.id)

    async def sent(self, claim: TaskClaim, reservation: UUID) -> None:
        # The actual HTTP hook rechecks authorization immediately before sending.
        await self.check(claim)
        row = await self.session.get(BudgetEntry, str(reservation), populate_existing=True)
        if row is None or row.attempt_id != str(claim.attempt_id) or row.state != "reserved":
            raise ExecutionDeniedError("invalid_request_reservation")
        _, settings = await check_boundary(self.session, claim, self.base)
        prices = self.prices(settings)
        if (prices != (row.input_price, row.output_price) or
            row.price_version is not None and settings.investor_agent_price_version != row.price_version):
            raise ExecutionDeniedError("price_configuration_changed")
        row.state = "sent"
        await self.session.flush()

    async def _record_usage(self, task_id: str, attempt_id: str) -> Usage:
        """Publish the task's current usage total after a ledger change."""
        total = await self.usage(UUID(task_id))
        service = TaskService(self.session)
        task_row = await service._row(UUID(task_id))
        task = await service.get_task(UUID(task_id))
        await service._save(task_row, task.model_copy(update={"usage": total}))
        await service.append_event(UUID(task_id), UUID(attempt_id), "usage", total.model_dump(mode="json"))
        return total

    async def reconcile(self, entry_id: UUID, *, action: Literal["settle", "release"],
                        operator: str, reason: str, usage: dict[str, int] | None = None) -> BudgetEntry:
        """Trusted operator exit for fail-closed unknown entries.

        Unknown usage never recovers by itself: an operator either settles the
        real provider-reported usage (from the provider console) or releases a
        request that provably never reached the provider. Both leave a durable
        audit event with the operator and reason on the owning task. Any other
        state is refused, so the default stays fail-closed.
        """
        if action not in {"settle", "release"}:
            raise ValueError("reconciliation action must be settle or release")
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required for the audit trail")
        if action == "settle" and usage is None:
            raise ValueError("settling requires the actual provider usage")
        # Acquire a writer lock before reading the mutable row.
        await self.session.execute(update(BudgetEntry).where(BudgetEntry.id == str(entry_id))
                                   .values(state=BudgetEntry.state))
        row = await self.session.get(BudgetEntry, str(entry_id), populate_existing=True)
        if row is None:
            raise LookupError("budget entry not found")
        if row.state != "unknown":
            raise ValueError("only unknown entries can be reconciled")
        if action == "release":
            row.state = "released"
        else:
            provider_usage = usage or {}
            if set(provider_usage) != {"input_tokens", "output_tokens"} or any(type(v) is not int or v < 0 for v in provider_usage.values()):
                raise ValueError("invalid actual usage")
            row.state = "settled"
            row.input_tokens, row.output_tokens = provider_usage["input_tokens"], provider_usage["output_tokens"]
            if row.input_price is not None and row.output_price is not None:
                row.cost_usd = ((Decimal(row.input_tokens) * row.input_price + Decimal(row.output_tokens) * row.output_price) / Decimal(1000000)).quantize(Decimal(".000000000001"), rounding=ROUND_CEILING)
        await self.session.flush()
        await self._record_usage(row.task_id, row.attempt_id)
        await TaskService(self.session).append_event(UUID(row.task_id), UUID(row.attempt_id), "progress",
            {"summary": f"Unknown usage entry {row.id} {action}d by {operator}: {reason}"[:500]})
        return row

    async def settle(self, reservation: UUID, usage: dict[str, int] | None, *, sent: bool,
                     reason: str = "provider_usage_missing_or_incomplete") -> Usage:
        # Never fence expenses: revoked/cancelled/in-flight attempts still owe
        # their actual charge. Acquire a writer lock before reading mutable rows.
        await self.session.execute(update(BudgetEntry).where(BudgetEntry.id == str(reservation))
                                   .values(state=BudgetEntry.state))
        row = await self.session.get(BudgetEntry, str(reservation), populate_existing=True)
        if row is None:
            raise LookupError("reservation not found")
        if row.state not in {"reserved", "sent"}:
            if row.state == "unknown" and usage is None or row.state == "settled" and usage == {
                "input_tokens": row.input_tokens, "output_tokens": row.output_tokens
            } or row.state == "released" and not sent:
                return await self.usage(UUID(row.task_id))
            raise ValueError("settlement is immutable")
        if not sent:
            row.state = "released"
        elif usage is None:
            row.state = "unknown"
            row.unknown_reason = reason
        else:
            if set(usage) != {"input_tokens", "output_tokens"} or any(type(v) is not int or v < 0 for v in usage.values()):
                raise ValueError("invalid actual usage")
            row.state = "settled"
            row.input_tokens, row.output_tokens = usage["input_tokens"], usage["output_tokens"]
            if row.input_price is not None and row.output_price is not None:
                row.cost_usd = ((Decimal(row.input_tokens) * row.input_price + Decimal(row.output_tokens) * row.output_price) / Decimal(1000000)).quantize(Decimal(".000000000001"), rounding=ROUND_CEILING)
        await self.session.flush()
        return await self._record_usage(row.task_id, row.attempt_id)

    async def usage(self, task_id: UUID) -> Usage:
        rows = (await self.session.scalars(select(BudgetEntry).where(
            BudgetEntry.task_id == str(task_id), BudgetEntry.state != "released"
        ))).all()
        unknown = any(r.state != "settled" for r in rows)
        costs = [r.cost_usd for r in rows]
        known_cost = all(cost is not None for cost in costs) and not unknown
        return Usage(requests=sum(r.calls for r in rows if r.kind == "model"),
                     tool_calls=sum(r.calls for r in rows if r.kind == "tool"), input_tokens=None if unknown else sum(r.input_tokens or 0 for r in rows),
                     output_tokens=None if unknown else sum(r.output_tokens or 0 for r in rows),
                     cost=Quantity(value=sum((cost for cost in costs if cost is not None), Decimal(0)) if known_cost else None,
                                   unit="currency", currency="USD",
                                   unknown_reason=None if known_cost else "price_or_usage_unknown"))

    async def observe_tools(self, claim: TaskClaim, count: int) -> None:
        """Charge requested business-tool attempts before native validation/dispatch.

        Includes malformed, duplicate and unauthorized requests. Framework
        validation retries therefore cannot reset the application tool budget.
        An oversized batch stops without dispatching any of its tools, while
        retaining the already-observed model charge and attempted tool count.
        """
        if count <= 0:
            return
        # Record attempts even if cancellation/configuration changed while the
        # model was in flight. Like response usage, this is an observation, not
        # authorization to execute the requested action.
        await self.session.execute(update(BudgetEntry).where(BudgetEntry.task_id == str(claim.task_id))
                                   .values(state=BudgetEntry.state))
        from investment_agent.agent.execution import ExecutionGrant
        from investment_agent.storage.models.agent import ExecutionGrantRow
        saved = await self.session.get(ExecutionGrantRow, str(claim.task_id))
        assert saved is not None
        row = BudgetEntry(id=str(uuid4()), task_id=str(claim.task_id), attempt_id=str(claim.attempt_id),
            owner_id="local_owner", day=self.day(), state="settled", kind="tool", purpose=budget_purpose(ExecutionGrant.model_validate(saved.payload)), calls=count,
            estimated_tokens=0, estimated_cost_usd=Decimal(0), price_version=None,
            input_price=None, output_price=None, input_tokens=0, output_tokens=0,
            cost_usd=Decimal(0), unknown_reason=None)
        self.session.add(row)
        await self.session.flush()
        await self._record_usage(str(claim.task_id), str(claim.attempt_id))

    async def recover(self, claim: TaskClaim) -> None:
        await TaskService(self.session).fence(claim)
        abandoned = (await self.session.scalars(select(BudgetEntry).where(
            BudgetEntry.task_id == str(claim.task_id), BudgetEntry.attempt_id != str(claim.attempt_id),
            BudgetEntry.state.in_(["reserved", "sent"])
        ))).all()
        for row in abandoned:
            await self.settle(UUID(row.id), None, sent=row.state == "sent", reason="abandoned_request_usage_unknown")

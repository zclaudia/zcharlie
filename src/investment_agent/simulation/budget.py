"""Durable per-request simulation USD admission with conservative uncertainty.

Reservations commit before a provider request. Actual provider usage can exceed
an estimate: record that overrun and stop, never rewrite charges to fit a cap.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.simulation.contracts import AccountScope, BudgetConfig, CostRecord
from investment_agent.simulation.errors import SimulationError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.simulation import SimulationCostRecord as CostRow
from investment_agent.storage.models.simulation import SimulationParticipantRecord as ParticipantRow

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def priced_usage(config: BudgetConfig, input_tokens: int, output_tokens: int) -> Decimal | None:
    if (
        type(input_tokens) is not int
        or type(output_tokens) is not int
        or min(input_tokens, output_tokens) < 0
    ):
        raise ValueError("invalid provider token usage")
    if config.input_price_per_million is None or config.output_price_per_million is None:
        return None
    with localcontext() as context:
        context.prec = 100
        return (
            Decimal(input_tokens) * config.input_price_per_million
            + Decimal(output_tokens) * config.output_price_per_million
        ) / Decimal(1_000_000)


class SimulationBudget:
    """Implements agent.runtime.RequestLifecycle without a long DB transaction."""

    def __init__(
        self,
        *,
        scope: AccountScope,
        round_id: str,
        config: BudgetConfig,
        model_ref: str,
        guard: Callable[[], Awaitable[None]],
        sessions: SessionFactory = session_scope,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        purpose: str = "decision",
        output_tokens_bound: int | None = None,
    ) -> None:
        self.scope, self.round_id, self.config, self.model_ref = scope, round_id, config, model_ref
        self.guard, self.sessions, self.now = guard, sessions, now
        self.purpose = purpose
        self.output_tokens_bound = output_tokens_bound
        self.stop_reason: str | None = None
        self.overrun = False
        self.repairs = 0
        self.reservations: list[UUID] = []

    def validation_retry(self, tool_name: str | None) -> None:
        self.repairs += 1
        if self.repairs > self.config.format_repair_attempts:
            self.stop_reason = "invalid_decision"
            raise SimulationError("invalid_decision", "format repair allowance exhausted")

    async def observe_tools(self, count: int) -> None:
        if count:
            raise SimulationError("scope_mismatch", "simulation runtime has no business tools")

    async def _check_guard(self) -> None:
        try:
            await self.guard()
        except SimulationError as error:
            self.stop_reason = error.code
            raise
        except ValueError:
            self.stop_reason = "invalid_config"
            raise

    async def reserve(self, estimated_input: int) -> UUID:
        if type(estimated_input) is not int or estimated_input <= 0:
            raise ValueError("positive request input estimate required")
        await self._check_guard()
        async with self.sessions() as session:
            # Acquire the writer before reading totals (SQLite's FOR UPDATE is
            # a no-op). The frozen value is unchanged; there is no semantic edit.
            locked = await session.execute(
                update(ParticipantRow)
                .where(
                    ParticipantRow.id == self.scope.participant_id,
                    ParticipantRow.experiment_id == self.scope.experiment_id,
                    ParticipantRow.account_id == self.scope.account_id,
                )
                .values(name=ParticipantRow.name)
                .returning(ParticipantRow.id)
            )
            if locked.scalar_one_or_none() is None:
                raise SimulationError("scope_mismatch", "budget participant scope changed")
            rows = (
                await session.scalars(
                    select(CostRow).where(CostRow.participant_id == self.scope.participant_id)
                )
            ).all()
            costs = [CostRecord.model_validate(row.payload) for row in rows]
            if any(
                cost.experiment_id != self.scope.experiment_id
                or cost.account_id != self.scope.account_id
                for cost in costs
            ):
                raise SimulationError(
                    "scope_mismatch", "cost record scope differs from participant"
                )
            current = [cost for cost in costs if cost.round_id == self.round_id]
            if any(cost.status in {"reserved", "unknown"} for cost in current):
                self.stop_reason = "unknown_call"
                raise SimulationError("unknown_call", "an earlier request has an uncertain outcome")
            reserved = self.config.conservative_call_reservation
            if self.output_tokens_bound is not None:
                if type(self.output_tokens_bound) is not int or self.output_tokens_bound <= 0:
                    raise ValueError("positive request output bound required")
                priced_bound = priced_usage(self.config, estimated_input, self.output_tokens_bound)
                if priced_bound is not None:
                    reserved = max(reserved, priced_bound)
            with localcontext() as context:
                context.prec = 100
                round_exceeded = (
                    sum((cost.amount for cost in current), Decimal(0)) + reserved
                    > self.config.round_cost_limit
                )
                season_exceeded = (
                    sum((cost.amount for cost in costs), Decimal(0)) + reserved
                    > self.config.season_cost_limit
                )
            if (
                len(current) >= self.config.max_calls_per_round
                or round_exceeded
                or season_exceeded
                or self.overrun
            ):
                self.stop_reason = "budget_exhausted"
                raise SimulationError(
                    "budget_exhausted", "simulation monetary or request allowance exhausted"
                )
            identity = uuid4()
            record = CostRecord(
                **self.scope.model_dump(),
                id=str(identity),
                round_id=self.round_id,
                call_key=f"{self.purpose}:{identity}",
                model_ref=self.model_ref,
                status="reserved",
                amount=reserved,
                estimated=True,
                currency="USD",
                occurred_at=self.now(),
            )
            session.add(
                CostRow(
                    id=record.id,
                    **self.scope.model_dump(),
                    round_id=self.round_id,
                    call_key=record.call_key,
                    status=record.status,
                    payload=record.model_dump(mode="json"),
                )
            )
            await session.flush()
        self.reservations.append(identity)
        return identity

    async def _row(self, session: AsyncSession, identity: UUID) -> tuple[CostRow, CostRecord]:
        row = await session.get(CostRow, str(identity), populate_existing=True)
        if row is None:
            raise SimulationError("not_found", "cost reservation missing")
        cost = CostRecord.model_validate(row.payload)
        if any(
            getattr(row, key) != getattr(cost, key)
            for key in (
                "id",
                "experiment_id",
                "participant_id",
                "account_id",
                "round_id",
                "call_key",
                "status",
            )
        ):
            raise SimulationError(
                "reconciliation_failed", "cost indexed identity differs from payload"
            )
        if any(getattr(cost, key) != getattr(self.scope, key) for key in AccountScope.model_fields):
            raise SimulationError("scope_mismatch", "cost reservation scope differs")
        if cost.round_id != self.round_id or cost.model_ref != self.model_ref:
            raise SimulationError("scope_mismatch", "cost reservation request binding differs")
        return row, cost

    async def _fence(self, session: AsyncSession, identity: UUID) -> None:
        # Serialize state checks as well as reservations. Otherwise two send
        # hooks can both observe reserved, or two settlements overwrite cost.
        await session.execute(update(CostRow).where(CostRow.id == str(identity)).values(status=CostRow.status))

    async def sent(self, reservation: UUID) -> None:
        await self._check_guard()
        async with self.sessions() as session:
            await self._fence(session, reservation)
            row, cost = await self._row(session, reservation)
            if cost.status != "reserved":
                raise SimulationError("unknown_call", "request reservation was already used")
            row.status = "unknown"
            row.payload = cost.model_copy(update={"status": "unknown"}).model_dump(mode="json")
            await session.flush()
        # The writer reservation may have waited behind a pause/end request.
        # Recheck after committing, immediately before handing off to HTTP.
        await self._check_guard()

    async def settle(self, reservation: UUID, usage: dict[str, int] | None, *, sent: bool) -> None:
        async with self.sessions() as session:
            await self._fence(session, reservation)
            row, cost = await self._row(session, reservation)
            if cost.status in {"settled", "failed"}:
                return
            actual = (
                priced_usage(self.config, usage["input_tokens"], usage["output_tokens"])
                if usage is not None
                else None
            )
            if not sent:
                amount, status = Decimal(0), "failed"
            elif usage is None:
                amount, status = cost.amount, "unknown"
            elif actual is None:
                # A returned response with known usage is a completed call.
                # Missing tariffs mean a conservative amount estimate, not
                # ambiguous delivery; bounded repair/review may proceed.
                amount, status = cost.amount, "settled"
            else:
                amount, status = actual, "settled"
                self.overrun |= actual > cost.amount
            updated = CostRecord.model_validate(
                cost.model_dump()
                | {
                    "status": status,
                    "amount": amount,
                    "estimated": sent and actual is None,
                    "input_tokens": usage["input_tokens"] if usage else 0,
                    "output_tokens": usage["output_tokens"] if usage else 0,
                }
            )
            row.status, row.payload = updated.status, updated.model_dump(mode="json")
            await session.flush()

    async def costs(self) -> tuple[CostRecord, ...]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(CostRow).where(
                        CostRow.participant_id == self.scope.participant_id,
                        CostRow.round_id == self.round_id,
                    )
                )
            ).all()
            return tuple(CostRecord.model_validate(row.payload) for row in rows)

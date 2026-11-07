"""Persistent experiment wakes, bounded free-source collection and native jobs."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from pydantic import Field
from pydantic_ai.models import Model
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.config import Settings
from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.providers.baostock_market import (
    BaostockMarketClient,
    DailyBarSeries,
    DividendYear,
    QuarterlyFinancials,
    StockBasic,
)
from investment_agent.scheduler.trading_calendar import TradingCalendar, load_trading_calendar
from investment_agent.settings.loader import configured_calendar_path, resolved_calendar_path
from investment_agent.simulation.artifacts import ArtifactStore
from investment_agent.simulation.budget import SessionFactory
from investment_agent.simulation.context import build_common_manifest
from investment_agent.simulation.contracts import Decision
from investment_agent.simulation.data import (
    ExperimentCollector,
    collect_experiment_data,
    content_hash,
    next_trading_day,
    validate_listing_scope,
)
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.risk import stable_id
from investment_agent.simulation.runtime import _check_worker_lease, decide_participant
from investment_agent.simulation.service import SimulationService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.simulation import SimulationCostRecord as CostRow
from investment_agent.storage.models.simulation import (
    SimulationExperimentRecord as ExperimentRow,
)
from investment_agent.storage.models.simulation import (
    SimulationOrderRecord as OrderRow,
)
from investment_agent.storage.models.simulation import (
    SimulationRequestRecord as RequestRow,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

SimulationKind = Literal["simulation_cycle", "simulation_decide", "simulation_review"]
_DEFERRED: ContextVar[bool] = ContextVar("simulation_job_deferred", default=False)
SIMULATION_KINDS: tuple[SimulationKind, ...] = (
    "simulation_cycle",
    "simulation_decide",
    "simulation_review",
)


class SimulationJobPayload(Contract):
    experiment_id: str = Field(min_length=1, max_length=160)
    round_id: str | None = None
    participant_id: str | None = None
    force_collection: bool = False


def simulation_calendar(settings: Settings) -> TradingCalendar:
    path = resolved_calendar_path(configured_calendar_path(), data_dir=settings.data_dir)
    return load_trading_calendar(path)


async def wake_experiment(
    session: AsyncSession,
    *,
    experiment_id: str,
    now: datetime | None = None,
    force_collection: bool = False,
) -> tuple[PipelineRun, bool]:
    current = utc_timestamp(now or datetime.now(UTC))
    experiment = await session.get(ExperimentRow, experiment_id, populate_existing=True)
    if experiment is None:
        raise SimulationError("not_found", "simulation experiment not found")
    outstanding = await session.scalar(
        select(OrderRow.id)
        .where(OrderRow.experiment_id == experiment_id, OrderRow.status == "locked")
        .limit(1)
    )
    if (
        experiment.status not in {"running", "paused"}
        and not outstanding
        and not (force_collection and experiment.status == "draft")
    ):
        raise SimulationError("invalid_transition", "experiment has no active simulation work")
    payload = SimulationJobPayload(experiment_id=experiment_id, force_collection=force_collection)
    return await PipelineRunRepository(session).enqueue(
        kind="simulation_cycle",
        idempotency_key=f"sim:{experiment_id}:{current.strftime('%Y%m%d%H%M')}:{int(force_collection)}",
        triggered_by="simulation",
        request_json=payload.model_dump(mode="json"),
        max_attempts=1,
    )


async def wake_active_experiments(session: AsyncSession, *, now: datetime | None = None) -> int:
    candidates = (
        await session.scalars(
            select(ExperimentRow.id).where(
                ExperimentRow.status.in_(("running", "paused"))
                | ExperimentRow.id.in_(
                    select(OrderRow.experiment_id).where(OrderRow.status == "locked")
                )
            )
        )
    ).all()
    return sum(
        [
            int((await wake_experiment(session, experiment_id=identity, now=now))[1])
            for identity in candidates
        ]
    )


@asynccontextmanager
async def baostock_collector() -> AsyncIterator[ExperimentCollector]:
    client = BaostockMarketClient()
    async with client.session():
        yield client


def financial_periods(day: date) -> tuple[tuple[int, int], ...]:
    quarter = (day.month - 1) // 3 + 1
    year = day.year
    periods = []
    for _ in range(4):
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
        periods.append((year, quarter))
    return tuple(periods)


@dataclass
class _StagedCollector:
    """Network answers staged before any database writes begin."""

    basics: dict[str, StockBasic | None] = field(default_factory=dict)
    bars: dict[tuple[str, date, date], DailyBarSeries] = field(default_factory=dict)
    dividends: dict[tuple[str, int], DividendYear] = field(default_factory=dict)
    financials: dict[tuple[str, int, int], QuarterlyFinancials | None] = field(default_factory=dict)

    async def fetch_stock_basic(self, symbol: str) -> StockBasic | None:
        return self.basics[symbol]

    async def fetch_daily_bars(self, symbol: str, start: date, end: date, adjust: Literal["none"]) -> DailyBarSeries:
        return self.bars[(symbol, start, end)]

    async def fetch_dividends(self, symbol: str, year: int) -> DividendYear:
        return self.dividends[(symbol, year)]

    async def fetch_quarterly_financials(self, symbol: str, year: int, quarter: int) -> QuarterlyFinancials | None:
        return self.financials[(symbol, year, quarter)]


async def _stage_collection(client: ExperimentCollector, tickers: list[str], start: date, end: date) -> _StagedCollector:
    staged = _StagedCollector()
    for ticker in tickers:
        staged.basics[ticker] = await client.fetch_stock_basic(ticker)
        staged.bars[(ticker, start, end)] = await client.fetch_daily_bars(ticker, start, end, "none")
        for year in range(start.year, end.year + 1):
            staged.dividends[(ticker, year)] = await client.fetch_dividends(ticker, year)
        for year, quarter in financial_periods(end):
            staged.financials[(ticker, year, quarter)] = await client.fetch_quarterly_financials(ticker, year, quarter)
    return staged


async def cycle_experiment(
    experiment_id: str,
    *,
    settings: Settings,
    calendar: TradingCalendar,
    sessions: SessionFactory = session_scope,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    collector_factory: Callable[
        [], AbstractAsyncContextManager[ExperimentCollector]
    ] = baostock_collector,
    force_collection: bool = False,
    require_financials: bool = True,
    pipeline_run_id: int | None = None,
) -> tuple[str, ...]:
    """Resume existing economics first; create only today's genuine forward round."""

    def service(session: AsyncSession) -> SimulationService:
        return SimulationService(session, data_dir=settings.data_dir, calendar=calendar, now=now)

    @asynccontextmanager
    async def checked_sessions() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            await _check_worker_lease(session, pipeline_run_id)
            yield session

    async with checked_sessions() as session:
        svc = service(session)
        experiment = await svc.get(experiment_id)
        rounds = await svc.rounds(experiment_id)
    current = utc_timestamp(now())
    local = current.astimezone(SHANGHAI_TZ)
    today = local.date()
    actions = []
    # Deadline enforcement happens regardless of market data or collection.
    for item in rounds:
        if item.status == "collecting_decisions" and current >= item.decision_deadline:
            async with checked_sessions() as session:
                await service(session).close_round(experiment_id, item.id)
            actions.append("closed")
    trading = calendar.is_trading_day(today)
    post_close = trading and local.time() >= time(15, 5)
    collection_day = today
    if force_collection and not post_close:
        for offset in range(1, 367):
            candidate = today - timedelta(days=offset)
            if calendar.is_trading_day(candidate):
                collection_day = candidate
                break
        else:
            raise SimulationError("incomplete_data", "reviewed closed collection session not found")
    if post_close or force_collection:
        # Recapture only while recovering data or preparing today's new round;
        # shared market tables must not grow a new version every scheduler poll.
        collection_scope = f"sim-collect:{experiment_id}:{collection_day}"
        capture_key = f"{local.date()}:{local.hour}:{local.minute // 30}"
        async with checked_sessions() as session:
            await session.execute(update(ExperimentRow).where(ExperimentRow.id == experiment_id)
                .values(revision=ExperimentRow.revision))
            captures = (
                await session.scalars(
                    select(RequestRow).where(RequestRow.scope == collection_scope)
                )
            ).all()
            already = any(row.idempotency_key == capture_key for row in captures)
            automatic_complete = any(row.payload.get("automatic_data_complete") for row in captures)
            fresh_rounds = await service(session).rounds(experiment_id)
            needs_data = (
                force_collection
                or not any(
                    r.observation_date == today
                    and r.status in {"collecting_decisions", "locked", "settled"}
                    for r in fresh_rounds
                )
                or any(
                    r.status in {"locked", "awaiting_execution_data"} and r.execution_date <= today
                    for r in fresh_rounds
                )
            )
            collect = not already and needs_data and (force_collection or not automatic_complete)
            if collect:
                session.add(RequestRow(scope=collection_scope, idempotency_key=capture_key,
                    input_hash="0" * 64, result_id="collecting",
                    payload={"automatic_data_complete": False, "status": "collecting"}))
                await session.flush()
        if collect:
            starts = [
                r.execution_date
                for r in fresh_rounds
                if r.status in {"locked", "awaiting_execution_data", "blocked"}
                and r.execution_date <= today
            ]
            # Bound cold-start research and turnover history. It is captured
            # now; strict PIT readers never pretend it was acquired earlier.
            start = min([collection_day - timedelta(days=60), *starts])
            artifacts = ArtifactStore(settings.data_dir)
            async with checked_sessions() as session:
                listing_tickers = await validate_listing_scope(session, experiment.config.universe)
            async with collector_factory() as collector:
                staged = await _stage_collection(collector, list(listing_tickers.values()), start, collection_day)
            # Only in-memory answers and local artifact writes occur below.
            # Provider awaits above hold no open database transaction.
            async with checked_sessions() as session:
                await collect_experiment_data(
                    session,
                    listing_ids=experiment.config.universe,
                    start=start,
                    end=collection_day,
                    client=staged,
                    archive=lambda name, payload: artifacts.put({"name": name, "data": payload})[0],
                    financial_periods=financial_periods(collection_day),
                )
                automatic_facts = await build_common_manifest(
                    session,
                    listing_ids=experiment.config.universe,
                    observation_date=collection_day,
                    cutoff=utc_timestamp(now()),
                    archive=artifacts.archive,
                    require_financials=require_financials,
                )
                automatic_missing = list(automatic_facts.manifest.missing_reasons)
                for listing_id, ticker in listing_tickers.items():
                    current_bar = next((bar for bar in staged.bars[(ticker, start, collection_day)].bars
                                        if bar.trade_date == collection_day), None)
                    if (current_bar is not None and current_bar.amount is None
                            and not (current_bar.trade_status == 0 and current_bar.volume == 0)):
                        automatic_missing.append(f"historical_turnover_missing:{listing_id}")
                execution_days = {r.execution_date for r in fresh_rounds
                    if r.status in {"locked", "awaiting_execution_data"} and r.execution_date <= collection_day}
                for execution_day in sorted(execution_days - {collection_day}):
                    execution_facts = await build_common_manifest(session,
                        listing_ids=experiment.config.universe, observation_date=execution_day,
                        cutoff=utc_timestamp(now()), archive=artifacts.archive, require_financials=False)
                    automatic_missing.extend(execution_facts.manifest.missing_reasons)
                receipt = await session.get(RequestRow, (collection_scope, capture_key))
                assert receipt is not None
                receipt.result_id = "collected"
                receipt.payload = {"listing_ids": list(experiment.config.universe),
                    "status": "collected", "automatic_data_complete": not automatic_missing,
                    "missing": sorted(set(automatic_missing))}
            actions.append("collected")
    current = utc_timestamp(now())
    async with checked_sessions() as session:
        rounds = await service(session).rounds(experiment_id)
    for item in rounds:
        if (
            item.status in {"locked", "awaiting_execution_data", "blocked"}
            and current >= item.execution_at
        ):
            try:
                async with checked_sessions() as session:
                    await service(session).settle_round(experiment_id, item.id)
                actions.append("settled_or_waiting")
            except SimulationError as error:
                if error.code != "invalid_transition":
                    raise
                # A predecision blocked round has no submitted economics.
    if post_close:
        async with checked_sessions() as session:
            await service(session).value(experiment_id, today)
        actions.append("valued")
    # Review maturity is independent of whether a new daily/weekly round is
    # due. Paused/end processing still writes deterministic observations; the
    # review runtime refuses new model calls unless the experiment is running.
    async with checked_sessions() as session:
        await PipelineRunRepository(session).enqueue(
            kind="simulation_review",
            idempotency_key=f"sim-review:{experiment_id}:{today}:{local.hour}:{local.minute // 30}",
            triggered_by="simulation",
            request_json=SimulationJobPayload(experiment_id=experiment_id).model_dump(mode="json"),
            max_attempts=1,
        )
        svc = service(session)
        experiment = await svc.get(experiment_id)
        rounds = await svc.rounds(experiment_id)
    if experiment.status != "running":
        return tuple(actions)
    if today > experiment.config.end_date:
        async with checked_sessions() as session:
            await service(session).end(
                experiment_id,
                expected_revision=experiment.revision,
                idempotency_key=f"season-end:{today}",
            )
        return (*actions, "ended")
    if not post_close or today < experiment.config.start_date:
        return tuple(actions)
    previous_week = any(
        r.observation_date.isocalendar()[:2] == today.isocalendar()[:2]
        and r.observation_date != today
        and r.status not in {"skipped", "cancelled"}
        for r in rounds
    )
    if experiment.config.rebalance_frequency == "weekly" and previous_week:
        return tuple(actions)
    existing = next((r for r in rounds if r.observation_date == today), None)
    if existing is None or existing.status in {"waiting_data", "blocked"}:
        # Fixed exchange deadline, not a fresh timer extended by each poll.
        execution_day = next_trading_day(calendar, today)
        deadline = datetime.combine(execution_day, time(9, 0), SHANGHAI_TZ)
        if current >= deadline:
            return (*actions, "missed_window")
        cutoff = utc_timestamp(now())
        async with checked_sessions() as session:
            try:
                item = await service(session).prepare_round(
                    experiment_id,
                    observation_date=today,
                    cutoff=cutoff,
                    decision_deadline=deadline,
                    idempotency_key=f"cycle:{today}:{cutoff.isoformat()}",
                    require_financials=require_financials,
                )
            except SimulationError as error:
                if error.code != "settlement_blocked":
                    raise
                return (*actions, "waiting_settlement")
        actions.append(item.status)
    else:
        item = existing
    if item.status == "collecting_decisions" and utc_timestamp(now()) < item.decision_deadline:
        async with checked_sessions() as session:
            participants = await service(session).participants(experiment_id)
            for participant in participants:
                if participant.config.kind != "agent":
                    continue
                payload = SimulationJobPayload(
                    experiment_id=experiment_id, round_id=item.id, participant_id=participant.id
                )
                await PipelineRunRepository(session).enqueue(
                    kind="simulation_decide",
                    idempotency_key="sim-decision:" + content_hash([item.id, participant.id]),
                    triggered_by="simulation",
                    request_json=payload.model_dump(mode="json"),
                    max_attempts=1,
                )
    return tuple(actions)


class SimulationHandler(BaseJobHandler):
    def __init__(
        self,
        kind: Literal["simulation_cycle", "simulation_decide", "simulation_review"],
        *,
        settings: Settings,
        sessions: SessionFactory = session_scope,
        calendar: TradingCalendar | None = None,
        model: Model | None = None,
    ) -> None:
        self.kind, self.settings, self.sessions, self.calendar, self.model = (
            kind,
            settings,
            sessions,
            calendar,
            model,
        )

    async def on_claimed(self, session: AsyncSession, job: ClaimedJob) -> None:
        _DEFERRED.set(False)
        if self.kind == "simulation_cycle":
            return
        from investment_agent.agent.operations import (
            AGENT_KINDS,
            MAX_BACKGROUND,
            classify,
            limits,
            reserve,
        )

        # Shared admission cursor serializes this read/count with all agent
        # claims, including across processes. The claim transaction already
        # owns its pipeline row; no model I/O happens under this lock.
        await reserve(session)
        running = list(
            await session.scalars(
                select(PipelineRun).where(
                    PipelineRun.status == "running",
                    PipelineRun.worker_id.is_not(None),
                    PipelineRun.id != job.run_id,
                    PipelineRun.kind.in_((*AGENT_KINDS, "simulation_decide", "simulation_review")),
                )
            )
        )
        maximum, _ = await limits(session)
        background_limit = min(MAX_BACKGROUND, max(1, maximum - 1))
        backgrounds = sum(
            purpose != "interactive" for _, purpose, _ in await classify(session, running)
        )
        if len(running) >= maximum or backgrounds >= background_limit:
            await PipelineRunRepository(session).defer_running(
                job.run_id, available_at=datetime.now(UTC) + timedelta(seconds=5), lease=job.lease
            )
            _DEFERRED.set(True)

    async def _recover_decision(
        self, session: AsyncSession, run: PipelineRun, now: datetime
    ) -> None:
        payload = SimulationJobPayload.model_validate(run.request_json)
        if payload.round_id is None or payload.participant_id is None:
            return
        svc = SimulationService(
            session,
            data_dir=self.settings.data_dir,
            calendar=self.calendar or simulation_calendar(self.settings),
            now=lambda: now,
        )
        account = await svc.account(payload.experiment_id, payload.participant_id)
        previous = [
            item
            for item in await svc.decisions(payload.experiment_id, payload.participant_id)
            if item.round_id == payload.round_id
        ]
        key = f"runtime:{payload.round_id}:{payload.participant_id}"
        if any(item.idempotency_key == key for item in previous):
            return
        costs = (
            await session.scalars(
                select(CostRow).where(
                    CostRow.experiment_id == payload.experiment_id,
                    CostRow.participant_id == payload.participant_id,
                    CostRow.round_id == payload.round_id,
                )
            )
        ).all()
        unknown = any(row.status in {"reserved", "unknown"} for row in costs)
        failure = Decision(
            experiment_id=payload.experiment_id,
            participant_id=payload.participant_id,
            account_id=account.account_id,
            id=stable_id("runtime-recovery", key),
            round_id=payload.round_id,
            revision=max((item.revision for item in previous), default=-1) + 1,
            account_revision=account.revision,
            status="unknown" if unknown else "failed",
            action="no_submission",
            rationale="Worker stopped without a durable trading decision.",
            submitted_at=now,
            failure_code="unknown_call_recovery" if unknown else "model_output_not_committed" if costs else "worker_failed",
            idempotency_key=key,
        )
        await svc.submit_decision(
            payload.experiment_id, payload.round_id, payload.participant_id, failure
        )

    async def on_recovered(
        self, session: AsyncSession, run: PipelineRun, *, requeued: bool, now: datetime
    ) -> None:
        if self.kind == "simulation_decide" and not requeued:
            await self._recover_decision(session, run, utc_timestamp(now))

    async def finalize(self, job: ClaimedJob) -> None:
        if self.kind != "simulation_decide":
            return
        try:
            async with self.sessions() as session:
                run = await session.get(PipelineRun, job.run_id)
                if run is not None and run.status == "failed":
                    await self._recover_decision(session, run, datetime.now(UTC))
        except Exception:
            # Finalization is best effort; cost reservations remain durable
            # and the deadline closer still records a no-submission outcome.
            import structlog

            structlog.get_logger(__name__).exception(
                "simulation.failure_recovery_failed", run_id=job.run_id
            )

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        if _DEFERRED.get():
            _DEFERRED.set(False)
            return "completed"
        payload = SimulationJobPayload.model_validate(job.request_json)

        async def work() -> JobOutcome:
            calendar = self.calendar or simulation_calendar(self.settings)
            if self.kind == "simulation_cycle":
                await cycle_experiment(
                    payload.experiment_id,
                    settings=self.settings,
                    calendar=calendar,
                    sessions=self.sessions,
                    force_collection=payload.force_collection,
                    pipeline_run_id=job.run_id,
                )
            elif self.kind == "simulation_decide":
                if payload.round_id is None or payload.participant_id is None:
                    raise ValueError(
                        "simulation decision requires server-bound round and participant"
                    )
                await decide_participant(
                    payload.experiment_id,
                    payload.round_id,
                    payload.participant_id,
                    settings=self.settings,
                    calendar=calendar,
                    sessions=self.sessions,
                    model=self.model,
                    pipeline_run_id=job.run_id,
                )
            else:
                from investment_agent.simulation.runtime import review_participants

                await review_participants(
                    payload.experiment_id,
                    settings=self.settings,
                    calendar=calendar,
                    sessions=self.sessions,
                    model=self.model,
                    pipeline_run_id=job.run_id,
                )
            async with self.sessions() as session:
                await PipelineRunRepository(session).mark_success(
                    job.run_id,
                    {"experiment_id": payload.experiment_id, "kind": self.kind},
                    lease=job.lease,
                )
            return "completed"

        return await ctx.run_with_heartbeat(job.lease, work)


def simulation_job_handlers(settings: Settings) -> list[SimulationHandler]:
    return [SimulationHandler(kind, settings=settings) for kind in SIMULATION_KINDS]

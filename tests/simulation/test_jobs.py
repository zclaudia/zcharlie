from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from investment_agent.jobs.base import ClaimedJob
from investment_agent.providers.baostock_market import (
    DailyBar,
    DailyBarSeries,
    DividendYear,
    StockBasic,
)
from investment_agent.research.worker import partition_worker_kinds
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.jobs import (
    SimulationHandler,
    cycle_experiment,
    financial_periods,
    simulation_calendar,
    wake_active_experiments,
    wake_experiment,
)
from investment_agent.simulation.service import SimulationService
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import RunLease
from tests.simulation.test_runtime import runtime_arena as runtime_arena
from tests.simulation.test_service import DAY, DEADLINE, NOW, add_proof


class Collector:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.windows = []

    async def fetch_stock_basic(self, ticker):
        self.calls.append(ticker)
        return StockBasic(ticker, "fixture", date(2000, 1, 1), None, True)

    async def fetch_daily_bars(self, ticker, start, end, adjust):
        self.windows.append((start, end))
        bars = tuple(
            DailyBar(
                start + timedelta(days=offset),
                Decimal(10),
                Decimal(11),
                Decimal(9),
                Decimal(10),
                Decimal(100_000_000),
                Decimal(1_000_000_000),
                None,
                1,
                False,
                None,
                None,
                None,
            )
            for offset in range((end - start).days + 1)
        )
        return DailyBarSeries(
            ticker, adjust, start, end, bars, (), self.clock[0] - timedelta(seconds=1), ()
        )

    async def fetch_dividends(self, ticker, year):
        return DividendYear(ticker, year, (), (), (), 0, self.clock[0], ())

    async def fetch_quarterly_financials(self, ticker, year, quarter):
        return None


def cycle_args(arena, collector):
    @asynccontextmanager
    async def factory():
        yield collector

    return dict(
        settings=arena["settings"],
        calendar=arena["calendar"],
        sessions=arena["sessions"],
        now=lambda: arena["clock"][0],
        collector_factory=factory,
        require_financials=False,
    )


def service(arena, session):
    return SimulationService(
        session,
        data_dir=arena["settings"].data_dir,
        calendar=arena["calendar"],
        now=lambda: arena["clock"][0],
    )


async def test_active_wakes_are_minute_idempotent_and_drafts_need_explicit_refresh(runtime_arena):
    arena = runtime_arena
    async with arena["sessions"]() as session:
        svc = service(arena, session)
        draft = await svc.create(arena["experiment"].config, idempotency_key="draft")
        assert await wake_active_experiments(session, now=NOW) == 1
        assert await wake_active_experiments(session, now=NOW) == 0
        with pytest.raises(SimulationError):
            await wake_experiment(session, experiment_id=draft.id, now=NOW)
        await wake_experiment(session, experiment_id=draft.id, now=NOW, force_collection=True)
        runs = (
            await session.scalars(select(PipelineRun).where(PipelineRun.kind == "simulation_cycle"))
        ).all()
        assert len(runs) == 2
        assert all(run.max_attempts == 1 for run in runs)
        assert {run.request_json["experiment_id"] for run in runs} == {
            arena["experiment"].id,
            draft.id,
        }


async def test_cycle_closes_at_real_deadline_without_collecting_or_catchup(runtime_arena):
    arena = runtime_arena
    arena["clock"][0] = DEADLINE
    collector = Collector(arena["clock"])
    actions = await cycle_experiment(arena["experiment"].id, **cycle_args(arena, collector))
    assert actions == ("closed",)
    assert not collector.calls
    async with arena["sessions"]() as session:
        assert (
            await service(arena, session).get_round(arena["experiment"].id, arena["round"].id)
        ).status == "locked"
    arena["clock"][0] = datetime(2026, 11, 12, 1, tzinfo=UTC)  # restart before today's close
    await cycle_experiment(arena["experiment"].id, **cycle_args(arena, collector))
    async with arena["sessions"]() as session:
        rounds = await service(arena, session).rounds(arena["experiment"].id)
    assert [item.observation_date for item in rounds] == [DAY]


async def test_cycle_waits_for_proof_then_recovers_settles_values_and_enqueues_next_decision(
    runtime_arena,
):
    arena = runtime_arena
    arena["clock"][0] = DEADLINE
    collector = Collector(arena["clock"])
    await cycle_experiment(arena["experiment"].id, **cycle_args(arena, collector))
    arena["clock"][0] = NOW + timedelta(days=1)
    actions = await cycle_experiment(arena["experiment"].id, **cycle_args(arena, collector))
    assert "collected" in actions and "waiting_settlement" in actions
    assert set(collector.calls) == {"600519.SH", "600036.SH"}
    async with arena["sessions"]() as session:
        svc = service(arena, session)
        assert (
            await svc.get_round(arena["experiment"].id, arena["round"].id)
        ).status == "awaiting_execution_data"
        for listing in arena["listings"]:
            await add_proof(svc, arena["experiment"].id, listing.id, DAY + timedelta(days=1))
    actions = await cycle_experiment(arena["experiment"].id, **cycle_args(arena, collector))
    assert "collecting_decisions" in actions
    assert len(collector.calls) == 2  # same recovery bucket does not recollect
    await cycle_experiment(arena["experiment"].id, **cycle_args(arena, collector))
    async with arena["sessions"]() as session:
        svc = service(arena, session)
        assert (await svc.get_round(arena["experiment"].id, arena["round"].id)).status == "settled"
        rounds = await svc.rounds(arena["experiment"].id)
        assert len(rounds) == 2 and rounds[-1].status == "collecting_decisions"
        runs = (
            await session.scalars(
                select(PipelineRun).where(PipelineRun.kind == "simulation_decide")
            )
        ).all()
        assert len(runs) == 1 and runs[0].request_json["round_id"] == rounds[-1].id
        navs = await svc.nav(arena["experiment"].id)
        latest = {}
        for nav in navs:
            key = (nav.account_id, nav.valuation_date)
            if key not in latest or nav.version > latest[key].version:
                latest[key] = nav
        assert any(nav.quality == "incomplete" for nav in navs)
        assert all(nav.quality == "complete" for nav in latest.values())


async def test_handler_uses_heartbeat_and_explicit_simulation_kind(runtime_arena, monkeypatch):
    from investment_agent.simulation import jobs

    arena = runtime_arena
    calls = []

    async def cycle(identity, **kwargs):
        calls.append(identity)
        return ()

    monkeypatch.setattr(jobs, "cycle_experiment", cycle)

    class Context:
        async def run_with_heartbeat(self, lease, work):
            calls.append(lease)
            return await work()

    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

    async with arena["sessions"]() as session:
        repo = PipelineRunRepository(session)
        queued, _ = await repo.enqueue(
            kind="simulation_cycle",
            idempotency_key="handler-test",
            triggered_by="test",
            request_json={"experiment_id": arena["experiment"].id},
        )
        claimed = await repo.claim_next(
            worker_id="worker", kinds=["simulation_cycle"], run_id=queued.id
        )
    lease = RunLease(claimed.id, "worker", 1)
    job = ClaimedJob(
        claimed.id,
        "simulation_cycle",
        lease,
        request_json={"experiment_id": arena["experiment"].id},
    )
    handler = SimulationHandler(
        "simulation_cycle",
        settings=arena["settings"],
        sessions=arena["sessions"],
        calendar=arena["calendar"],
    )
    assert await handler.execute(job, Context()) == "completed"
    assert calls == [lease, arena["experiment"].id]


def test_worker_and_deployment_partitions_cover_model_and_deterministic_work(runtime_arena):
    arena = runtime_arena
    agent, collection = partition_worker_kinds(arena["settings"])
    assert {"simulation_decide", "simulation_review"} <= set(agent)
    assert "simulation_cycle" in collection
    assert not set(agent) & set(collection)
    compose = yaml.safe_load((Path(__file__).parents[2] / "docker-compose.yml").read_text())
    deployed = {}
    for name, configuration in compose["services"].items():
        command = configuration.get("command", [])
        if "--kinds" in command:
            deployed[name] = set(command[command.index("--kinds") + 1].split(","))
    assert any({"simulation_decide", "simulation_review"} <= kinds for kinds in deployed.values())
    assert any("simulation_cycle" in kinds for kinds in deployed.values())


async def test_scheduler_dispatch_only_queues_and_runtime_calendar_path_is_honored(
    runtime_arena, monkeypatch
):
    from investment_agent.scheduler import runner

    arena = runtime_arena
    monkeypatch.setattr(runner, "session_scope", arena["sessions"])
    assert await runner.run_simulation_dispatch() == 1
    assert await runner.run_simulation_dispatch() == 0
    runtime = arena["settings"].data_dir / "calendars" / "a_share.yaml"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(yaml.safe_dump(arena["calendar"].model_dump(mode="json")))
    assert simulation_calendar(arena["settings"]).calendar_id == "sse_2026"
    assert financial_periods(date(2026, 1, 1))[0] == (2025, 4)


async def test_manual_draft_refresh_before_close_uses_last_closed_session_and_no_decisions(
    runtime_arena,
):
    arena = runtime_arena
    async with arena["sessions"]() as session:
        draft = await service(arena, session).create(
            arena["experiment"].config, idempotency_key="manual-draft"
        )
    arena["clock"][0] = datetime(2026, 11, 10, 2, tzinfo=UTC)
    collector = Collector(arena["clock"])
    result = await cycle_experiment(draft.id, force_collection=True, **cycle_args(arena, collector))
    assert result == ("collected",)
    assert collector.windows and all(
        end == DAY and (end - start).days >= 60 for start, end in collector.windows
    )
    async with arena["sessions"]() as session:
        assert not await service(arena, session).rounds(draft.id)
        assert not await service(arena, session).decisions(draft.id)


async def test_cold_start_collects_bounded_history_and_blocks_on_unproven_rules(runtime_arena):
    from investment_agent.simulation.contracts import ExperimentConfig
    from investment_agent.storage.repositories.listing import ListingRepository

    arena = runtime_arena
    async with arena["sessions"]() as session:
        listing = await ListingRepository(session).ensure_a_share_listing("601318.SH")
        config = ExperimentConfig.model_validate(
            arena["experiment"].config.model_dump() | {"universe": (listing.id,)}
        )
        svc = service(arena, session)
        fresh = await svc.create(config, idempotency_key="cold-create")
        await svc.start(fresh.id, expected_revision=0, idempotency_key="cold-start")
    collector = Collector(arena["clock"])
    result = await cycle_experiment(fresh.id, **cycle_args(arena, collector))
    assert "collected" in result and all(
        (end - start).days == 60 for start, end in collector.windows
    )
    assert set(collector.calls) == {"601318.SH"}
    async with arena["sessions"]() as session:
        svc = service(arena, session)
        rounds = await svc.rounds(fresh.id)
        assert len(rounds) == 1 and rounds[0].status in {"blocked", "waiting_data"}
        assert any(
            "execution_evidence_missing" in reason for reason in rounds[0].manifest.missing_reasons
        )
        facts = (await svc.common_facts(fresh.id, rounds[0].id)).payload()["facts"]
        assert len(facts) > 1  # imported historical records are actually shared
        assert not await svc.decisions(fresh.id)


@pytest.mark.parametrize("runtime_arena", ["weekly"], indirect=True)
async def test_weekly_suppression_still_enqueues_maturity_reviews(runtime_arena):
    arena = runtime_arena
    arena["clock"][0] = DEADLINE
    async with arena["sessions"]() as session:
        await service(arena, session).close_round(arena["experiment"].id, arena["round"].id)
    arena["clock"][0] = NOW + timedelta(days=1)
    async with arena["sessions"]() as session:
        svc = service(arena, session)
        for listing in arena["listings"]:
            await add_proof(svc, arena["experiment"].id, listing.id, DAY + timedelta(days=1))
    collector = Collector(arena["clock"])
    await cycle_experiment(arena["experiment"].id, **cycle_args(arena, collector))
    first_capture_count = len(collector.windows)
    arena["clock"][0] += timedelta(minutes=31)
    await cycle_experiment(arena["experiment"].id, **cycle_args(arena, collector))
    assert len(collector.windows) == first_capture_count
    async with arena["sessions"]() as session:
        assert len(await service(arena, session).rounds(arena["experiment"].id)) == 1
        review_runs = (
            await session.scalars(
                select(PipelineRun).where(PipelineRun.kind == "simulation_review")
            )
        ).all()
        assert review_runs and all(
            run.request_json["experiment_id"] == arena["experiment"].id for run in review_runs
        )


async def test_concurrent_model_admission_preserves_interactive_slot(runtime_arena, tmp_path):
    import asyncio
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investment_agent.agent.operations import AGENT_KINDS
    from investment_agent.storage.database import configure_sqlite_engine
    from investment_agent.storage.models import Base
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

    arena = runtime_arena
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admission.db'}")
    configure_sqlite_engine(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with maker() as session, session.begin():
            yield session

    try:
        async with sessions() as session:
            ids = []
            for index in range(6):
                row, _ = await PipelineRunRepository(session).enqueue(
                    kind="simulation_review",
                    idempotency_key=f"background:{index}",
                    triggered_by="test",
                    max_attempts=1,
                    request_json={"experiment_id": arena["experiment"].id},
                )
                ids.append(row.id)
        handler = SimulationHandler(
            "simulation_review",
            settings=arena["settings"],
            sessions=sessions,
            calendar=arena["calendar"],
        )

        async def claim(identity):
            async with sessions() as session:
                row = await PipelineRunRepository(session).claim_next(
                    worker_id=f"worker-{identity}", kinds=["simulation_review"], run_id=identity
                )
                job = ClaimedJob(
                    row.id,
                    row.kind,
                    RunLease(row.id, row.worker_id, row.attempt_count),
                    request_json=row.request_json,
                )
                await handler.on_claimed(session, job)
                await session.refresh(row)
                return row.status, row.attempt_count

        outcomes = await asyncio.gather(*(claim(identity) for identity in ids))
        assert outcomes.count(("running", 1)) == 3
        assert outcomes.count(("queued", 0)) == 3  # deferral does not spend retries
        async with sessions() as session:
            repo = PipelineRunRepository(session)
            interactive, _ = await repo.enqueue(
                kind="agent_turn",
                idempotency_key="interactive",
                triggered_by="test",
                request_json={},
            )
            claimed = await repo.claim_next(
                worker_id="interactive-worker", kinds=["agent_turn"], run_id=interactive.id
            )
            assert claimed is not None  # fourth slot is reserved for interactive work
            next_row, _ = await repo.enqueue(
                kind="agent_turn",
                idempotency_key="interactive-two",
                triggered_by="test",
                request_json={},
            )
            assert (
                await repo.claim_next(
                    worker_id="another-worker", kinds=["agent_turn"], run_id=next_row.id
                )
                is None
            )
        assert "simulation_review" not in AGENT_KINDS
    finally:
        await engine.dispose()


async def test_exhausted_worker_recovery_records_unknown_without_reissuing_model(runtime_arena):
    from investment_agent.simulation.budget import SimulationBudget
    from investment_agent.simulation.contracts import AccountScope
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

    arena = runtime_arena
    participant = arena["participant"]

    async def allowed():
        return None

    ledger = SimulationBudget(
        scope=AccountScope(
            experiment_id=participant.experiment_id,
            participant_id=participant.id,
            account_id=participant.account_id,
        ),
        round_id=arena["round"].id,
        config=participant.config.budget,
        model_ref="selected/model",
        guard=allowed,
        sessions=arena["sessions"],
    )
    identity = await ledger.reserve(100)
    await ledger.sent(identity)
    handler = SimulationHandler(
        "simulation_decide",
        settings=arena["settings"],
        sessions=arena["sessions"],
        calendar=arena["calendar"],
    )
    async with arena["sessions"]() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="simulation_decide",
            idempotency_key="crashed",
            triggered_by="test",
            request_json={
                "experiment_id": arena["experiment"].id,
                "round_id": arena["round"].id,
                "participant_id": participant.id,
            },
            max_attempts=1,
        )
        run.status = "failed"
        await handler.on_recovered(session, run, requeued=False, now=arena["clock"][0])
        result = await service(arena, session).decisions(arena["experiment"].id, participant.id)
        assert len(result) == 1 and result[0].status == "unknown" and not result[0].orders
        await handler.on_recovered(session, run, requeued=False, now=arena["clock"][0])
        assert (
            await service(arena, session).decisions(arena["experiment"].id, participant.id)
            == result
        )


async def test_collection_provider_awaits_hold_no_database_transaction(runtime_arena):
    arena = runtime_arena
    class AssertNoTransaction(Collector):
        async def fetch_stock_basic(self, ticker):
            assert not any(session.in_transaction() for session in arena["opened"])
            return await super().fetch_stock_basic(ticker)
        async def fetch_daily_bars(self, ticker, start, end, adjust):
            assert not any(session.in_transaction() for session in arena["opened"])
            return await super().fetch_daily_bars(ticker, start, end, adjust)
        async def fetch_dividends(self, ticker, year):
            assert not any(session.in_transaction() for session in arena["opened"])
            return await super().fetch_dividends(ticker, year)
        async def fetch_quarterly_financials(self, ticker, year, quarter):
            assert not any(session.in_transaction() for session in arena["opened"])
            return await super().fetch_quarterly_financials(ticker, year, quarter)
    collector = AssertNoTransaction(arena["clock"])
    actions = await cycle_experiment(arena["experiment"].id, force_collection=True,
                                    **cycle_args(arena, collector))
    assert "collected" in actions and len(collector.calls) == 2


async def test_missing_older_execution_bar_does_not_poison_daily_success_cache(runtime_arena):
    from dataclasses import replace

    from investment_agent.storage.models.simulation import SimulationRequestRecord

    arena = runtime_arena
    arena["clock"][0] = DEADLINE
    async with arena["sessions"]() as session:
        await service(arena, session).close_round(arena["experiment"].id, arena["round"].id)
    missing_day = DAY + timedelta(days=1)
    arena["clock"][0] = NOW + timedelta(days=2)
    class MissingOlderBar(Collector):
        async def fetch_daily_bars(self, ticker, start, end, adjust):
            result = await super().fetch_daily_bars(ticker, start, end, adjust)
            return replace(result, bars=tuple(bar for bar in result.bars if bar.trade_date != missing_day))
    broken = MissingOlderBar(arena["clock"])
    await cycle_experiment(arena["experiment"].id, **cycle_args(arena, broken))
    async with arena["sessions"]() as session:
        receipts = (await session.scalars(select(SimulationRequestRecord).where(
            SimulationRequestRecord.scope.like("sim-collect:%")))).all()
        assert receipts and not receipts[0].payload["automatic_data_complete"]
        for listing in arena["listings"]:
            await add_proof(service(arena, session), arena["experiment"].id, listing.id, missing_day)
    arena["clock"][0] += timedelta(minutes=31)
    recovered = Collector(arena["clock"])
    actions = await cycle_experiment(arena["experiment"].id, **cycle_args(arena, recovered))
    assert "collected" in actions and recovered.calls
    async with arena["sessions"]() as session:
        previous = await service(arena, session).get_round(arena["experiment"].id, arena["round"].id)
        assert previous.status == "settled"


async def test_collection_claim_prevents_two_workers_fetching_same_recovery_bucket(runtime_arena):
    import asyncio

    arena = runtime_arena
    started, release = asyncio.Event(), asyncio.Event()
    class BlockingCollector(Collector):
        async def fetch_stock_basic(self, ticker):
            started.set()
            await release.wait()
            return await super().fetch_stock_basic(ticker)
    first, second = BlockingCollector(arena["clock"]), Collector(arena["clock"])
    task = asyncio.create_task(cycle_experiment(arena["experiment"].id,
        force_collection=True, **cycle_args(arena, first)))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await cycle_experiment(arena["experiment"].id, force_collection=True, **cycle_args(arena, second))
        assert not second.calls
    finally:
        release.set()
        await task
    assert len(first.calls) == 2

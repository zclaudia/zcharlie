import json
from contextlib import asynccontextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from investment_agent.config import Settings
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    TradingCalendarSource,
    TradingCalendarYear,
)
from investment_agent.simulation.contracts import ExperimentConfig
from investment_agent.simulation.runtime import (
    builtin_participant,
    decide_participant,
    freeze_experiment_models,
    resolve_participant,
    validate_experiment_models,
)
from investment_agent.simulation.service import SimulationService
from investment_agent.storage.models.agent import AgentModelRun
from investment_agent.storage.models.simulation import SimulationCostRecord
from investment_agent.storage.repositories.listing import ListingRepository
from tests.simulation.test_service import DAY, DEADLINE, NOW, add_bar, add_proof


def response(**changes):
    output = {
        "action": "hold",
        "rationale": "Waiting for stronger archived evidence.",
        "expected_holding_days": 1,
        **changes,
    }
    return ModelResponse(
        [TextPart(json.dumps(output))], usage=RequestUsage(input_tokens=100, output_tokens=30)
    )


@pytest.fixture
async def runtime_arena(db_session, tmp_path, sim_config, request):
    catalog = {
        "providers": [
            {
                "id": "selected",
                "name": "selected",
                "kind": "openai",
                "base_url": "https://selected.example/v1",
                "api_key": "private-never-snapshot",
                "models": [
                    {"id": "model", "name": "fixture", "model": "upstream-model", "max_tokens": 128}
                ],
            }
        ]
    }
    settings = Settings(
        api_key="test", data_dir=tmp_path, llm_catalog=json.dumps(catalog), model_slots=""
    )
    selected = builtin_participant(
        name="selected-agent",
        provider_ref="selected",
        model_ref="model",
        memory_policy="matured_own_reviews",
        budget={
            "input_price_per_million": "1",
            "output_price_per_million": "2",
            "conservative_call_reservation": "1",
            "round_cost_limit": "10",
            "season_cost_limit": "100",
        },
    )
    listings = [
        await ListingRepository(db_session).ensure_a_share_listing(ticker)
        for ticker in ("600519.SH", "600036.SH")
    ]
    config = ExperimentConfig.model_validate(
        sim_config.model_dump()
        | {
            "universe": tuple(item.id for item in listings),
            "rebalance_frequency": getattr(request, "param", "daily"),
            "participants": (*sim_config.participants[:2], selected),
        }
    )
    config = freeze_experiment_models(config, settings)
    calendar = TradingCalendar(
        calendar_id="sse_2026",
        timezone="Asia/Shanghai",
        source=TradingCalendarSource(title="fixture", url="https://example.test", published_on=DAY),
        years=[TradingCalendarYear(year=2026)],
    )
    clock = [NOW]
    svc = SimulationService(db_session, data_dir=tmp_path, calendar=calendar, now=lambda: clock[0])
    experiment = await svc.create(config, idempotency_key="create")
    await svc.start(experiment.id, expected_revision=0, idempotency_key="start")
    for listing in listings:
        await add_bar(db_session, listing.id, DAY)
        await add_proof(svc, experiment.id, listing.id, DAY)
    round_ = await svc.prepare_round(
        experiment.id,
        observation_date=DAY,
        cutoff=NOW,
        decision_deadline=DEADLINE,
        idempotency_key="prepare",
        require_financials=False,
    )
    participant = next(
        item for item in await svc.participants(experiment.id) if item.config.kind == "agent"
    )
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, expire_on_commit=False)
    opened = []

    @asynccontextmanager
    async def sessions():
        async with maker() as session:
            opened.append(session)
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def live():
        return settings

    return dict(
        settings=settings,
        catalog=catalog,
        calendar=calendar,
        clock=clock,
        sessions=sessions,
        experiment=experiment,
        round=round_,
        participant=participant,
        listings=listings,
        live=live,
        opened=opened,
    )


def arguments(arena):
    return dict(
        settings=arena["settings"],
        calendar=arena["calendar"],
        sessions=arena["sessions"],
        now=lambda: arena["clock"][0],
        settings_loader=arena["live"],
    )


async def decide(arena, model):
    return await decide_participant(
        arena["experiment"].id,
        arena["round"].id,
        arena["participant"].id,
        model=model,
        **arguments(arena),
    )


async def test_selected_model_no_fallback_secret_free_freeze_and_config_change(runtime_arena):
    arena = runtime_arena
    participant = arena["participant"].config
    selected = resolve_participant(participant, arena["settings"])
    assert selected.selection.ref == "selected/model"
    assert selected.values["agent_model"] == "upstream-model"
    assert selected.values["agent_max_tokens"] == 128
    snapshot = participant.model_dump_json()
    assert "private-never-snapshot" not in snapshot
    assert "https://selected.example" not in snapshot
    validate_experiment_models(arena["experiment"].config, arena["settings"])
    arena["catalog"]["providers"][0]["models"][0]["enabled"] = False
    changed = arena["settings"].model_copy(update={"llm_catalog": json.dumps(arena["catalog"])})
    with pytest.raises(ValueError):
        resolve_participant(participant, changed)


async def test_native_decision_committed_costs_safe_audit_fair_payload_and_no_write_transaction(
    runtime_arena,
):
    arena = runtime_arena
    calls = []

    async def model(messages, info):
        # The audit session is open but untouched. All admission writes have
        # already committed and their sessions exited before external await.
        assert not any(session.in_transaction() for session in arena["opened"])
        serialized = str(messages)
        assert "selected-agent" not in serialized  # no participant roster
        assert "latest" not in serialized.lower()
        assert info.function_tools == []
        calls.append(messages)
        return response()

    output = await decide(arena, FunctionModel(model))
    assert output.status == "submitted" and output.action == "hold"
    assert await decide(arena, FunctionModel(model)) == output
    assert len(calls) == 1
    async with arena["sessions"]() as session:
        costs = (await session.scalars(select(SimulationCostRecord))).all()
        audits = (await session.scalars(select(AgentModelRun))).all()
    assert len(costs) == len(audits) == 1
    assert costs[0].status == "settled" and costs[0].payload["amount"] == "0.00016"
    assert audits[0].success and audits[0].config_snapshot["selection"] == {
        "provider_id": "selected",
        "model_id": "model",
    }


async def test_format_retry_reserves_each_actual_request_and_commits_failure_audit(runtime_arena):
    arena = runtime_arena
    calls = []

    def invalid(messages, info):
        calls.append(True)
        return ModelResponse(
            [TextPart('{"action":"hold"}')], usage=RequestUsage(input_tokens=10, output_tokens=10)
        )

    output = await decide(arena, FunctionModel(invalid))
    assert output.action == "no_submission" and output.status in {"invalid", "failed"}
    assert len(calls) == 2
    async with arena["sessions"]() as session:
        costs = (await session.scalars(select(SimulationCostRecord))).all()
        audit = (await session.scalars(select(AgentModelRun))).one()
    assert len(costs) == 2 and all(cost.status == "settled" for cost in costs)
    assert not audit.success


async def test_unknown_sent_call_is_recorded_and_never_replayed(runtime_arena):
    arena = runtime_arena
    calls = []

    async def fail_after_send(messages, info):
        calls.append(True)
        raise TimeoutError("provider response lost")

    output = await decide(arena, FunctionModel(fail_after_send))
    assert output.status == "unknown" and output.action == "no_submission"
    assert (await decide(arena, FunctionModel(fail_after_send))).id == output.id
    assert len(calls) == 1
    async with arena["sessions"]() as session:
        cost = (await session.scalars(select(SimulationCostRecord))).one()
        assert cost.status == "unknown" and Decimal(cost.payload["amount"]) == 1
        assert not (await session.scalars(select(AgentModelRun))).one().success


@pytest.mark.parametrize("boundary", ["deadline", "pause", "disabled"])
async def test_rechecks_boundaries_before_send_without_fake_hold(runtime_arena, boundary):
    arena = runtime_arena
    if boundary == "deadline":
        arena["clock"][0] = DEADLINE
    elif boundary == "pause":
        async with arena["sessions"]() as session:
            await SimulationService(
                session,
                data_dir=arena["settings"].data_dir,
                calendar=arena["calendar"],
                now=lambda: NOW,
            ).pause(arena["experiment"].id, expected_revision=1, idempotency_key="pause")
    else:
        arena["catalog"]["providers"][0]["models"][0]["enabled"] = False
        arena["settings"].llm_catalog = json.dumps(arena["catalog"])

    def unexpected(messages, info):
        pytest.fail("boundary must prevent sending")

    output = await decide(arena, FunctionModel(unexpected))
    assert output.action == "no_submission" and output.failure_code is not None
    async with arena["sessions"]() as session:
        assert not (await session.scalars(select(SimulationCostRecord))).all()


async def test_two_day_fake_model_flow_is_durable_and_pit(runtime_arena):
    arena = runtime_arena
    first = arena["listings"][0].id
    result = await decide(
        arena,
        FunctionModel(
            lambda m, i: response(
                action="trade",
                orders=[
                    {
                        "listing_id": first,
                        "side": "buy",
                        "quantity": "100",
                        "protection_price": "10.1",
                    }
                ],
            )
        ),
    )
    assert result.status == "submitted"
    arena["clock"][0] = DEADLINE
    async with arena["sessions"]() as session:
        service = SimulationService(
            session,
            data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"],
            now=lambda: arena["clock"][0],
        )
        await service.close_round(arena["experiment"].id, arena["round"].id)
    arena["clock"][0] = NOW + timedelta(days=1)
    async with arena["sessions"]() as session:
        service = SimulationService(
            session,
            data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"],
            now=lambda: arena["clock"][0],
        )
        for listing in arena["listings"]:
            await add_bar(session, listing.id, date(2026, 11, 10), close="11")
            await add_proof(service, arena["experiment"].id, listing.id, date(2026, 11, 10))
        settled = await service.settle_round(arena["experiment"].id, arena["round"].id)
        assert settled.status == "settled"
        account = await service.account(arena["experiment"].id, arena["participant"].id)
        assert account.state.lots[0].quantity == 100 and account.state.cash < 1_000_000
        assert all(item.quality == "complete" for item in await service.nav(arena["experiment"].id))


async def test_model_change_while_response_is_pending_stops_submission(runtime_arena):
    arena = runtime_arena

    def changed(messages, info):
        arena["catalog"]["providers"][0]["models"][0]["temperature"] = 0.9
        arena["settings"].llm_catalog = json.dumps(arena["catalog"])
        return response()

    result = await decide(arena, FunctionModel(changed))
    assert result.action == "no_submission" and result.failure_code == "invalid_config"


async def mature_arena(arena):
    await decide(arena, FunctionModel(lambda m, i: response()))
    arena["clock"][0] = DEADLINE
    async with arena["sessions"]() as session:
        service = SimulationService(
            session,
            data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"],
            now=lambda: arena["clock"][0],
        )
        await service.close_round(arena["experiment"].id, arena["round"].id)
    arena["clock"][0] = NOW + timedelta(days=1)
    async with arena["sessions"]() as session:
        service = SimulationService(
            session,
            data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"],
            now=lambda: arena["clock"][0],
        )
        for listing in arena["listings"]:
            await add_bar(session, listing.id, DAY + timedelta(days=1))
            await add_proof(service, arena["experiment"].id, listing.id, DAY + timedelta(days=1))
        await service.settle_round(arena["experiment"].id, arena["round"].id)
    arena["clock"][0] = NOW + timedelta(days=2)
    async with arena["sessions"]() as session:
        service = SimulationService(
            session,
            data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"],
            now=lambda: arena["clock"][0],
        )
        for listing in arena["listings"]:
            await add_bar(session, listing.id, DAY + timedelta(days=2))
        await service.value(arena["experiment"].id, DAY + timedelta(days=2))


async def test_mature_model_review_has_own_numeric_refs_and_is_not_replayed(runtime_arena):
    from pydantic_ai.messages import UserPromptPart

    from investment_agent.simulation.runtime import review_participants

    arena = runtime_arena
    await mature_arena(arena)
    calls = []

    def review(messages, info):
        payload = next(
            json.loads(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        calls.append(payload)
        assert payload["decision"]["participant_id"] == arena["participant"].id
        return ModelResponse(
            [
                TextPart(
                    json.dumps(
                        {
                            "findings": "The original hypothesis remains under observation.",
                            "counter_evidence": ["The evidence remains limited."],
                            "improvement_proposals": [
                                "Check the archived operating indicators at the declared horizon."
                            ],
                            "nav_refs": [payload["nav"][-1]["id"]],
                        }
                    )
                )
            ],
            usage=RequestUsage(input_tokens=100, output_tokens=40),
        )

    result = await review_participants(
        arena["experiment"].id, model=FunctionModel(review), **arguments(arena)
    )
    assert result == ("matured_model_review",)
    assert (
        await review_participants(
            arena["experiment"].id, model=FunctionModel(review), **arguments(arena)
        )
        == ()
    )
    assert len(calls) == 1
    async with arena["sessions"]() as session:
        service = SimulationService(
            session,
            data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"],
            now=lambda: arena["clock"][0],
        )
        reviews = await service.reviews(arena["experiment"].id, arena["participant"].id)
        learned = next(item for item in reviews if item.findings.startswith("Model review:"))
        assert learned.memory_eligible and learned.nav_refs
        assert all(
            not item.memory_eligible
            for item in reviews
            if not item.findings.startswith("Model review:")
        )


async def test_review_failure_is_explicit_and_cannot_enter_memory(runtime_arena):
    from investment_agent.simulation.runtime import review_participants

    arena = runtime_arena
    await mature_arena(arena)

    def invalid_review(messages, info):
        return ModelResponse(
            [
                TextPart(
                    json.dumps(
                        {"findings": "Return was 999 percent.", "nav_refs": ["other-account-nav"]}
                    )
                )
            ],
            usage=RequestUsage(input_tokens=10, output_tokens=10),
        )

    result = await review_participants(
        arena["experiment"].id, model=FunctionModel(invalid_review), **arguments(arena)
    )
    assert result == ("future_evidence",)
    async with arena["sessions"]() as session:
        service = SimulationService(
            session,
            data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"],
            now=lambda: arena["clock"][0],
        )
        reviews = await service.reviews(arena["experiment"].id, arena["participant"].id)
        failed = next(
            item for item in reviews if item.findings.startswith("AI review unavailable:")
        )
        assert failed.status == "failed" and not failed.memory_eligible


async def test_orphaned_request_reservation_recovers_as_unknown_without_model_replay(runtime_arena):
    from investment_agent.simulation.budget import SimulationBudget
    from investment_agent.simulation.contracts import AccountScope

    arena = runtime_arena
    participant = arena["participant"]

    async def guard():
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
        sessions=arena["sessions"],
        guard=guard,
    )
    await ledger.reserve(100)  # process dies before persisting a decision/output

    def unexpected(messages, info):
        pytest.fail("orphaned request cannot be silently sent again")

    recovered = await decide(arena, FunctionModel(unexpected))
    assert recovered.status == "unknown" and recovered.failure_code == "unknown_call_recovery"


async def test_paused_experiment_records_observation_without_new_paid_review(runtime_arena):
    from investment_agent.simulation.runtime import review_participants

    arena = runtime_arena
    await mature_arena(arena)
    async with arena["sessions"]() as session:
        svc = SimulationService(
            session,
            data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"],
            now=lambda: arena["clock"][0],
        )
        await svc.pause(arena["experiment"].id, expected_revision=1, idempotency_key="review-pause")

    def unexpected(messages, info):
        pytest.fail("paused simulation must not buy a model review")

    assert await review_participants(
        arena["experiment"].id, model=FunctionModel(unexpected), **arguments(arena)
    ) == ("observing_inactive_experiment",)
    async with arena["sessions"]() as session:
        assert len((await session.scalars(select(SimulationCostRecord))).all()) == 1


def test_method_comparison_freezes_model_but_allows_distinct_hashed_method_text(runtime_arena):
    from investment_agent.simulation.contracts import ConfigEntry, ParticipantConfig
    from investment_agent.simulation.runtime import text_hash

    arena = runtime_arena
    original = arena["participant"].config
    method = "Compare source durability and counter-evidence over the declared horizon."
    second = ParticipantConfig.model_validate(
        original.model_dump()
        | {
            "name": "alternative-method",
            "method_ref": "custom-method-v2",
            "method_hash": text_hash(method),
            "model_snapshot": tuple(
                ConfigEntry(
                    key=entry.key, value=method if entry.key == "method_text" else entry.value
                )
                for entry in original.model_snapshot
            ),
        }
    )
    config = ExperimentConfig.model_validate(
        arena["experiment"].config.model_dump()
        | {
            "mode": "method_comparison",
            "participants": (*arena["experiment"].config.participants[:2], original, second),
        }
    )
    frozen = freeze_experiment_models(config, arena["settings"])
    assert frozen.participants[-1].method_hash != frozen.participants[-2].method_hash
    assert (
        resolve_participant(frozen.participants[-1], arena["settings"]).version
        == resolve_participant(frozen.participants[-2], arena["settings"]).version
    )


async def test_completed_unpriced_call_without_decision_is_not_delivery_unknown_or_replayed(runtime_arena):
    from investment_agent.simulation.budget import SimulationBudget
    from investment_agent.simulation.contracts import AccountScope

    arena = runtime_arena
    participant = arena["participant"]
    async def allowed():
        return None
    ledger = SimulationBudget(scope=AccountScope(experiment_id=participant.experiment_id,
        participant_id=participant.id, account_id=participant.account_id), round_id=arena["round"].id,
        config=participant.config.budget.model_copy(update={"input_price_per_million": None,
            "output_price_per_million": None}), model_ref="selected/model", guard=allowed,
        sessions=arena["sessions"])
    identity = await ledger.reserve(100)
    await ledger.sent(identity)
    await ledger.settle(identity, {"input_tokens": 100, "output_tokens": 30}, sent=True)
    def unexpected(messages, info):
        pytest.fail("completed response lost before decision commit must not be reissued")
    recovered = await decide(arena, FunctionModel(unexpected))
    assert recovered.status == "failed" and recovered.failure_code == "model_output_not_committed"


async def test_stale_worker_cannot_publish_returned_model_output(runtime_arena):
    from investment_agent.storage.models.analysis import PipelineRun
    from investment_agent.storage.repositories.pipeline_run import (
        LeaseLostError,
        PipelineRunRepository,
        RunLease,
        lease_scope,
    )

    arena = runtime_arena
    async with arena["sessions"]() as session:
        repo = PipelineRunRepository(session)
        run, _ = await repo.enqueue(kind="simulation_decide", idempotency_key="lease-review",
            triggered_by="test", request_json={"experiment_id": arena["experiment"].id}, max_attempts=1)
        claimed = await repo.claim_next(worker_id="old-worker", kinds=["simulation_decide"], run_id=run.id)
        identity = claimed.id
        lease = RunLease(identity, "old-worker", claimed.attempt_count)
    async def stolen(messages, info):
        async with arena["sessions"]() as session:
            run = await session.get(PipelineRun, identity)
            run.status = "failed"
        return response()
    with lease_scope(lease), pytest.raises(LeaseLostError):
        await decide_participant(arena["experiment"].id, arena["round"].id, arena["participant"].id,
            model=FunctionModel(stolen), pipeline_run_id=identity, **arguments(arena))
    async with arena["sessions"]() as session:
        svc = SimulationService(session, data_dir=arena["settings"].data_dir,
            calendar=arena["calendar"], now=lambda: arena["clock"][0])
        assert not await svc.decisions(arena["experiment"].id, arena["participant"].id)
        cost = (await session.scalars(select(SimulationCostRecord))).one()
        assert cost.status == "settled"

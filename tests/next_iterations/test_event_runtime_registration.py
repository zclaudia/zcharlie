"""EV-09 isolated process configuration and enqueue-only scheduler wiring."""

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from investment_agent.cli.commands.worker import parse_kinds
from investment_agent.config import Settings
from investment_agent.research import worker as worker_module
from investment_agent.research.worker import (
    ResearchWorker,
    default_job_handlers,
    partition_worker_kinds,
)
from investment_agent.scheduler import runner

KIND = "research_events_maintenance"
ROOT = Path(__file__).resolve().parents[2]


def settings():
    return Settings(_env_file=None, api_key="ci-inert-api-key")


def test_dedicated_worker_does_not_construct_ordinary_handlers_or_own_singleton_duties(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("mechanical worker constructed ordinary research handlers")

    monkeypatch.setattr(worker_module, "default_job_handlers", forbidden)
    worker = ResearchWorker(settings=settings(), worker_id="events", kinds=parse_kinds(KIND))
    assert worker.kinds == [KIND] and not worker.owns_singleton_duties()


def test_ordinary_default_and_two_existing_partitions_never_claim_mechanical_jobs():
    base = settings()
    assert KIND not in {handler.kind for handler in default_job_handlers(base)}
    assert KIND not in ResearchWorker(settings=base, worker_id="ordinary").kinds
    agent, collection = partition_worker_kinds(base)
    assert KIND not in agent and KIND not in collection


@pytest.mark.parametrize("other", ["agent_turn", "ifind_research", "market_snapshot"])
def test_mechanical_kind_cannot_be_mixed_with_paid_or_collection_work(other):
    with pytest.raises(ValueError, match="must run alone"):
        ResearchWorker(settings=settings(), worker_id="mixed", kinds=[KIND, other])


def test_compose_has_exactly_one_dedicated_mechanical_service_without_starting_it():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    mechanical = services["worker-events"]
    assert mechanical["command"] == ["ia", "worker", "start", "--kinds", KIND]
    assert mechanical["environment"]["LIVENESS_FILE"] == "/tmp/worker-events.alive"
    assert mechanical["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    for name in ("worker", "worker-agent"):
        assert KIND not in services[name]["command"][-1].split(",")


async def test_empty_composition_does_not_fill_pipeline_with_empty_ticks(monkeypatch):
    from investment_agent.research_events import maintenance

    monkeypatch.setattr(maintenance, "get_maintenance_bindings", lambda: SimpleNamespace(consumers=(), wakeups=()))
    def forbidden():
        raise AssertionError("uninstalled consumers should not enqueue empty jobs")

    monkeypatch.setattr(runner, "session_scope", forbidden)
    assert await runner.run_research_events_dispatch() is None


async def test_independent_tick_keeps_running_while_slow_research_waits_and_cancels_cleanly(monkeypatch):
    entered, release, repeated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def slow_research():
        entered.set()
        await release.wait()

    async def dispatch():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary enqueue failure")
        if calls >= 3:
            repeated.set()

    monkeypatch.setattr(runner, "run_research_events_dispatch", dispatch)
    slow = asyncio.create_task(slow_research())
    tick = asyncio.create_task(runner.run_research_events_dispatch_loop(poll_seconds=0.001))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(repeated.wait(), 1)
        assert not slow.done() and not tick.done()
    finally:
        tick.cancel()
        with suppress(asyncio.CancelledError):
            await tick
        release.set()
        await slow
    frozen = calls
    await asyncio.sleep(0)
    assert calls == frozen and tick.cancelled()


async def test_invalid_dispatch_interval_is_rejected():
    assert runner.EVENT_DISPATCH_SECONDS == 30
    with pytest.raises(ValueError):
        await runner.run_research_events_dispatch_loop(poll_seconds=0)

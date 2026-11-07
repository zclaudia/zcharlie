"""Idle notification writes, snapshot maintenance, and the two-worker split."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import event

from investment_agent.agent.operations import AGENT_KINDS
from investment_agent.config import Settings
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.research.worker import ResearchWorker, partition_worker_kinds
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.support.publication import publication_db as publication_db
from tests.test_agent_operations import NOW, enqueue

REPO = Path(__file__).resolve().parents[2]


class _SnapshotNop(BaseJobHandler):
    kind = "market_snapshot"

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        return "completed"


def _trace_delivery_writes(engine):
    writes: list[str] = []

    def trace(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.split()).upper()
        if "NOTIFICATION_DELIVERY" in normalized and normalized.startswith(
            ("UPDATE", "INSERT", "DELETE")
        ):
            writes.append(normalized)

    event.listen(engine.sync_engine, "before_cursor_execute", trace)
    return writes


async def test_disabled_notifications_do_not_write_the_outbox(publication_db) -> None:
    from investment_agent.investor.notifications import deliver_one
    from tests.support.publication import base

    writes = _trace_delivery_writes(publication_db)
    assert await deliver_one(base()) is None
    assert writes == []


async def test_snapshot_claims_do_not_rerun_agent_maintenance(
    publication_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def maintain(self, limit=50):  # noqa: ANN001
        nonlocal calls
        calls += 1

    monkeypatch.setattr(
        "investment_agent.investor.monitoring.MonitoringService.maintain", maintain
    )
    worker = ResearchWorker(
        settings=Settings(_env_file=None, api_key="ci-inert-api-key"),
        worker_id="all-kinds",
        maintenance_interval_seconds=3600,
    )
    worker.handlers["market_snapshot"] = _SnapshotNop()
    async with session_scope() as session:
        repository = PipelineRunRepository(session)
        for index in range(3):
            await repository.enqueue(
                kind="market_snapshot",
                idempotency_key=f"snap-{index}",
                triggered_by="fixture",
                available_at=datetime.now(UTC),
            )
    for _ in range(3):
        assert await worker.run_once() is not None
    assert calls == 1
    assert worker._maintenance_at > 0


async def test_collection_worker_skips_singleton_maintenance(
    publication_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def maintain(self, limit=50):  # noqa: ANN001
        nonlocal calls
        calls += 1

    monkeypatch.setattr(
        "investment_agent.investor.monitoring.MonitoringService.maintain", maintain
    )
    writes = _trace_delivery_writes(publication_db)
    worker = ResearchWorker(
        settings=Settings(_env_file=None, api_key="ci-inert-api-key"),
        worker_id="collect",
        kinds=["market_snapshot"],
        maintenance_interval_seconds=0,
    )
    assert worker.owns_singleton_duties() is False
    assert await worker.run_once() is None
    assert calls == 0
    assert writes == []


async def test_disjoint_workers_claim_in_parallel(publication_db) -> None:
    agent_id, _task = await enqueue("agent_turn")
    async with session_scope() as session:
        snapshot, _created = await PipelineRunRepository(session).enqueue(
            kind="market_snapshot",
            idempotency_key="parallel-snapshot",
            triggered_by="fixture",
            available_at=NOW,
        )
        snapshot_id = snapshot.id

    async def claim(worker_id: str, kinds: list[str]):
        async with session_scope() as session:
            run = await PipelineRunRepository(session).claim_next(
                worker_id=worker_id, kinds=kinds, now=NOW
            )
            assert run is not None
            return run.id, run.kind

    agent_claim, snapshot_claim = await asyncio.gather(
        claim("agent-worker", list(AGENT_KINDS)),
        claim("collect-worker", ["market_snapshot"]),
    )
    assert agent_claim == (agent_id, "agent_turn")
    assert snapshot_claim == (snapshot_id, "market_snapshot")


def test_compose_workers_cover_every_kind_once() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    agent_command = services["worker-agent"]["command"]
    collection_command = services["worker"]["command"]
    assert agent_command[:4] == ["ia", "worker", "start", "--kinds"]
    assert collection_command[:4] == ["ia", "worker", "start", "--kinds"]
    settings = Settings(_env_file=None, api_key="ci-inert-api-key")
    agent, collection = partition_worker_kinds(settings)
    assert agent_command[4].split(",") == list(agent)
    assert collection_command[4].split(",") == list(collection)
    assert set(agent).isdisjoint(collection)
    # Review seats also run on the agent worker but are not an agent task kind.
    assert set(agent) == set(AGENT_KINDS) | {"review_seat", "simulation_decide", "simulation_review"}

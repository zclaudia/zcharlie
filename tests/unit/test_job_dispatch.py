"""Worker job dispatch through the kind -> handler registry."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from investment_agent.config import Settings
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        ifind_proxy_api_key="proxy-test",
        data_dir=tmp_path,
    )


class RecordingHandler(BaseJobHandler):
    def __init__(self, kind: str, *, error: Exception | None = None) -> None:
        self.kind = kind
        self.error = error
        self.executed: list[ClaimedJob] = []
        self.claimed: list[int] = []
        self.finalized: list[int] = []
        self.retries: list[int] = []
        self.recovered: list[tuple[int, bool]] = []

    async def on_claimed(self, session, job):
        self.claimed.append(job.run_id)

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        self.executed.append(job)

        async def work() -> None:
            if self.error is not None:
                raise self.error
            async with session_scope() as session:
                await PipelineRunRepository(session).mark_success(job.run_id, {"k": job.kind})

        await ctx.run_with_heartbeat(job.lease, work)
        return "completed"

    async def on_retry_scheduled(self, session, job, error):
        self.retries.append(job.run_id)

    async def on_recovered(self, session, run, *, requeued, now):
        self.recovered.append((run.id, requeued))

    async def finalize(self, job):
        self.finalized.append(job.run_id)


async def _enqueue(kind: str, key: str) -> int:
    async with session_scope() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind=kind, idempotency_key=key, triggered_by="test", request_json={"x": key}
        )
        return run.id


async def _get(run_id: int):
    async with session_scope() as session:
        return await PipelineRunRepository(session).get(run_id)


async def test_worker_dispatches_each_kind_to_its_handler(db_session, tmp_path):
    del db_session
    a, b = RecordingHandler("kind_a"), RecordingHandler("kind_b")
    run_a = await _enqueue("kind_a", "a")
    run_other = await _enqueue("ifind_research", "other")
    run_b = await _enqueue("kind_b", "b")
    worker = ResearchWorker(settings=_settings(tmp_path), worker_id="w", handlers=[a, b])

    assert await worker.run_once() == run_a
    assert await worker.run_once() == run_b
    # ifind_research has no handler in this worker, so it is never claimed.
    assert await worker.run_once() is None

    assert [job.run_id for job in a.executed] == [run_a]
    assert [job.run_id for job in b.executed] == [run_b]
    assert a.executed[0].request_json == {"x": "a"}
    assert a.claimed == a.finalized == [run_a]
    assert (await _get(run_a)).status == "success"
    assert (await _get(run_a)).metadata_json == {"k": "kind_a"}
    assert (await _get(run_other)).status == "queued"


async def test_worker_kinds_restricts_claims(db_session, tmp_path):
    del db_session
    a, b = RecordingHandler("kind_a"), RecordingHandler("kind_b")
    await _enqueue("kind_a", "a")
    run_b = await _enqueue("kind_b", "b")
    worker = ResearchWorker(
        settings=_settings(tmp_path), worker_id="w", handlers=[a, b], kinds=["kind_b"]
    )
    assert worker.kinds == ["kind_b"]
    assert await worker.run_once() == run_b
    assert await worker.run_once() is None
    assert a.executed == []


def test_worker_rejects_unknown_kinds(tmp_path):
    with pytest.raises(ValueError, match="no handler registered"):
        ResearchWorker(
            settings=_settings(tmp_path),
            worker_id="w",
            handlers=[RecordingHandler("kind_a")],
            kinds=["nope"],
        )


def test_default_registry_serves_research_and_snapshot_kinds(tmp_path):
    worker = ResearchWorker(settings=_settings(tmp_path), worker_id="w")
    assert set(worker.kinds) == {
        "ifind_research",
        "universe_sync",
        "industry_sync",
        "market_snapshot",
        "financial_snapshot",
        "snapshot_backfill",
        "dividend_snapshot",
        "screen_run",
        "ifind_verify",
    }


async def test_run_once_can_claim_a_specific_run(db_session, tmp_path):
    del db_session
    a = RecordingHandler("kind_a")
    first = await _enqueue("kind_a", "first")
    second = await _enqueue("kind_a", "second")
    worker = ResearchWorker(settings=_settings(tmp_path), worker_id="w", handlers=[a])
    assert await worker.run_once(run_id=second) == second
    assert (await _get(first)).status == "queued"


async def test_transient_handler_error_is_retried_via_handler_hook(db_session, tmp_path):
    del db_session
    flaky = RecordingHandler(
        "kind_a",
        error=httpx.ConnectError("down", request=httpx.Request("POST", "http://proxy")),
    )
    run_id = await _enqueue("kind_a", "a")
    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="w",
        handlers=[flaky],
        retry_base_delay_seconds=0,
    )
    assert await worker.run_once() == run_id
    run = await _get(run_id)
    assert run.status == "queued"
    assert flaky.retries == [run_id]
    assert flaky.finalized == [run_id]


async def test_terminal_handler_error_fails_the_run(db_session, tmp_path):
    del db_session
    broken = RecordingHandler("kind_a", error=ValueError("bad payload"))
    run_id = await _enqueue("kind_a", "a")
    worker = ResearchWorker(settings=_settings(tmp_path), worker_id="w", handlers=[broken])
    await worker.run_once()
    run = await _get(run_id)
    assert run.status == "failed"
    assert run.error_summary == "bad payload"


async def test_recover_stale_covers_all_registered_kinds(db_session, tmp_path):
    del db_session
    a, b = RecordingHandler("kind_a"), RecordingHandler("kind_b")
    ids = {
        kind: await _enqueue(kind, kind) for kind in ("kind_a", "kind_b", "unregistered")
    }
    old = datetime.now(UTC) - timedelta(hours=1)
    async with session_scope() as session:
        repository = PipelineRunRepository(session)
        for _ in ids:
            run = await repository.claim_next(worker_id="dead")
            assert run is not None
            run.heartbeat_at = old
            run.claimed_at = old
            run.started_at = old
        await session.flush()
    worker = ResearchWorker(settings=_settings(tmp_path), worker_id="w", handlers=[a, b])
    requeued, failed = await worker.recover_stale(stale_after_seconds=60)
    assert sorted(requeued) == sorted([ids["kind_a"], ids["kind_b"]])
    assert failed == []
    assert a.recovered == [(ids["kind_a"], True)]
    assert b.recovered == [(ids["kind_b"], True)]
    assert (await _get(ids["unregistered"])).status == "running"


async def test_claim_next_accepts_kind_list(db_session):
    repository = PipelineRunRepository(db_session)
    first, _ = await repository.enqueue(kind="x", idempotency_key="1", triggered_by="t")
    second, _ = await repository.enqueue(kind="y", idempotency_key="2", triggered_by="t")
    claimed = await repository.claim_next(worker_id="w", kinds=["y"])
    assert claimed is not None and claimed.id == second.id
    assert await repository.claim_next(worker_id="w", kinds=["z"]) is None
    claimed = await repository.claim_next(worker_id="w", kinds=["x", "y"])
    assert claimed is not None and claimed.id == first.id

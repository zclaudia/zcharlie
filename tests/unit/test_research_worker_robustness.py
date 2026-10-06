"""Regression tests for research queue / worker robustness."""

import asyncio
import contextlib
import errno
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from sqlalchemy.exc import OperationalError

from investment_agent.config import Settings
from investment_agent.research import ifind_chain
from investment_agent.research import worker as worker_module
from investment_agent.research.ifind_chain import (
    CanonicalBundle,
    ResearchChainResult,
    ResearchExplanation,
    ResearchPartialError,
    run_ifind_research_chain,
)
from investment_agent.research.retry_policy import (
    ResearchRetryableError,
    is_transient_error,
)
from investment_agent.research.worker import ResearchWorker, run_worker
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import (  # noqa: F401
    analysis,
    market_data,
    ops,
    pit_snapshot,
    reference,
    relationship,
    user_domain,
)
from investment_agent.storage.models.base import Base
from investment_agent.storage.repositories.pipeline_run import (
    PipelineRunRepository,
    RunLease,
    lease_scope,
)
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel" / "600406"
PAYLOAD = {
    "ticker": "600406.SH",
    "industry": "电力设备",
    "report_year": 2025,
    "as_of": "2026-10-05",
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        ifind_proxy_api_key="proxy-test",
        data_dir=tmp_path,
    )


def _result(run_id: int, tmp_path: Path) -> ResearchChainResult:
    return ResearchChainResult(
        run_id=run_id,
        artifact_dir=tmp_path,
        score={},
        explanation=ResearchExplanation(
            summary="fixture",
            classification_rationale=[],
            positive_evidence=[],
            negative_evidence=[],
            risk_factors=[],
            data_quality_notes=[],
            open_questions=[],
            workflow_conclusion="needs_review",
        ),
    )


async def _enqueue(key: str, *, max_attempts: int = 3) -> int:
    async with session_scope() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key=key,
            triggered_by="api",
            request_json=PAYLOAD,
            max_attempts=max_attempts,
        )
        return run.id


async def _get(run_id: int):
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
        assert run is not None
        return run


async def _steal(run_id: int, *, worker_id: str = "other-worker") -> None:
    """Simulate stale recovery + a re-claim by another worker."""

    async with session_scope() as session:
        repository = PipelineRunRepository(session)
        run = await repository.get(run_id)
        assert run is not None
        run.status = "queued"
        run.worker_id = None
        await session.flush()
        claimed = await repository.claim_next(worker_id=worker_id, kind="ifind_research")
        assert claimed is not None and claimed.id == run_id


# --- transient classification -------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        TimeoutError(),
        ConnectionResetError(),
        OperationalError("UPDATE x", {}, Exception("database is locked")),
        openai.APITimeoutError(request=httpx.Request("POST", "http://llm.test")),
        openai.APIConnectionError(request=httpx.Request("POST", "http://llm.test")),
        openai.RateLimitError(
            "slow down",
            response=httpx.Response(429, request=httpx.Request("POST", "http://llm.test")),
            body=None,
        ),
        ModelHTTPError(503, "m"),
        ResearchRetryableError(run_id=1, reason="explanation failed"),
    ],
)
def test_transient_errors_are_retried(exc: BaseException) -> None:
    assert is_transient_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        FileExistsError("artifact dir exists"),
        PermissionError("denied"),
        OSError(errno.ENOSPC, "No space left on device"),
        ValueError("bad payload"),
        OperationalError("SELECT", {}, Exception("no such table: x")),
        openai.BadRequestError(
            "bad",
            response=httpx.Response(400, request=httpx.Request("POST", "http://llm.test")),
            body=None,
        ),
        ModelHTTPError(400, "m"),
    ],
)
def test_terminal_errors_are_not_retried(exc: BaseException) -> None:
    assert is_transient_error(exc) is False


async def test_local_os_error_fails_terminally(db_session, tmp_path):
    del db_session
    run_id = await _enqueue("disk-full")

    async def disk_full_runner(**_kwargs: object) -> ResearchChainResult:
        raise OSError(errno.ENOSPC, "No space left on device")

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="w",
        runner=disk_full_runner,
        retry_base_delay_seconds=0,
    )
    assert await worker.run_once() == run_id
    run = await _get(run_id)
    assert run.status == "failed"
    assert run.attempt_count == 1


# --- heartbeat + lease fencing in the worker ----------------------------------


async def test_worker_heartbeats_while_runner_executes(db_session, tmp_path):
    del db_session
    run_id = await _enqueue("heartbeat")
    seen: list[datetime] = []

    async def slow_runner(**_kwargs: object) -> ResearchChainResult:
        claimed = await _get(run_id)
        assert claimed.heartbeat_at is not None
        seen.append(claimed.heartbeat_at)
        await asyncio.sleep(0.2)
        refreshed = await _get(run_id)
        assert refreshed.heartbeat_at is not None
        seen.append(refreshed.heartbeat_at)
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(run_id)
        return _result(run_id, tmp_path)

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="w",
        runner=slow_runner,
        heartbeat_interval_seconds=0.02,
    )
    assert await worker.run_once() == run_id
    assert seen[1] > seen[0]
    assert (await _get(run_id)).status == "success"


async def test_heartbeat_lease_loss_cancels_runner_without_clobbering(
    db_session, tmp_path
):
    del db_session
    run_id = await _enqueue("lease-lost")
    cancelled = asyncio.Event()

    async def hijacked_runner(**_kwargs: object) -> ResearchChainResult:
        await _steal(run_id)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("runner should have been cancelled")

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="w",
        runner=hijacked_runner,
        heartbeat_interval_seconds=0.02,
    )
    assert await asyncio.wait_for(worker.run_once(), timeout=3) == run_id
    assert cancelled.is_set()
    run = await _get(run_id)
    assert run.status == "running"
    assert run.worker_id == "other-worker"
    assert run.attempt_count == 2


async def test_stale_worker_failure_does_not_requeue_live_claim(db_session, tmp_path):
    del db_session
    run_id = await _enqueue("stale-retry")

    async def stale_runner(**_kwargs: object) -> ResearchChainResult:
        await _steal(run_id)
        raise httpx.ReadTimeout("proxy timeout")

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="w",
        runner=stale_runner,
        retry_base_delay_seconds=0,
    )
    assert await worker.run_once() == run_id
    run = await _get(run_id)
    assert run.status == "running"
    assert run.worker_id == "other-worker"
    assert run.error_summary is None


async def test_stale_worker_success_is_rejected(db_session, tmp_path):
    del db_session
    run_id = await _enqueue("stale-success")

    async def stale_runner(**_kwargs: object) -> ResearchChainResult:
        await _steal(run_id)
        async with session_scope() as session:
            # The chain's own lifecycle call: fenced via the active lease scope.
            await PipelineRunRepository(session).mark_success(run_id)
        return _result(run_id, tmp_path)

    worker = ResearchWorker(settings=_settings(tmp_path), worker_id="w", runner=stale_runner)
    assert await worker.run_once() == run_id
    run = await _get(run_id)
    assert run.status == "running"
    assert run.worker_id == "other-worker"


async def test_watchlist_sync_failure_does_not_escape_run_once(
    db_session, tmp_path, monkeypatch
):
    del db_session
    run_id = await _enqueue("sync-failure")
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
        assert run is not None
        # Any listing id takes the watchlist-sync path (no watchlist row needed).
        run.listing_id = 999_999

    async def ok_runner(**_kwargs: object) -> ResearchChainResult:
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(run_id)
        return _result(run_id, tmp_path)

    async def broken_sync(**_kwargs: object) -> None:
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    async def no_notify(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker_module, "_sync_watchlist_refresh", broken_sync)
    monkeypatch.setattr(worker_module, "_notify_meaningful_change", no_notify)

    worker =ResearchWorker(settings=_settings(tmp_path), worker_id="w", runner=ok_runner)
    assert await worker.run_once() == run_id
    assert (await _get(run_id)).status == "success"


# --- worker loop --------------------------------------------------------------


class _ScriptedWorker:
    def __init__(self, outcomes: list[object], done: asyncio.Event) -> None:
        self.outcomes = outcomes
        self.done = done
        self.recoveries = 0
        self.calls = 0

    async def recover_stale(self, *, stale_after_seconds: float):
        del stale_after_seconds
        self.recoveries += 1
        return [], []

    async def run_once(self) -> int | None:
        self.calls += 1
        if not self.outcomes:
            self.done.set()
            return None
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


async def test_worker_loop_survives_errors_and_recovers_periodically(tmp_path):
    done = asyncio.Event()
    scripted = _ScriptedWorker(
        [
            OperationalError("UPDATE", {}, Exception("database is locked")),
            RuntimeError("unexpected"),
            None,
            None,
        ],
        done,
    )
    task = asyncio.create_task(
        run_worker(
            settings=_settings(tmp_path),
            worker_id="loop",
            poll_interval_seconds=0.001,
            stale_after_seconds=60,
            recovery_interval_seconds=0,
            worker=scripted,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(done.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scripted.calls >= 5
    # Recovery runs on every iteration when the interval is 0, not just at startup.
    assert scripted.recoveries >= scripted.calls


async def test_worker_loop_recovery_failure_is_not_fatal(tmp_path):
    done = asyncio.Event()

    class FlakyRecovery(_ScriptedWorker):
        async def recover_stale(self, *, stale_after_seconds: float):
            self.recoveries += 1
            if self.recoveries == 1:
                raise OperationalError("SELECT", {}, Exception("database is locked"))
            return [], []

    flaky = FlakyRecovery([None], done)
    task = asyncio.create_task(
        run_worker(
            settings=_settings(tmp_path),
            worker_id="loop",
            poll_interval_seconds=0.001,
            recovery_interval_seconds=0,
            worker=flaky,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(done.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert flaky.recoveries >= 2


async def test_worker_recovers_orphans_after_startup(tmp_path):
    """A run orphaned while the worker is already idling is still recovered."""

    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    completed = asyncio.Event()
    idle = asyncio.Event()
    holder: dict[str, int] = {}

    async def ok_runner(**_kwargs: object) -> ResearchChainResult:
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(holder["run_id"])
        completed.set()
        return _result(holder["run_id"], tmp_path)

    class IdleTrackingWorker(ResearchWorker):
        async def run_once(self) -> int | None:
            processed = await super().run_once()
            if processed is None:
                idle.set()
            return processed

    live = IdleTrackingWorker(settings=_settings(tmp_path), worker_id="live", runner=ok_runner)
    task = asyncio.create_task(
        run_worker(
            settings=_settings(tmp_path),
            worker_id="live",
            poll_interval_seconds=0.001,
            stale_after_seconds=60,
            recovery_interval_seconds=0,
            worker=live,
        )
    )
    try:
        await asyncio.wait_for(idle.wait(), timeout=3)
        # Orphan appears only after the worker's startup recovery already ran.
        async with session_scope() as session:
            repository = PipelineRunRepository(session)
            run, _ = await repository.enqueue(
                kind="ifind_research",
                idempotency_key="orphan",
                triggered_by="api",
                request_json=PAYLOAD,
            )
            claimed = await repository.claim_next(
                worker_id="dead-worker", kind="ifind_research"
            )
            assert claimed is not None and claimed.id == run.id
            stale = datetime.now(UTC) - timedelta(minutes=10)
            claimed.claimed_at = stale
            claimed.heartbeat_at = stale
            holder["run_id"] = run.id
        await asyncio.wait_for(completed.wait(), timeout=3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        run_after = await _get(holder["run_id"]) if "run_id" in holder else None
        await engine.dispose()
    assert run_after is not None
    assert run_after.status == "success"
    assert run_after.worker_id == "live"
    assert run_after.attempt_count == 2


# --- single retry-or-terminal decision in the chain ---------------------------


def _bundle() -> CanonicalBundle:
    return CanonicalBundle(
        pool=json.loads((FIXTURE_ROOT / "pool.json").read_text(encoding="utf-8")),
        market=json.loads((FIXTURE_ROOT / "market.json").read_text(encoding="utf-8")),
        financial=json.loads((FIXTURE_ROOT / "financial.json").read_text(encoding="utf-8")),
        raw={},
    )


async def _claim(worker_id: str = "w") -> RunLease:
    async with session_scope() as session:
        claimed = await PipelineRunRepository(session).claim_next(
            worker_id=worker_id, kind="ifind_research"
        )
        assert claimed is not None
        return RunLease(run_id=claimed.id, worker_id=worker_id, attempt=claimed.attempt_count)


async def _run_chain(tmp_path: Path, lease: RunLease) -> ResearchChainResult:
    with lease_scope(lease):
        return await run_ifind_research_chain(
            settings=_settings(tmp_path),
            proxy_url="http://unused.invalid",
            proxy_api_key="unused",
            ticker="600406.SH",
            industry="电力设备",
            report_year=2025,
            as_of=date(2026, 10, 5),
            output_root=tmp_path / "research-runs",
            triggered_by="worker",
            claimed_run_id=lease.run_id,
        )


async def test_chain_leaves_failure_decision_to_worker(db_session, tmp_path, monkeypatch):
    del db_session
    await _enqueue("chain-fail")

    async def failing_fetch(*_args, **_kwargs) -> CanonicalBundle:
        raise httpx.ConnectError("proxy down")

    monkeypatch.setattr(ifind_chain, "fetch_canonical_bundle", failing_fetch)
    lease = await _claim()
    with pytest.raises(httpx.ConnectError):
        await _run_chain(tmp_path, lease)
    # No intermediate `failed` commit: the worker transitions atomically.
    run = await _get(lease.run_id)
    assert run.status == "running"
    assert run.finished_at is None


async def test_transient_llm_failure_is_retried_then_partial(
    db_session, tmp_path, monkeypatch
):
    del db_session
    run_id = await _enqueue("llm-timeout", max_attempts=2)

    async def fake_fetch(*_args, **_kwargs) -> CanonicalBundle:
        return _bundle()

    async def timeout_explanation(**_kwargs) -> ResearchExplanation:
        raise openai.APITimeoutError(request=httpx.Request("POST", "http://llm.test"))

    monkeypatch.setattr(ifind_chain, "fetch_canonical_bundle", fake_fetch)
    monkeypatch.setattr(ifind_chain, "_explain_score", timeout_explanation)

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="w",
        retry_base_delay_seconds=0,
    )
    assert await worker.run_once() == run_id
    first = await _get(run_id)
    assert first.status == "queued"
    assert "APITimeoutError" in (first.error_summary or "")
    async with session_scope() as session:
        assert await ResearchSnapshotRepository(session).by_run(run_id) is None

    # Final attempt: fall back to partial, preserving the deterministic score.
    assert await worker.run_once() == run_id
    final = await _get(run_id)
    assert final.status == "partial"
    assert final.attempt_count == 2
    assert final.metadata_json["partial_reason"] == "explanation_failed"
    async with session_scope() as session:
        snapshot = await ResearchSnapshotRepository(session).by_run(run_id)
    assert snapshot is not None
    assert snapshot.explanation_json is None
    assert snapshot.score_json["business_score"] == 55.22


async def test_non_transient_llm_failure_is_partial_immediately(
    db_session, tmp_path, monkeypatch
):
    del db_session
    await _enqueue("llm-invalid")

    async def fake_fetch(*_args, **_kwargs) -> CanonicalBundle:
        return _bundle()

    async def invalid_explanation(**_kwargs) -> ResearchExplanation:
        raise ValueError("Agent cited unknown canonical fields")

    monkeypatch.setattr(ifind_chain, "fetch_canonical_bundle", fake_fetch)
    monkeypatch.setattr(ifind_chain, "_explain_score", invalid_explanation)
    lease = await _claim()
    with pytest.raises(ResearchPartialError):
        await _run_chain(tmp_path, lease)
    run = await _get(lease.run_id)
    assert run.status == "partial"
    assert run.attempt_count == 1


# --- idempotent change notifications ------------------------------------------


async def test_change_notification_is_written_once(tmp_path, monkeypatch):
    change_set = SimpleNamespace(
        has_meaningful_changes=True,
        notification_path=None,
        diff_json={"previous_run_id": 1, "changes": []},
    )
    snapshot = SimpleNamespace(
        symbol="600406", market="SH", company_name="国电南瑞", as_of=date(2026, 10, 5)
    )

    async def by_run_change(_self, _run_id):
        return change_set

    async def by_run_snapshot(_self, _run_id):
        return snapshot

    mark_calls: list[str] = []

    async def flaky_mark_notified(_self, _run_id, *, path):
        mark_calls.append(path)
        if len(mark_calls) == 1:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        change_set.notification_path = path
        return change_set

    sends = 0
    original_send = worker_module.FileNotifier.send

    async def counting_send(self, content, channel_metadata):
        nonlocal sends
        sends += 1
        return await original_send(self, content, channel_metadata)

    class _NullSession:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(worker_module, "session_scope", _NullSession)
    monkeypatch.setattr(worker_module.ResearchChangeRepository, "by_run", by_run_change)
    monkeypatch.setattr(worker_module.ResearchSnapshotRepository, "by_run", by_run_snapshot)
    monkeypatch.setattr(
        worker_module.ResearchChangeRepository, "mark_notified", flaky_mark_notified
    )
    monkeypatch.setattr(worker_module.FileNotifier, "send", counting_send)
    settings = _settings(tmp_path)

    with pytest.raises(OperationalError):
        await worker_module._notify_meaningful_change(settings=settings, run_id=7)
    path = await worker_module._notify_meaningful_change(settings=settings, run_id=7)

    assert sends == 1
    assert path is not None
    assert mark_calls == [path, path]
    assert _read(path).startswith("# 研究变化提醒")
    assert list(Path(path).parent.iterdir()) == [Path(path)]
    # Once recorded, later calls reuse the existing file without rewriting it.
    assert await worker_module._notify_meaningful_change(settings=settings, run_id=7) == path
    assert sends == 1


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")

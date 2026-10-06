from datetime import UTC, date, datetime, timedelta

import httpx

from investment_agent.config import Settings
from investment_agent.research.ifind_chain import (
    ResearchChainResult,
    ResearchExplanation,
    ResearchPartialError,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        ifind_proxy_api_key="proxy-test",
        data_dir=tmp_path,
    )


async def _seed_watchlist_item(
    session,
    *,
    ticker: str = "600406",
    company_name: str = "国电南瑞",
) -> int:
    if await session.get(Market, "SSE") is None:
        session.add(
            Market(
                code="SSE",
                name="Shanghai Stock Exchange",
                timezone="Asia/Shanghai",
                currency="CNY",
            )
        )
        await session.flush()
    listing = await ListingRepository(session).upsert_listing(
        market_code="SSE",
        ticker=ticker,
        company_name=company_name,
    )
    await WatchlistRepository(session).add(
        listing.id,
        industry="电力设备",
        provider="ifind",
        report_year=2025,
    )
    return listing.id


async def test_worker_claims_and_executes_queued_research(db_session, tmp_path):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        listing_id = await _seed_watchlist_item(session)
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key="worker-success",
            triggered_by="api",
            listing_id=listing_id,
            request_json={
                "ticker": "600406.SH",
                "industry": "电力设备",
                "report_year": 2025,
                "as_of": "2026-10-05",
            },
        )
        run_id = run.id

    captured: dict[str, object] = {}

    async def fake_runner(**kwargs: object) -> ResearchChainResult:
        captured.update(kwargs)
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(run_id)
        return ResearchChainResult(
            run_id=run_id,
            artifact_dir=tmp_path / "research-runs" / str(run_id),
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

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="test-worker",
        runner=fake_runner,
    )
    processed = await worker.run_once()

    assert processed == run_id
    assert captured["claimed_run_id"] == run_id
    assert captured["ticker"] == "600406.SH"
    assert captured["as_of"] == date(2026, 10, 5)
    async with session_scope() as session:
        persisted = await PipelineRunRepository(session).get(run_id)
        assert persisted is not None
        assert persisted.status == "success"
        assert persisted.worker_id == "test-worker"
        assert persisted.attempt_count == 1
        item = await WatchlistRepository(session).get_by_listing(listing_id)
        assert item is not None
        assert item.last_refresh_status == "success"
        assert item.last_research_run_id == run_id
        assert item.last_refreshed_at is not None


async def test_worker_marks_corrupt_payload_failed(db_session, tmp_path):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        listing_id = await _seed_watchlist_item(session)
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key="worker-invalid",
            triggered_by="api",
            listing_id=listing_id,
            request_json={"ticker": "not-a-ticker"},
        )
        run_id = run.id

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="test-worker",
    )
    processed = await worker.run_once()

    assert processed == run_id
    async with session_scope() as session:
        persisted = await PipelineRunRepository(session).get(run_id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.stage == "failed"
        assert "validation errors" in (persisted.error_summary or "")
        item = await WatchlistRepository(session).get_by_listing(listing_id)
        assert item is not None
        assert item.last_refresh_status == "failed"
        assert item.last_research_run_id == run_id
        assert "validation errors" in (item.last_refresh_error or "")


async def test_worker_preserves_partial_status_from_runner(db_session, tmp_path):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        listing_id = await _seed_watchlist_item(session)
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key="worker-partial",
            triggered_by="api",
            listing_id=listing_id,
            request_json={
                "ticker": "600406.SH",
                "industry": "电力设备",
                "report_year": 2025,
                "as_of": "2026-10-05",
            },
        )
        run_id = run.id

    async def partial_runner(**_kwargs: object) -> ResearchChainResult:
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_partial(
                run_id,
                "explanation unavailable",
                {"partial_reason": "explanation_failed"},
            )
        raise ResearchPartialError(
            run_id=run_id,
            reason="explanation unavailable",
        )

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="test-worker",
        runner=partial_runner,
    )
    processed = await worker.run_once()

    assert processed == run_id
    async with session_scope() as session:
        persisted = await PipelineRunRepository(session).get(run_id)
        assert persisted is not None
        assert persisted.status == "partial"
        assert persisted.metadata_json["partial_reason"] == "explanation_failed"
        item = await WatchlistRepository(session).get_by_listing(listing_id)
        assert item is not None
        assert item.last_refresh_status == "partial"
        assert item.last_research_run_id == run_id
        assert item.last_refresh_error == "explanation unavailable"


async def test_worker_recovery_syncs_terminal_watchlist_failure(db_session, tmp_path):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        listing_id = await _seed_watchlist_item(session)
        repository = PipelineRunRepository(session)
        run, _ = await repository.enqueue(
            kind="ifind_research",
            idempotency_key="worker-stale",
            triggered_by="watchlist_api",
            listing_id=listing_id,
            request_json={
                "ticker": "600406.SH",
                "industry": "电力设备",
                "report_year": 2025,
                "as_of": "2026-10-05",
            },
            max_attempts=1,
        )
        claimed = await repository.claim_next(
            worker_id="abandoned-worker",
            kind="ifind_research",
        )
        assert claimed is not None
        claimed.heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
        await WatchlistRepository(session).mark_refresh_queued(
            listing_id,
            run_id=run.id,
            status="running",
        )
        run_id = run.id

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="recovery-worker",
    )
    requeued, failed = await worker.recover_stale(stale_after_seconds=60)

    assert requeued == []
    assert failed == [run_id]
    async with session_scope() as session:
        item = await WatchlistRepository(session).get_by_listing(listing_id)
        assert item is not None
        assert item.last_refresh_status == "failed"
        assert item.last_research_run_id == run_id
        assert item.last_refreshed_at is not None
        assert item.last_refresh_error == "worker heartbeat expired after max attempts"


async def test_one_watchlist_job_failure_does_not_block_the_next(db_session, tmp_path):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        first_listing_id = await _seed_watchlist_item(session)
        second_listing_id = await _seed_watchlist_item(
            session,
            ticker="601398",
            company_name="工商银行",
        )
        repository = PipelineRunRepository(session)
        first, _ = await repository.enqueue(
            kind="ifind_research",
            idempotency_key="isolated-first",
            triggered_by="watchlist_api",
            listing_id=first_listing_id,
            request_json={
                "ticker": "600406.SH",
                "industry": "电力设备",
                "report_year": 2025,
                "as_of": "2026-10-05",
            },
        )
        second, _ = await repository.enqueue(
            kind="ifind_research",
            idempotency_key="isolated-second",
            triggered_by="watchlist_api",
            listing_id=second_listing_id,
            request_json={
                "ticker": "601398.SH",
                "industry": "银行",
                "report_year": 2025,
                "as_of": "2026-10-05",
            },
        )

    async def isolated_runner(**kwargs: object) -> ResearchChainResult:
        claimed_run_id = kwargs["claimed_run_id"]
        if claimed_run_id == first.id:
            raise RuntimeError("provider rejected first symbol")
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(second.id)
        return ResearchChainResult(
            run_id=second.id,
            artifact_dir=tmp_path / "research-runs" / str(second.id),
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

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="isolation-worker",
        runner=isolated_runner,
    )
    assert await worker.run_once() == first.id
    assert await worker.run_once() == second.id

    async with session_scope() as session:
        first_item = await WatchlistRepository(session).get_by_listing(first_listing_id)
        second_item = await WatchlistRepository(session).get_by_listing(second_listing_id)
        assert first_item is not None
        assert second_item is not None
        assert first_item.last_refresh_status == "failed"
        assert "provider rejected first symbol" in (first_item.last_refresh_error or "")
        assert second_item.last_refresh_status == "success"
        assert second_item.last_refresh_error is None


async def test_transient_failure_retries_with_same_run_and_bounded_attempts(
    db_session,
    tmp_path,
):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        listing_id = await _seed_watchlist_item(session)
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key="transient-retry",
            triggered_by="watchlist_api",
            listing_id=listing_id,
            request_json={
                "ticker": "600406.SH",
                "industry": "电力设备",
                "report_year": 2025,
                "as_of": "2026-10-05",
            },
            max_attempts=2,
        )
        await WatchlistRepository(session).mark_refresh_queued(
            listing_id,
            run_id=run.id,
        )
        run_id = run.id

    attempts = 0

    async def flaky_runner(**_kwargs: object) -> ResearchChainResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout(
                "proxy timeout",
                request=httpx.Request("POST", "http://ifind.test/stock/info"),
            )
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(run_id)
        return ResearchChainResult(
            run_id=run_id,
            artifact_dir=tmp_path / "research-runs" / str(run_id) / "attempt-2",
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

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="retry-worker",
        runner=flaky_runner,
        retry_base_delay_seconds=0,
    )
    assert await worker.run_once() == run_id

    async with session_scope() as session:
        queued = await PipelineRunRepository(session).get(run_id)
        item = await WatchlistRepository(session).get_by_listing(listing_id)
        assert queued is not None
        assert item is not None
        assert queued.status == "queued"
        assert queued.attempt_count == 1
        assert "retry scheduled" in (queued.error_summary or "")
        assert item.last_refresh_status == "queued"
        assert "proxy timeout" in (item.last_refresh_error or "")

    assert await worker.run_once() == run_id
    async with session_scope() as session:
        completed = await PipelineRunRepository(session).get(run_id)
        item = await WatchlistRepository(session).get_by_listing(listing_id)
        assert completed is not None
        assert item is not None
        assert completed.status == "success"
        assert completed.attempt_count == 2
        assert item.last_refresh_status == "success"
        assert item.last_refresh_error is None


async def test_transient_failure_stops_at_max_attempts(db_session, tmp_path):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key="transient-exhausted",
            triggered_by="api",
            request_json={
                "ticker": "600406.SH",
                "industry": "电力设备",
                "report_year": 2025,
                "as_of": "2026-10-05",
            },
            max_attempts=1,
        )
        run_id = run.id

    async def timeout_runner(**_kwargs: object) -> ResearchChainResult:
        raise httpx.ReadTimeout(
            "proxy timeout",
            request=httpx.Request("POST", "http://ifind.test/stock/info"),
        )

    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="retry-worker",
        runner=timeout_runner,
        retry_base_delay_seconds=0,
    )
    assert await worker.run_once() == run_id

    async with session_scope() as session:
        exhausted = await PipelineRunRepository(session).get(run_id)
        assert exhausted is not None
        assert exhausted.status == "failed"
        assert exhausted.attempt_count == 1
        assert exhausted.finished_at is not None

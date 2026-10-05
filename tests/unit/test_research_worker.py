from datetime import date

from investment_agent.config import Settings
from investment_agent.research.ifind_chain import (
    ResearchChainResult,
    ResearchExplanation,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier2_api_key="test",
        llm_tier3_api_key="test",
        ifind_proxy_api_key="proxy-test",
        data_dir=tmp_path,
    )


async def test_worker_claims_and_executes_queued_research(db_session, tmp_path):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key="worker-success",
            triggered_by="api",
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


async def test_worker_marks_corrupt_payload_failed(db_session, tmp_path):
    del db_session  # initializes the shared in-memory engine and schema
    async with session_scope() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key="worker-invalid",
            triggered_by="api",
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

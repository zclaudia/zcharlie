import json
from datetime import date
from pathlib import Path

import pytest

from investment_agent.config import Settings
from investment_agent.research import ifind_chain
from investment_agent.research.ifind_chain import (
    CanonicalBundle,
    EvidenceClaim,
    ResearchExplanation,
    ResearchPartialError,
    _artifact_dir_for_attempt,
    _return_stats,
    parse_proxy_rows,
    run_ifind_research_chain,
    validate_explanation_fields,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)


def test_parse_proxy_rows_unwraps_data_preview_csv():
    body = {
        "format": "json",
        "data": {"data_preview": "code,close\n600406.SH,22.52\n"},
    }

    assert parse_proxy_rows(body) == [{"code": "600406.SH", "close": "22.52"}]


def test_return_stats_are_deterministic():
    rows = [
        {"time": "20260101", "close": "10"},
        {"time": "20260102", "close": "12"},
        {"time": "20260103", "close": "9"},
    ]

    total_return, volatility, drawdown = _return_stats(rows)

    assert total_return == -0.1
    assert volatility is not None and volatility > 0
    assert drawdown == 0.25


def test_explanation_rejects_unknown_canonical_field():
    explanation = ResearchExplanation(
        summary="summary",
        classification_rationale=[
            EvidenceClaim(text="claim", supporting_fields=["key_metrics.not_real"])
        ],
        positive_evidence=[],
        negative_evidence=[],
        risk_factors=[],
        data_quality_notes=[],
        open_questions=[],
        workflow_conclusion="needs_review",
    )

    try:
        validate_explanation_fields(explanation, {"key_metrics": {"roe": 0.16}})
    except ValueError as exc:
        assert "key_metrics.not_real" in str(exc)
    else:
        raise AssertionError("unknown field should fail validation")


def test_retry_attempt_uses_isolated_artifact_directory(tmp_path):
    assert _artifact_dir_for_attempt(
        output_root=tmp_path,
        run_id=42,
        attempt_count=1,
    ) == tmp_path / "42"
    assert _artifact_dir_for_attempt(
        output_root=tmp_path,
        run_id=42,
        attempt_count=2,
    ) == tmp_path / "42" / "attempt-2"


async def test_successful_idempotent_run_replays_persisted_snapshot(
    db_session,
    tmp_path,
):
    del db_session  # initializes the shared in-memory engine and schema
    explanation = ResearchExplanation(
        summary="persisted",
        classification_rationale=[],
        positive_evidence=[],
        negative_evidence=[],
        risk_factors=[],
        data_quality_notes=[],
        open_questions=[],
        workflow_conclusion="needs_review",
    )
    report_path = tmp_path / "existing" / "agent_report.md"
    report_path.parent.mkdir()
    report_path.write_text("fixture", encoding="utf-8")
    async with session_scope() as session:
        run_repository = PipelineRunRepository(session)
        run, _ = await run_repository.enqueue(
            kind="ifind_research",
            idempotency_key="existing-success",
            triggered_by="api",
        )
        await run_repository.mark_running(run.id, stage="fetching")
        await ResearchSnapshotRepository(session).create(
            pipeline_run_id=run.id,
            symbol="600406",
            market="SH",
            company_name="国电南瑞",
            industry="电力设备",
            as_of=date(2026, 10, 5),
            report_year=2025,
            canonical_schema_version="ifind-canonical.v1",
            scoring_version="v1.2",
            score_schema_version="v2.0",
            score_json={"business_score": 55.22},
            explanation_json=explanation.model_dump(mode="json"),
            artifact_manifest={"agent_report": str(report_path)},
        )
        await run_repository.mark_success(run.id)
        run_id = run.id

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier2_api_key="test",
        llm_tier3_api_key="test",
    )
    result = await run_ifind_research_chain(
        settings=settings,
        proxy_url="http://should-not-be-called.invalid",
        proxy_api_key="unused",
        ticker="600406.SH",
        industry="电力设备",
        report_year=2025,
        as_of=date(2026, 10, 5),
        output_root=tmp_path / "new-runs",
        triggered_by="api",
        idempotency_key="existing-success",
    )

    assert result.run_id == run_id
    assert result.score["business_score"] == 55.22
    assert result.explanation.summary == "persisted"
    assert not (tmp_path / "new-runs").exists()


async def test_explanation_failure_persists_partial_deterministic_result(
    db_session,
    tmp_path,
    monkeypatch,
):
    del db_session  # initializes the shared in-memory engine and schema
    fixture_root = (
        Path(__file__).parents[1] / "fixtures" / "research_kernel" / "600406"
    )
    bundle = CanonicalBundle(
        pool=json.loads((fixture_root / "pool.json").read_text(encoding="utf-8")),
        market=json.loads((fixture_root / "market.json").read_text(encoding="utf-8")),
        financial=json.loads(
            (fixture_root / "financial.json").read_text(encoding="utf-8")
        ),
        raw={},
    )

    async def fake_fetch(*_args, **_kwargs) -> CanonicalBundle:
        return bundle

    async def fail_explanation(**_kwargs) -> ResearchExplanation:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(ifind_chain, "fetch_canonical_bundle", fake_fetch)
    monkeypatch.setattr(ifind_chain, "_explain_score", fail_explanation)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier2_api_key="test",
        llm_tier3_api_key="test",
    )

    with pytest.raises(ResearchPartialError) as exc_info:
        await run_ifind_research_chain(
            settings=settings,
            proxy_url="http://unused.invalid",
            proxy_api_key="unused",
            ticker="600406.SH",
            industry="电力设备",
            report_year=2025,
            as_of=date(2026, 10, 5),
            output_root=tmp_path / "research-runs",
        )

    run_id = exc_info.value.run_id
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
        snapshot = await ResearchSnapshotRepository(session).by_run(run_id)

    assert run is not None
    assert run.status == "partial"
    assert run.stage == "partial"
    assert run.metadata_json["partial_reason"] == "explanation_failed"
    assert "model unavailable" in (run.error_summary or "")
    assert snapshot is not None
    assert snapshot.score_json["business_score"] == 55.22
    assert snapshot.explanation_json is None
    assert "agent_report" not in snapshot.artifact_manifest
    assert str(snapshot.artifact_manifest["scores"]).endswith("/scores.json")

"""Evidence navigation tests (work package D / MVP-403).

Agent claims must cite audited deterministic fields: every persisted
explanation citation resolves against the stored score item, and the browser
client renders citations as clickable evidence links with a fail-loud
"引用失效" path for anything unresolved.
"""

import asyncio
import pathlib
from datetime import date

from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)


def _headers():
    return {"X-API-Key": "secret"}


def _path_exists(payload: dict[str, object], path: str) -> bool:
    node: object = payload
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return False
    return True


def _seed_run_with_explanation(api_client) -> int:
    async def seed() -> int:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(
                kind="ifind_research",
                request_json={"ticker": "600406.SH", "industry": "电力设备"},
            )
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
                score_json={
                    "items": [
                        {
                            "strategy_label": "稳健质量成长",
                            "investment_score": 66.0,
                            "business_score": 64.0,
                            "needs_agent_review": True,
                            "factor_scores": {"profitability_quality": 88.0},
                            "key_metrics": {"roe": 0.16, "pe": 19.17},
                            "missing_fields": ["dividend_yield"],
                            "field_sources": {
                                "roe": {
                                    "field": "roe",
                                    "provider": "ifind",
                                    "source": "/financial/index",
                                    "quality": "direct",
                                    "updated_at": "2026-10-05T00:00:00Z",
                                    "note": "FY2025 profitability",
                                }
                            },
                        }
                    ],
                    "needs_agent_review": True,
                },
                explanation_json={
                    "summary": "盈利改善。",
                    "classification_rationale": [
                        {"text": "ROE 稳定", "supporting_fields": ["key_metrics.roe"]}
                    ],
                    "positive_evidence": [
                        {"text": "因子分高", "supporting_fields": ["factor_scores.profitability_quality"]}
                    ],
                    "negative_evidence": [],
                    "risk_factors": [
                        {"text": "股息率缺失", "supporting_fields": ["missing_fields"]}
                    ],
                    "data_quality_notes": [],
                    "open_questions": [],
                    "workflow_conclusion": "needs_review：股息率缺失",
                },
                artifact_manifest={},
            )
            await PipelineRunRepository(session).mark_success(run.id)
            return run.id

    return asyncio.run(seed())


def test_persisted_explanation_citations_all_resolve(api_client):
    run_id = _seed_run_with_explanation(api_client)
    detail = api_client.get(f"/research/runs/{run_id}", headers=_headers()).json()

    score_item = detail["result"]["score"]["items"][0]
    explanation = detail["result"]["explanation"]
    assert explanation is not None

    cited_paths = [
        field
        for group in (
            explanation["classification_rationale"],
            explanation["positive_evidence"],
            explanation["negative_evidence"],
            explanation["risk_factors"],
            explanation["data_quality_notes"],
        )
        for claim in group
        for field in claim["supporting_fields"]
    ]
    assert cited_paths, "fixture explanation must cite at least one field"
    unresolved = [path for path in cited_paths if not _path_exists(score_item, path)]
    assert unresolved == [], f"citations must resolve against the score item: {unresolved}"

    # Provenance for a cited metric must be available for the evidence panel.
    assert "roe" in score_item["field_sources"]
    provenance = score_item["field_sources"]["roe"]
    for key in ("provider", "source", "quality", "updated_at"):
        assert key in provenance


def test_invalid_citation_is_rejected_before_persistence():
    """The production explanation path refuses unknown fields (no silent drop)."""

    from investment_agent.research.ifind_chain import (
        EvidenceClaim,
        ResearchExplanation,
        validate_explanation_policy,
    )

    score_item = {"key_metrics": {"roe": 0.16}, "needs_agent_review": False}
    explanation = ResearchExplanation(
        summary="s",
        classification_rationale=[
            EvidenceClaim(text="c", supporting_fields=["key_metrics.not_there"])
        ],
        positive_evidence=[],
        negative_evidence=[],
        risk_factors=[],
        data_quality_notes=[],
        open_questions=[],
        workflow_conclusion="常规",
    )
    try:
        validate_explanation_policy(explanation, score_item)
    except ValueError as exc:
        assert "key_metrics.not_there" in str(exc)
    else:
        raise AssertionError("invalid citation must be rejected loudly")


def test_client_contract_renders_evidence_navigation():
    source = pathlib.Path(
        "src/investment_agent/web/static/app.js"
    ).read_text(encoding="utf-8")
    assert "resolveEvidence" in source
    assert "引用失效" in source, "unresolved citations must be shown, never hidden"
    assert "data-field-path" in source
    assert "data-evidence-section" in source
    assert "evidence-inspector" in source
    for section in ("evidence-score", "evidence-factors", "evidence-metrics", "evidence-quality"):
        assert section in source, section
    for provenance_key in ("provider", "quality", "updated_at", "note"):
        assert provenance_key in source, provenance_key

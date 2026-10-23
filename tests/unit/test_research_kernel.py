"""Kernel input validation tests.

The golden parity coverage lives in ``test_golden_corpus_replay.py`` and is
driven by ``tests/fixtures/research_kernel/manifest.yaml``.
"""

import json
from pathlib import Path

from investment_agent.research.contracts import FinancialDataItem, MarketDataItem
from investment_agent.research.ifind_chain import (
    EvidenceClaim,
    ResearchExplanation,
    _explanation_field_paths,
    validate_explanation_policy,
)
from investment_agent.research.kernel import InProcessResearchKernel
from investment_agent.research_kernel.scoring import score_stock


def test_in_process_kernel_rejects_non_object_input(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")

    try:
        InProcessResearchKernel().score_and_render(
            pool=invalid,
            market=invalid,
            financial=invalid,
            artifact_dir=tmp_path,
        )
    except ValueError as exc:
        assert "must be a JSON object" in str(exc)
    else:
        raise AssertionError("non-object input should be rejected")


def test_in_process_kernel_writes_all_artifacts(tmp_path: Path):
    fixture = Path(__file__).parents[1] / "fixtures" / "research_kernel" / "600406"
    artifacts = InProcessResearchKernel().score_and_render(
        pool=fixture / "pool.json",
        market=fixture / "market.json",
        financial=fixture / "financial.json",
        artifact_dir=tmp_path,
        as_of="2026-10-05T09:46:20.080336+00:00",
    )

    assert artifacts.scores_json.is_file()
    assert artifacts.scores_csv.is_file()
    assert artifacts.report_context_json.is_file()
    assert artifacts.report_markdown.is_file()

    score = json.loads(artifacts.scores_json.read_text())["items"][0]
    context = json.loads(artifacts.report_context_json.read_text())["items"][0]
    market = json.loads((fixture / "market.json").read_text())["items"][0]
    financial = json.loads((fixture / "financial.json").read_text())["items"][0]
    pool = json.loads((fixture / "pool.json").read_text())["items"][0]
    metrics = score["key_metrics"]
    # A canonical metric addition must also reach the explanation context.
    assert set(metrics) == set(MarketDataItem.provenance_fields) | set(
        FinancialDataItem.provenance_fields
    )
    assert context["key_metrics"] == metrics
    assert score["schema_version"] == context["schema_version"] == "v2.2"
    for key in MarketDataItem.provenance_fields:
        assert metrics[key] == (pool["market_cap"] if key == "market_cap" else market[key])
    for key in FinancialDataItem.provenance_fields:
        assert metrics[key] == financial[key]

    paths = _explanation_field_paths(score)
    evidence = []
    quality = []
    for key, value in metrics.items():
        path = f"key_metrics.{key}"
        assert path in paths
        claim = EvidenceClaim(text=key, supporting_fields=[path])
        (quality if value is None else evidence).append(claim)
    validate_explanation_policy(
        ResearchExplanation(
            summary="完整指标可引用",
            classification_rationale=[],
            positive_evidence=evidence,
            negative_evidence=[],
            risk_factors=[],
            data_quality_notes=quality,
            open_questions=[],
            workflow_conclusion="needs_review",
        ),
        score,
    )


def test_explanation_metrics_preserve_missing_zero_and_negative_values():
    fixture = Path(__file__).parents[1] / "fixtures" / "research_kernel" / "600406"
    pool = json.loads((fixture / "pool.json").read_text())["items"][0]
    market = json.loads((fixture / "market.json").read_text())["items"][0]
    financial = json.loads((fixture / "financial.json").read_text())["items"][0]
    market["volatility_1y"] = 0
    financial.update(operating_cash_flow=-123.0, free_cash_flow=0, roe_stability=None)

    metrics = score_stock(pool, market, financial)["key_metrics"]

    assert metrics["volatility_1y"] == 0
    assert metrics["operating_cash_flow"] == -123.0
    assert metrics["free_cash_flow"] == 0
    assert metrics["roe_stability"] is None

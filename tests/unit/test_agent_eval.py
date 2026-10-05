"""Agent explanation evals (work package A4).

The explanation agent may only cite canonical score fields that exist, must
preserve the ``needs_review`` workflow label whenever the deterministic kernel
flagged ``needs_agent_review``, and structurally cannot emit trading
instructions. These evals run against every sanitized corpus sample so a
kernel change that breaks the explanation contract fails immediately.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from investment_agent.research.ifind_chain import (
    EXPLANATION_SYSTEM_PROMPT_TEMPLATE,
    EvidenceClaim,
    ResearchExplanation,
    validate_explanation_policy,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.yaml"

ALLOWED_EXPLANATION_FIELDS = {
    "summary",
    "classification_rationale",
    "positive_evidence",
    "negative_evidence",
    "risk_factors",
    "data_quality_notes",
    "open_questions",
    "workflow_conclusion",
}
TRADING_INSTRUCTION_MARKS = ("买入", "卖出", "仓位", "收益承诺", "下单", "调仓")


def _manifest() -> dict[str, Any]:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def _sanitized_samples() -> list[dict[str, Any]]:
    return [s for s in _manifest()["samples"] if s["fixture_status"] == "sanitized"]


def _score_item(sample: dict[str, Any]) -> dict[str, Any]:
    fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
    scores_name = "legacy_scores.json" if (fixture_dir / "legacy_scores.json").is_file() else "golden_scores.json"
    scores = json.loads((fixture_dir / scores_name).read_text(encoding="utf-8"))
    item = scores["items"][0]
    assert isinstance(item, dict)
    return item


def _walk_paths(node: Any, prefix: str = "") -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path
            yield from _walk_paths(value, path)


def _explanation(supporting_fields: list[str], *, needs_review: bool) -> ResearchExplanation:
    return ResearchExplanation(
        summary="确定性结果的结构化复述，仅引用已存在字段。",
        classification_rationale=[
            EvidenceClaim(text="策略分类由确定性内核给出", supporting_fields=list(supporting_fields))
        ],
        positive_evidence=[],
        negative_evidence=[],
        risk_factors=[],
        data_quality_notes=[],
        open_questions=[],
        workflow_conclusion=(
            "needs_review：存在缺失字段，需人工复核" if needs_review else "常规研究推进"
        ),
    )


@pytest.mark.parametrize("sample", _sanitized_samples(), ids=lambda sample: sample["ticker"])
def test_all_citations_resolve_against_deterministic_score(sample: dict[str, Any]):
    score_item = _score_item(sample)
    paths = sorted(set(_walk_paths(score_item)))
    assert paths, f"{sample['ticker']} score item exposes no citable paths"

    needs_review = bool(score_item.get("needs_agent_review"))
    explanation = _explanation(paths, needs_review=needs_review)
    validate_explanation_policy(explanation, score_item)


@pytest.mark.parametrize("sample", _sanitized_samples(), ids=lambda sample: sample["ticker"])
def test_unknown_citation_is_rejected_for_every_sample(sample: dict[str, Any]):
    score_item = _score_item(sample)
    explanation = _explanation(
        ["investment_score", "key_metrics.not_a_real_field"],
        needs_review=True,
    )

    with pytest.raises(ValueError, match="key_metrics.not_a_real_field"):
        validate_explanation_policy(explanation, score_item)


@pytest.mark.parametrize("sample", _sanitized_samples(), ids=lambda sample: sample["ticker"])
def test_needs_agent_review_forces_needs_review_label(sample: dict[str, Any]):
    score_item = _score_item(sample)
    assert score_item.get("needs_agent_review") is True, (
        "every current corpus sample misses dividend_yield; update this eval "
        "if the kernel or gateway stops flagging samples for review"
    )

    compliant = _explanation(["investment_score"], needs_review=True)
    validate_explanation_policy(compliant, score_item)

    non_compliant = _explanation(["investment_score"], needs_review=False)
    with pytest.raises(ValueError, match="needs_review workflow label"):
        validate_explanation_policy(non_compliant, score_item)


def test_explanation_schema_has_no_trading_surface():
    schema = ResearchExplanation.model_json_schema()
    properties = set(schema.get("properties", {}))
    assert properties == ALLOWED_EXPLANATION_FIELDS, (
        "explanation schema drifted; any new field must keep the agent "
        "explanation-only (no orders, positions or return promises)"
    )
    for mark in TRADING_INSTRUCTION_MARKS:
        assert all(mark not in field for field in properties), mark


def test_system_prompt_forbids_facts_and_trading_instructions():
    assert "不能补充外部事实" in EXPLANATION_SYSTEM_PROMPT_TEMPLATE
    assert "不能给出买入、卖出、仓位或收益承诺" in EXPLANATION_SYSTEM_PROMPT_TEMPLATE
    assert "supporting_fields" in EXPLANATION_SYSTEM_PROMPT_TEMPLATE
    assert "needs_review" in EXPLANATION_SYSTEM_PROMPT_TEMPLATE


def test_claims_must_cite_at_least_one_supporting_field():
    with pytest.raises(ValidationError):
        EvidenceClaim(text="没有证据的断言", supporting_fields=[])


def test_explanations_preserve_citation_evidence_in_agent_report():
    """The rendered report must keep the audited deterministic context header."""

    from investment_agent.research.ifind_chain import render_agent_report

    score_item = _score_item(_sanitized_samples()[0])
    explanation = _explanation(["investment_score"], needs_review=True)
    report = render_agent_report(score_item=score_item, explanation=explanation)
    assert "非投资建议" in report

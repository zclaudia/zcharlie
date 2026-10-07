"""P1-C5: rule draft output contract - parameter, citation and wording checks."""

from __future__ import annotations

import json

import pytest

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.registry import FactorValue
from investment_agent.rules.draft_policy import (
    RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE,
    ParamRationale,
    RuleDraftOutput,
    check_draft,
    instance_params,
    system_prompt,
    trading_terms,
    validate_draft,
)
from investment_agent.rules.schema import RuleTemplate
from investment_agent.rules.templates import load_templates


@pytest.fixture(scope="module")
def templates() -> dict[str, RuleTemplate]:
    return {template.id: template for _, template in load_templates()}


def _factors(**values: float | None) -> dict[str, FactorValue]:
    result: dict[str, FactorValue] = {}
    for factor in DEFAULT_REGISTRY:
        value = values.get(factor.name)
        result[factor.name] = FactorValue(
            factor.name, factor.version, value, "ok" if value is not None else "missing"
        )
    return result


FACTORS = _factors(roe=0.3, roe_stability=0.9, pe_percentile_5y=0.2, gross_margin=0.9)


def _output(**overrides: object) -> RuleDraftOutput:
    data: dict[str, object] = {
        "params": {"buy_pe_pct_max": 0.2},
        "rationale": [
            {
                "param": "buy_pe_pct_max",
                "choice": "adjusted",
                "reason": "估值分位处于自身 5 年区间低位，收紧 buy_zone 以突出研究优先级。",
                "cited_factors": ["pe_percentile_5y"],
            },
            {"param": "roe_min", "choice": "default", "reason": "ROE 稳定，沿用默认。"},
        ],
        "overall_rationale": "沿用模板条件，仅收紧 buy_zone 的估值分位参数。",
        "watch_points": ["free_cash_flow 缺失，需要复核现金流质量。"],
    }
    data.update(overrides)
    return RuleDraftOutput.model_validate(data)


def _codes(output: RuleDraftOutput, template: RuleTemplate) -> set[str]:
    return {issue.code for issue in check_draft(output, template=template, factors=FACTORS)}


def test_valid_output_builds_a_valid_instance(templates) -> None:
    template = templates["quality_growth"]
    instance, issues = validate_draft(
        _output(),
        template=template,
        factors=FACTORS,
        symbol="600519.SH",
        drafted_by="llm:stub",
        extra={"candidate_id": 7},
    )
    assert issues == []
    assert instance is not None
    assert instance.template_ref == template.ref
    assert instance.scope == "candidate" and instance.zones == {}
    assert instance.params == {"buy_pe_pct_max": 0.2}
    rationale = json.loads(instance.draft_rationale)
    assert rationale["calibration_status"] == "uncalibrated"
    assert rationale["candidate_id"] == 7
    assert rationale["params"]["buy_pe_pct_max"]["default"] == 0.3
    assert rationale["params"]["roe_min"]["choice"] == "default"
    assert rationale["watch_points"]


def test_values_equal_to_default_are_not_overrides(templates) -> None:
    template = templates["quality_growth"]
    output = _output(params={"buy_pe_pct_max": 0.2, "roe_min": template.params["roe_min"]})
    assert instance_params(output, template) == {"buy_pe_pct_max": 0.2}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"params": {"buy_pe_pct_max": 0.9}}, "param_out_of_range"),
        ({"params": {"buy_pe_pct_max": float("nan")}}, "param_not_finite"),
        ({"params": {"buy_pe_pct_max": 0.2, "made_up": 1.0}}, "unknown_param"),
        ({"params": {"buy_pe_pct_max": 0.2, "drop_roe_max": 0.12}}, "rationale_missing"),
    ],
)
def test_param_issues(templates, overrides, code) -> None:
    assert code in _codes(_output(**overrides), templates["quality_growth"])


def test_between_order_is_checked(templates) -> None:
    template = templates["quality_growth"]  # roe between [$roe_min, $roe_max]
    output = _output(
        params={"buy_pe_pct_max": 0.2, "roe_max": 0.40, "roe_min": 0.25},
        rationale=[
            ParamRationale(param=name, choice="adjusted", reason="测试", cited_factors=["roe"])
            for name in ("buy_pe_pct_max", "roe_max", "roe_min")
        ],
    )
    assert "param_order" not in _codes(output, template)  # 0.25 <= 0.40
    # The shipped ranges cannot cross (roe_min <= 0.25 < 0.40 <= roe_max), so
    # check the order through a template whose ranges overlap.
    ranges = dict(template.param_ranges)
    ranges["roe_min"] = ranges["roe_min"].model_copy(update={"max": 0.6})
    overlapping = template.model_copy(update={"param_ranges": ranges})
    flipped = output.model_copy(update={"params": {**output.params, "roe_min": 0.5, "roe_max": 0.45}})
    assert "param_order" in _codes(flipped, overlapping)


def test_rationale_consistency(templates) -> None:
    template = templates["quality_growth"]
    mismatch = _output(
        rationale=[
            {"param": "buy_pe_pct_max", "choice": "default", "reason": "x"},
            {"param": "roe_min", "choice": "adjusted", "reason": "x", "cited_factors": ["roe"]},
            {"param": "roe_min", "choice": "default", "reason": "x"},
        ]
    )
    codes = _codes(mismatch, template)
    assert {"rationale_mismatch", "rationale_duplicate"} <= codes
    uncited = _output(
        rationale=[{"param": "buy_pe_pct_max", "choice": "adjusted", "reason": "x"}]
    )
    assert "citation_missing" in _codes(uncited, template)


def test_citations_must_be_present_and_non_null(templates) -> None:
    template = templates["quality_growth"]
    unknown = _output(
        rationale=[
            {
                "param": "buy_pe_pct_max",
                "choice": "adjusted",
                "reason": "x",
                "cited_factors": ["pe_percentile_10y"],
            }
        ]
    )
    null = _output(
        rationale=[
            {
                "param": "buy_pe_pct_max",
                "choice": "adjusted",
                "reason": "x",
                "cited_factors": ["free_cash_flow"],
            }
        ]
    )
    assert "citation_unknown" in _codes(unknown, template)
    assert "citation_null" in _codes(null, template)


@pytest.mark.parametrize(
    "text",
    [
        "估值低位，建议买入",
        "跌破后考虑减仓",
        "目标价 2000 元",
        "设置止损线",
        "仓位建议 5%",
        "Consider to buy on dips",
        "a stop-loss at 10%",
    ],
)
def test_trading_language_is_rejected(templates, text) -> None:
    template = templates["quality_growth"]
    for output in (
        _output(overall_rationale=text),
        _output(watch_points=[text]),
        _output(
            rationale=[
                {
                    "param": "buy_pe_pct_max",
                    "choice": "adjusted",
                    "reason": text,
                    "cited_factors": ["pe_percentile_5y"],
                }
            ]
        ),
    ):
        assert "trading_language" in _codes(output, template)


def test_zone_identifiers_are_not_trading_language() -> None:
    text = "pe_percentile_5y 回落时进入 buy_zone；add / trim / exit_review 由持仓区间定义，drop 表示不再候选。"
    assert trading_terms(text) == []
    assert trading_terms("buy and sell") == ["buy", "sell"]


def test_rule_validation_runs_on_the_built_instance(templates) -> None:
    template = templates["quality_growth"]
    instance, issues = validate_draft(
        _output(),
        template=template,
        factors=FACTORS,
        symbol="not-a-ticker",
        drafted_by="llm:stub",
    )
    assert instance is None
    assert [issue.code for issue in issues] == ["rule_invalid_symbol"]


def test_output_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValueError):
        RuleDraftOutput.model_validate(
            {"params": {}, "overall_rationale": "x", "zones": {"sell": {}}}
        )


def test_prompt_states_the_contract() -> None:
    prompt = system_prompt()
    for marker in (
        "不是投资建议",
        "uncalibrated",
        "param_ranges",
        "cited_factors",
        "buy_zone",
        "exit_review",
        "preference_notes",
        '"overall_rationale"',
    ):
        assert marker in prompt
    assert "{output_schema}" in RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE

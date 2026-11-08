"""P1-C5: rule draft output contract - parameter, citation and wording checks."""

from __future__ import annotations

import json
from datetime import date

import pytest

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.registry import FactorValue
from investment_agent.rules.draft_policy import (
    MIN_CALIBRATION_SAMPLE,
    RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE,
    CalibrationEvidence,
    ParamRationale,
    RuleDraftOutput,
    calibration_allows_overrides,
    check_draft,
    instance_params,
    normalize_uncalibrated_output,
    output_json_schema,
    override_policy,
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


def _default_output(**overrides: object) -> RuleDraftOutput:
    """A policy-compliant draft: explain the defaults, never move a number."""

    data: dict[str, object] = {
        "params": {},
        "rationale": [
            {
                "param": "buy_pe_pct_max",
                "choice": "default",
                "reason": "估值分位处于自身 5 年区间，默认门槛保留；等待样本外校准。",
                "cited_factors": ["pe_percentile_5y"],
            },
            {
                "param": "roe_min",
                "choice": "default",
                "reason": "ROE 稳定，沿用默认；free_cash_flow 缺失列入复核。",
            },
        ],
        "overall_rationale": "沿用模板全部默认参数，仅解释适用性与数据缺口。",
        "watch_points": ["free_cash_flow 缺失，需要复核现金流质量。"],
    }
    data.update(overrides)
    return RuleDraftOutput.model_validate(data)


def _codes(output: RuleDraftOutput, template: RuleTemplate) -> set[str]:
    return {issue.code for issue in check_draft(output, template=template, factors=FACTORS)}


def _evidence(template: RuleTemplate, **overrides: object) -> CalibrationEvidence:
    data: dict[str, object] = {
        "template_ref": template.ref,
        "method": "walk_forward",
        "sample_periods": MIN_CALIBRATION_SAMPLE,
        "period_start": date(2018, 1, 1),
        "period_end": date(2025, 12, 31),
        "metrics": {"hit_rate": 0.58, "ic": 0.06},
        "report_ref": "research/oos/quality_growth@1.md",
    }
    data.update(overrides)
    return CalibrationEvidence(**data)  # type: ignore[arg-type]


def test_default_only_output_still_drafts(templates) -> None:
    """Conservative default: explaining the template defaults remains allowed."""

    template = templates["quality_growth"]
    instance, issues = validate_draft(
        _default_output(),
        template=template,
        factors=FACTORS,
        symbol="600519.SH",
        drafted_by="llm:stub",
        extra={"candidate_id": 7},
    )
    assert issues == []
    assert instance is not None
    assert instance.params == {}
    rationale = json.loads(instance.draft_rationale)
    assert rationale["schema"] == "rule_draft.v2"
    assert rationale["override_policy"] == "defaults_only"
    assert "suppressed_overrides" not in rationale
    assert rationale["calibration_status"] == "uncalibrated"
    assert rationale["candidate_id"] == 7
    assert rationale["params"]["buy_pe_pct_max"]["choice"] == "default"
    assert rationale["params"]["buy_pe_pct_max"]["value"] == template.params["buy_pe_pct_max"]
    assert rationale["watch_points"]


def test_uncalibrated_numeric_override_is_normalized(templates) -> None:
    """A single-stock numeric proposal is dropped and audited, never stored."""

    template = templates["quality_growth"]
    instance, issues = validate_draft(
        _output(),
        template=template,
        factors=FACTORS,
        symbol="600519.SH",
        drafted_by="llm:stub",
    )
    assert issues == []
    assert instance is not None
    assert instance.params == {}
    rationale = json.loads(instance.draft_rationale)
    assert rationale["override_policy"] == "defaults_only"
    assert rationale["params"]["buy_pe_pct_max"]["choice"] == "default"
    assert rationale["params"]["buy_pe_pct_max"]["value"] == 0.3
    suppressed = rationale["suppressed_overrides"]
    assert suppressed == [
        {
            "param": "buy_pe_pct_max",
            "proposed": 0.2,
            "default": 0.3,
            "reason": "估值分位处于自身 5 年区间低位，收紧 buy_zone 以突出研究优先级。",
            "cited_factors": ["pe_percentile_5y"],
        }
    ]
    assert "数值覆盖未应用" in rationale["overall_rationale"]


def test_normalize_drops_override_and_keeps_explanations(templates) -> None:
    template = templates["quality_growth"]
    normalized, suppressed = normalize_uncalibrated_output(_output(), template)
    assert normalized.params == {}
    assert [entry.choice for entry in normalized.rationale] == ["default", "default"]
    assert suppressed[0]["proposed"] == 0.2
    assert normalized.watch_points == _output().watch_points


def test_values_equal_to_default_are_not_overrides(templates) -> None:
    template = templates["quality_growth"]
    output = _output(params={"buy_pe_pct_max": 0.2, "roe_min": template.params["roe_min"]})
    assert instance_params(output, template) == {"buy_pe_pct_max": 0.2}
    # Equality with the default is not a change, so nothing is suppressed.
    equal = _output(
        params={"buy_pe_pct_max": template.params["buy_pe_pct_max"]},
        rationale=[
            {
                "param": "buy_pe_pct_max",
                "choice": "default",
                "reason": "默认值。",
                "cited_factors": ["pe_percentile_5y"],
            }
        ],
    )
    normalized, suppressed = normalize_uncalibrated_output(equal, template)
    assert normalized.params == equal.params and suppressed == []


def test_valid_calibration_evidence_allows_overrides(templates) -> None:
    template = templates["quality_growth"].model_copy(update={"calibration_status": "backtested"})
    evidence = _evidence(template)
    assert calibration_allows_overrides(evidence, template)
    assert override_policy(template, evidence) == "calibrated_overrides"
    instance, issues = validate_draft(
        _output(),
        template=template,
        factors=FACTORS,
        symbol="600519.SH",
        drafted_by="llm:stub",
        evidence=evidence,
    )
    assert issues == []
    assert instance is not None and instance.params == {"buy_pe_pct_max": 0.2}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"template_ref": "quality_growth@9"},
        {"method": "single_day_cross_section"},
        {"sample_periods": MIN_CALIBRATION_SAMPLE - 1},
        {"period_start": date(2026, 1, 1), "period_end": date(2025, 12, 31)},
        {"metrics": {}},
        {"report_ref": "  "},
        {"sample_periods": 0},
    ],
)
def test_malformed_evidence_fails_closed(templates, kwargs) -> None:
    template = templates["quality_growth"].model_copy(update={"calibration_status": "backtested"})
    evidence = _evidence(template, **kwargs)
    assert not calibration_allows_overrides(evidence, template)
    assert override_policy(template, evidence) == "defaults_only"
    instance, issues = validate_draft(
        _output(),
        template=template,
        factors=FACTORS,
        symbol="600519.SH",
        drafted_by="llm:stub",
        evidence=evidence,
    )
    assert issues == [] and instance is not None and instance.params == {}


def test_no_evidence_and_uncalibrated_status_fail_closed(templates) -> None:
    template = templates["quality_growth"]
    assert override_policy(template, None) == "defaults_only"
    assert override_policy(template.model_copy(update={"calibration_status": "backtested"}), None) == (
        "defaults_only"
    )
    # A label alone is not evidence, even for a "validated" template.
    assert override_policy(
        template.model_copy(update={"calibration_status": "validated"}), None
    ) == "defaults_only"


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
        "不改变任何数值参数",
        "不得据此收紧或放松任何阈值",
    ):
        assert marker in prompt
    assert "{output_schema}" in RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE
    # The schema itself tells the model to leave params empty.
    assert "必须为空" in prompt


def test_defaults_only_prompt_forbids_overrides() -> None:
    """The default prompt (and an unknown policy) is the conservative one."""

    default = system_prompt()
    explicit = system_prompt("defaults_only")
    unknown = system_prompt("something_else")
    for prompt in (default, explicit, unknown):
        assert "params 必须为空" in prompt
        assert "params 留空" in prompt
        assert "choice=adjusted" not in prompt
        assert "参数覆盖在允许范围内" not in prompt
        assert "preference_notes" in prompt  # background only, never calibration
        assert "不是校准证据" in prompt
    schema = json.loads(output_json_schema())
    assert "必须为空" in schema["properties"]["params"]["description"]


def test_calibrated_prompt_restores_the_override_contract() -> None:
    """With accepted evidence the prompt/schema must not still forbid overrides."""

    prompt = system_prompt("calibrated_overrides")
    assert "params 必须为空" not in prompt
    assert "params 留空" not in prompt
    for marker in (
        "样本外校准证据",
        "param_ranges",
        "[min, max]",
        "choice=adjusted",
        "cited_factors",
        "choice=default",
    ):
        assert marker in prompt
    # preference_notes stay non-authoritative on both paths.
    assert "不得仅凭 preference_notes 收紧或放松任何阈值" in prompt
    schema = json.loads(output_json_schema("calibrated_overrides"))
    assert "必须为空" not in schema["properties"]["params"]["description"]
    assert "param_ranges" in schema["properties"]["params"]["description"]

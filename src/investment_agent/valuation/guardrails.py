"""Quality checks around reverse DCF. They do not change the solved growth.

This repository has no treasury yield or equity-risk-premium series. The rate
band is ``risk_free_rate + equity_risk_premium``, stored as round placeholders
and marked ``uncalibrated``. Those numbers are not researched A-share parameters.

A failed check is written onto the calculation (quality, warnings, and
``details["guardrails"]``). It does not replace the solver and does not drop a
result the solver already accepted. ``g >= r`` stays the solver's
``invalid_parameters`` rejection. Terminal value above the share threshold is
only the 「低信息量」 mark.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from investment_agent.investor.common import Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.valuation.solver import solve

GUARDRAIL_VERSION = "valuation_guardrails.uncalibrated.v1"
UNCALIBRATED_NOTE = (
    "未校准的起始默认值。仓库没有国债收益率或股权风险溢价数据；"
    "折现率区间是无风险利率加股权风险溢价的占位锚，不是已研究的 A 股参数。"
)
READING_NOTE = (
    "反向 DCF 不计算高估、低估或安全边际。"
    "这里的投资结论是隐含 FCFE 增速的符号，检验只报告该符号在折现率浮动后是否改变。"
)
LOW_INFORMATION_MARK = "低信息量"

RATE_OUTSIDE_ANCHOR = "discount_rate_outside_anchor"
SPREAD_BELOW_FLOOR = "discount_spread_below_floor"
LOW_INFORMATION = "low_information_terminal_value"
FLIP_CHANGES = "rate_flip_conclusion_changes"
FLIP_NOT_COMPARABLE = "rate_flip_not_comparable"

# Same width the solver treats as a converged interval, so a last-digit zero
# is not reported as a change of sign.
IMPLIED_GROWTH_FLAT = Decimal("1e-8")

Outcome = Literal["pass", "fail", "not_checked"]
TerminalOutcome = Literal["low_information", "within_threshold", "not_checked"]
Reading = Literal["positive_implied_growth", "negative_implied_growth", "flat_implied_growth"]
Conclusion = Reading | Literal["unsolved"]

CONCLUSION_LABELS: dict[str, str] = {
    "positive_implied_growth": "隐含增速为正",
    "negative_implied_growth": "隐含增速为负",
    "flat_implied_growth": "隐含增速约为零",
    "unsolved": "无法求解",
}


@dataclass(frozen=True)
class GuardrailPolicy:
    """Single place for the starting thresholds. Callers can replace it.

    ``calibration`` stays ``uncalibrated`` until a cited risk-free series
    exists. Do not relabel these placeholders as researched market parameters.
    """

    calibration: Literal["uncalibrated"] = "uncalibrated"
    risk_free_rate: Decimal = Decimal("0.02")
    equity_risk_premium_low: Decimal = Decimal("0.03")
    equity_risk_premium_high: Decimal = Decimal("0.12")
    min_discount_spread: Decimal = Decimal("0.05")
    terminal_share_low_information: Decimal = Decimal("0.70")
    rate_flip_shift: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        if self.calibration != "uncalibrated":
            raise ValueError("placeholder anchor must stay marked uncalibrated")
        numbers = (
            self.risk_free_rate,
            self.equity_risk_premium_low,
            self.equity_risk_premium_high,
            self.min_discount_spread,
            self.terminal_share_low_information,
            self.rate_flip_shift,
        )
        if not all(number.is_finite() for number in numbers):
            raise ValueError("guardrail thresholds must be finite")
        if (
            self.equity_risk_premium_low < 0
            or self.equity_risk_premium_high < self.equity_risk_premium_low
        ):
            raise ValueError("equity risk premium band is empty")
        if self.min_discount_spread <= 0 or self.rate_flip_shift <= 0:
            raise ValueError("spread floor and rate shift must be positive")
        if not 0 < self.terminal_share_low_information <= 1:
            raise ValueError("terminal share threshold must be inside (0, 1]")

    @property
    def rate_floor(self) -> Decimal:
        return self.risk_free_rate + self.equity_risk_premium_low

    @property
    def rate_ceiling(self) -> Decimal:
        return self.risk_free_rate + self.equity_risk_premium_high


DEFAULT_GUARDRAILS = GuardrailPolicy()


@dataclass(frozen=True)
class GuardrailAssessment:
    quality: tuple[str, ...]
    warnings: tuple[str, ...]
    details: dict[str, Any]
    policy: dict[str, Any]
    scenario_marks: tuple[dict[str, Any], ...]
    result_fields: dict[str, Quantity]


def rate_anchor_outcome(
    rate: Decimal, policy: GuardrailPolicy = DEFAULT_GUARDRAILS
) -> Literal["pass", "fail"]:
    """Inclusive band: risk-free rate plus the equity-premium placeholders."""
    if policy.rate_floor <= rate <= policy.rate_ceiling:
        return "pass"
    return "fail"


def spread_outcome(
    rate: Decimal, terminal_growth: Decimal, policy: GuardrailPolicy = DEFAULT_GUARDRAILS
) -> Literal["pass", "fail"]:
    """``r - g`` must be at least the configured floor. Equality passes."""
    if rate - terminal_growth >= policy.min_discount_spread:
        return "pass"
    return "fail"


def terminal_share_outcome(
    fraction: Decimal, policy: GuardrailPolicy = DEFAULT_GUARDRAILS
) -> Literal["low_information", "within_threshold"]:
    """Strictly above the threshold. Equality is not low information."""
    if fraction > policy.terminal_share_low_information:
        return "low_information"
    return "within_threshold"


def implied_growth_reading(growth: Decimal) -> Reading:
    if growth > IMPLIED_GROWTH_FLAT:
        return "positive_implied_growth"
    if growth < -IMPLIED_GROWTH_FLAT:
        return "negative_implied_growth"
    return "flat_implied_growth"


def flip_discount_rate(
    *,
    market_value: Decimal,
    base: Decimal,
    rate: Decimal,
    terminal_growth: Decimal,
    published_growth: Decimal,
    policy: GuardrailPolicy = DEFAULT_GUARDRAILS,
) -> dict[str, Any]:
    """Re-solve at ``r ± shift`` and compare the sign of implied growth.

    The published main growth is the valuation's own result. The perturbed
    rates use the same solver and are not written back into that result.
    """
    shift = policy.rate_flip_shift
    main = _published_side(rate, published_growth)
    lower = _solved_side(market_value, base, rate - shift, terminal_growth)
    upper = _solved_side(market_value, base, rate + shift, terminal_growth)
    comparable = lower["conclusion"] != "unsolved" and upper["conclusion"] != "unsolved"
    changes: bool | None
    if not comparable:
        changes = None
    else:
        changes = (
            lower["conclusion"] != main["conclusion"] or upper["conclusion"] != main["conclusion"]
        )
    return {
        "calibration": policy.calibration,
        "shift": str(shift),
        "basis": "median",
        "reading": "implied_growth_sign",
        "reading_note": READING_NOTE,
        "attempted": True,
        "main": main,
        "lower": lower,
        "upper": upper,
        "conclusion_changes": changes,
    }


def assess_reverse_dcf_guardrails(
    *,
    main_rate: Decimal | None,
    terminal_growth: Decimal | None,
    sensitivity_rates: tuple[Decimal | None, ...],
    scenarios: list[dict[str, Any]],
    market_value: Decimal | None,
    median_base: Decimal | None,
    policy: GuardrailPolicy = DEFAULT_GUARDRAILS,
) -> GuardrailAssessment:
    """Explainable checks for one reverse-DCF calculation. Scenarios are not mutated."""
    rate_anchor = _rate_anchor(main_rate, sensitivity_rates, policy)
    spread = _spread(main_rate, terminal_growth, sensitivity_rates, policy)
    terminal, marks = _terminal_share(scenarios, policy)
    rate_flip = _rate_flip(scenarios, market_value, median_base, main_rate, terminal_growth, policy)
    quality = _quality_flags(rate_anchor, spread, terminal, rate_flip)
    warnings = _warnings(policy, rate_anchor, spread, terminal, rate_flip)
    details = {
        "version": GUARDRAIL_VERSION,
        "calibration": policy.calibration,
        "note": UNCALIBRATED_NOTE,
        "rate_anchor": rate_anchor,
        "discount_spread": spread,
        "terminal_share": terminal,
        "rate_flip": rate_flip,
    }
    return GuardrailAssessment(
        quality=quality,
        warnings=warnings,
        details=details,
        policy=_policy_snapshot(policy),
        scenario_marks=tuple(marks),
        result_fields=_result_fields(terminal, rate_flip),
    )


def _rate_anchor(
    main_rate: Decimal | None,
    sensitivity_rates: tuple[Decimal | None, ...],
    policy: GuardrailPolicy,
) -> dict[str, Any]:
    rates = [_rate_row("main", main_rate, policy)]
    rates.extend(_rate_row("sensitivity", rate, policy) for rate in sensitivity_rates)
    return {
        "calibration": policy.calibration,
        "source": "placeholder_no_risk_free_series",
        "note": UNCALIBRATED_NOTE,
        "risk_free_rate": str(policy.risk_free_rate),
        "equity_risk_premium_low": str(policy.equity_risk_premium_low),
        "equity_risk_premium_high": str(policy.equity_risk_premium_high),
        "band_low": str(policy.rate_floor),
        "band_high": str(policy.rate_ceiling),
        "band_rule": (
            "risk_free_rate + equity_risk_premium_low"
            " <= r <= risk_free_rate + equity_risk_premium_high"
        ),
        "rates": rates,
    }


def _rate_row(role: str, rate: Decimal | None, policy: GuardrailPolicy) -> dict[str, Any]:
    if rate is None:
        return {"role": role, "rate": None, "outcome": "not_checked"}
    return {"role": role, "rate": str(rate), "outcome": rate_anchor_outcome(rate, policy)}


def _spread(
    main_rate: Decimal | None,
    terminal_growth: Decimal | None,
    sensitivity_rates: tuple[Decimal | None, ...],
    policy: GuardrailPolicy,
) -> dict[str, Any]:
    pairs = [_spread_row("main", main_rate, terminal_growth, policy)]
    pairs.extend(
        _spread_row("sensitivity", rate, terminal_growth, policy) for rate in sensitivity_rates
    )
    return {
        "calibration": policy.calibration,
        "floor": str(policy.min_discount_spread),
        "rule": "r - g >= floor",
        "pairs": pairs,
    }


def _spread_row(
    role: str, rate: Decimal | None, terminal_growth: Decimal | None, policy: GuardrailPolicy
) -> dict[str, Any]:
    if rate is None or terminal_growth is None:
        return {
            "role": role,
            "rate": _text(rate),
            "terminal_growth": _text(terminal_growth),
            "spread": None,
            "outcome": "not_checked",
        }
    return {
        "role": role,
        "rate": str(rate),
        "terminal_growth": str(terminal_growth),
        "spread": str(rate - terminal_growth),
        "outcome": spread_outcome(rate, terminal_growth, policy),
    }


def _terminal_share(
    scenarios: list[dict[str, Any]], policy: GuardrailPolicy
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    marks: list[dict[str, Any]] = []
    low_cells: list[dict[str, Any]] = []
    for cell in scenarios:
        fraction = _decimal(cell.get("terminal_pv_fraction"))
        if fraction is None or terminal_share_outcome(fraction, policy) != "low_information":
            marks.append({})
            continue
        marks.append({"information": LOW_INFORMATION_MARK, "low_information": True})
        low_cells.append({"basis": cell.get("basis"), "rate": cell.get("rate")})
    main = next((cell for cell in scenarios if cell.get("basis") == "median"), None)
    fraction = _decimal(None if main is None else main.get("terminal_pv_fraction"))
    if fraction is None:
        outcome: TerminalOutcome = "not_checked"
        applied = False
    else:
        share = terminal_share_outcome(fraction, policy)
        outcome = share
        applied = share == "low_information"
    return {
        "calibration": policy.calibration,
        "threshold": str(policy.terminal_share_low_information),
        "rule": "terminal_pv_fraction > threshold",
        "mark": LOW_INFORMATION_MARK,
        "applied": applied,
        "outcome": outcome,
        "main": {
            "basis": None if main is None else main.get("basis"),
            "rate": None if main is None else main.get("rate"),
            "fraction": None if fraction is None else str(fraction),
            "outcome": outcome,
        },
        "low_information_cells": low_cells,
    }, marks


def _rate_flip(
    scenarios: list[dict[str, Any]],
    market_value: Decimal | None,
    median_base: Decimal | None,
    main_rate: Decimal | None,
    terminal_growth: Decimal | None,
    policy: GuardrailPolicy,
) -> dict[str, Any]:
    main = next((cell for cell in scenarios if cell.get("basis") == "median"), None)
    growth = _decimal(None if main is None else main.get("growth"))
    solved = main is not None and str(main.get("status")) == "ok" and growth is not None
    if (
        not solved
        or market_value is None
        or median_base is None
        or main_rate is None
        or terminal_growth is None
        or growth is None
    ):
        return {
            "calibration": policy.calibration,
            "shift": str(policy.rate_flip_shift),
            "basis": "median",
            "reading": "implied_growth_sign",
            "reading_note": READING_NOTE,
            "attempted": False,
            "conclusion_changes": None,
            "reason": "main_scenario_unsolved",
        }
    return flip_discount_rate(
        market_value=market_value,
        base=median_base,
        rate=main_rate,
        terminal_growth=terminal_growth,
        published_growth=growth,
        policy=policy,
    )


def _published_side(rate: Decimal, growth: Decimal) -> dict[str, Any]:
    conclusion = implied_growth_reading(growth)
    return {
        "rate": str(rate),
        "status": "ok",
        "growth": str(growth),
        "conclusion": conclusion,
        "conclusion_label": CONCLUSION_LABELS[conclusion],
    }


def _solved_side(
    market: Decimal, base: Decimal, rate: Decimal, terminal: Decimal
) -> dict[str, Any]:
    solution = solve(market, base, rate, terminal)
    growth = solution.growth
    if solution.status is not CalculationStatus.OK or growth is None:
        return {
            "rate": str(rate),
            "status": solution.status.value,
            "growth": None,
            "conclusion": "unsolved",
            "conclusion_label": CONCLUSION_LABELS["unsolved"],
            "reason": solution.reason,
        }
    conclusion = implied_growth_reading(growth)
    return {
        "rate": str(rate),
        "status": solution.status.value,
        "growth": str(growth),
        "conclusion": conclusion,
        "conclusion_label": CONCLUSION_LABELS[conclusion],
    }


def _quality_flags(
    rate_anchor: dict[str, Any],
    spread: dict[str, Any],
    terminal: dict[str, Any],
    rate_flip: dict[str, Any],
) -> tuple[str, ...]:
    flags: list[str] = []
    if any(row["outcome"] == "fail" for row in rate_anchor["rates"]):
        flags.append(RATE_OUTSIDE_ANCHOR)
    if any(row["outcome"] == "fail" for row in spread["pairs"]):
        flags.append(SPREAD_BELOW_FLOOR)
    if terminal["applied"]:
        flags.append(LOW_INFORMATION)
    if rate_flip["attempted"] and rate_flip["conclusion_changes"] is True:
        flags.append(FLIP_CHANGES)
    elif rate_flip["attempted"] and rate_flip["conclusion_changes"] is None:
        flags.append(FLIP_NOT_COMPARABLE)
    return tuple(flags)


def _warnings(
    policy: GuardrailPolicy,
    rate_anchor: dict[str, Any],
    spread: dict[str, Any],
    terminal: dict[str, Any],
    rate_flip: dict[str, Any],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if any(row["outcome"] == "fail" for row in rate_anchor["rates"]):
        warnings.append(
            "折现率超出未校准区间"
            f"[{_points(policy.rate_floor)}%, {_points(policy.rate_ceiling)}%]"
            "（无风险利率 + 股权风险溢价的占位锚，不是已研究的 A 股参数）。"
        )
    if any(row["outcome"] == "fail" for row in spread["pairs"]):
        warnings.append(
            "折现率与永续增长率的差额低于未校准下限"
            f"{_points(policy.min_discount_spread)} 个百分点。"
        )
    if terminal["applied"]:
        warnings.append(
            "终值现值占比超过"
            f"{_points(policy.terminal_share_low_information)}%，标为{LOW_INFORMATION_MARK}。"
        )
    if rate_flip["attempted"] and rate_flip["conclusion_changes"] is True:
        warnings.append(
            f"折现率上下浮动{_points(policy.rate_flip_shift)} 个百分点后，隐含增速的符号会改变。"
        )
    elif rate_flip["attempted"] and rate_flip["conclusion_changes"] is None:
        warnings.append(
            "折现率上下浮动"
            f"{_points(policy.rate_flip_shift)} 个百分点后，有一侧无法求解，"
            "不能确认隐含增速的符号是否保持。"
        )
    return tuple(warnings)


def _result_fields(terminal: dict[str, Any], rate_flip: dict[str, Any]) -> dict[str, Quantity]:
    fields: dict[str, Quantity] = {}
    if terminal["outcome"] != "not_checked":
        fields[LOW_INFORMATION] = Quantity(
            value=Decimal(1) if terminal["applied"] else Decimal(0), unit="indicator"
        )
    if rate_flip["attempted"] and rate_flip["conclusion_changes"] is None:
        fields[FLIP_CHANGES] = Quantity(
            value=None, unit="indicator", unknown_reason="rate_flip_not_comparable"
        )
    elif rate_flip["attempted"]:
        fields[FLIP_CHANGES] = Quantity(
            value=Decimal(1) if rate_flip["conclusion_changes"] else Decimal(0), unit="indicator"
        )
    return fields


def _policy_snapshot(policy: GuardrailPolicy) -> dict[str, Any]:
    return {
        "guardrails": {
            "version": GUARDRAIL_VERSION,
            "calibration": policy.calibration,
            "note": UNCALIBRATED_NOTE,
            "risk_free_rate": str(policy.risk_free_rate),
            "equity_risk_premium_low": str(policy.equity_risk_premium_low),
            "equity_risk_premium_high": str(policy.equity_risk_premium_high),
            "band_low": str(policy.rate_floor),
            "band_high": str(policy.rate_ceiling),
            "min_discount_spread": str(policy.min_discount_spread),
            "terminal_share_low_information": str(policy.terminal_share_low_information),
            "rate_flip_shift": str(policy.rate_flip_shift),
        }
    }


def _points(value: Decimal) -> str:
    rendered = format(value * 100, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)

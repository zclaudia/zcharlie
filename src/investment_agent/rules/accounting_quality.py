"""Shared accounting-quality pre-screen (screen entry, not a holding rule).

Three measurements, one gate, applied before a template's own ``screen``
conditions so a name that fails does not become a hit and therefore does not
enter the candidate set:

* ``ocf_to_net_income`` — latest annual operating cash flow / net profit
* ``share_dilution`` — change in total shares versus the prior annual report
* ``interest_coverage`` — (operating profit + finance expense) / finance expense

Banks and insurers are exempt: those statements do not support the same
cash-profit and interest-burden reading. Brokers are not exempt.

The default thresholds are an uncalibrated starting point. This repository
has not fitted them to A-share history. :class:`AccountingQualityPolicy`
refuses any calibration label other than ``uncalibrated``. Templates carry
the same numbers as parameters (see ``config/rule_templates``); a template
value replaces the policy default for that template only. A stored template
that predates the parameters still uses the policy defaults, so the gate
does not wait for a re-import.

Missing inputs, a ratio that cannot be computed, and a ``not_applicable``
factor outside the two exempt groups fail the gate. They are recorded as
``missing``. They are not treated as a pass. The one explicit non-event is a
non-positive finance expense (``non_positive_finance_expense``): there is no
net finance cost to cover, the factor is ``not_applicable``, and that signal
is skipped while the other two still have to pass.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from investment_agent.factors.library import FLAG_NON_POSITIVE_FINANCE_EXPENSE
from investment_agent.factors.registry import FactorValue
from investment_agent.rules.schema import RuleTemplate

FACTOR_OCF_TO_NI = "ocf_to_net_income"
FACTOR_SHARE_DILUTION = "share_dilution"
FACTOR_INTEREST_COVERAGE = "interest_coverage"
SIGNAL_FACTORS: tuple[str, ...] = (
    FACTOR_OCF_TO_NI,
    FACTOR_SHARE_DILUTION,
    FACTOR_INTEREST_COVERAGE,
)

PARAM_OCF_TO_NI_MIN = "ocf_to_net_income_min"
PARAM_SHARE_DILUTION_MAX = "share_dilution_max"
PARAM_INTEREST_COVERAGE_MIN = "interest_coverage_min"

# (factor, comparison the value must satisfy, template parameter)
_CHECKS: tuple[tuple[str, str, str], ...] = (
    (FACTOR_OCF_TO_NI, ">=", PARAM_OCF_TO_NI_MIN),
    (FACTOR_SHARE_DILUTION, "<=", PARAM_SHARE_DILUTION_MAX),
    (FACTOR_INTEREST_COVERAGE, ">=", PARAM_INTEREST_COVERAGE_MIN),
)

EXEMPT_GROUPS = frozenset({"banking", "insurance"})
"""Industry groups that skip the three signals. Brokers stay in the gate."""

CALIBRATION: Literal["uncalibrated"] = "uncalibrated"
UNCALIBRATED_NOTE = (
    "未校准的起始默认值，不是研究过的 A 股参数。"
    "经营现金流/净利润、股本稀释和利息覆盖只是筛选入口的起始门槛，没有用 A 股样本校准。"
)

_CONTEXT_REF_KEYS = frozenset({"provider", "as_of", "industry_group", "industry"})
_SKIP_FLAGS = frozenset({FLAG_NON_POSITIVE_FINANCE_EXPENSE})

Status = Literal["pass", "excluded", "exempt"]


@dataclass(frozen=True)
class AccountingQualityPolicy:
    """Starting thresholds for the shared gate.

    ``calibration`` stays ``uncalibrated`` until a cited A-share study
    replaces these placeholders. Do not relabel them as researched parameters.
    """

    calibration: Literal["uncalibrated"] = CALIBRATION
    ocf_to_net_income_min: float = 0.8
    """Operating cash flow must be at least this multiple of net profit."""
    share_dilution_max: float = 0.05
    """Total-share increase versus the prior annual report must not exceed this."""
    interest_coverage_min: float = 3.0
    """Approximate coverage must be at least this many times."""

    def __post_init__(self) -> None:
        if self.calibration != "uncalibrated":
            raise ValueError("accounting-quality thresholds must stay marked uncalibrated")
        numbers = (
            self.ocf_to_net_income_min,
            self.share_dilution_max,
            self.interest_coverage_min,
        )
        if not all(math.isfinite(number) for number in numbers):
            raise ValueError("accounting-quality thresholds must be finite")
        if self.ocf_to_net_income_min <= 0:
            raise ValueError("ocf/ni floor must be positive")
        if self.share_dilution_max < 0:
            raise ValueError("share-dilution cap cannot be negative")
        if self.interest_coverage_min <= 0:
            raise ValueError("interest-coverage floor must be positive")

    def params(self) -> dict[str, float]:
        return {
            PARAM_OCF_TO_NI_MIN: self.ocf_to_net_income_min,
            PARAM_SHARE_DILUTION_MAX: self.share_dilution_max,
            PARAM_INTEREST_COVERAGE_MIN: self.interest_coverage_min,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "calibration": self.calibration,
            "note": UNCALIBRATED_NOTE,
            "exempt_groups": sorted(EXEMPT_GROUPS),
            "signals": list(SIGNAL_FACTORS),
            "thresholds": self.params(),
        }


DEFAULT_ACCOUNTING_QUALITY = AccountingQualityPolicy()


def thresholds_for(
    template: RuleTemplate, policy: AccountingQualityPolicy = DEFAULT_ACCOUNTING_QUALITY
) -> dict[str, float]:
    """Policy defaults, overlaid with the same-named template parameters."""

    resolved = policy.params()
    for name in tuple(resolved):
        if name in template.params:
            resolved[name] = float(template.params[name])
    return resolved


@dataclass(frozen=True)
class AccountingQualityDecision:
    status: Status
    reasons: tuple[dict[str, Any], ...]
    """Failing signals, or the explicit not-applicable skip. Empty on a clean pass."""
    signals: tuple[str, ...]
    """Factor names that excluded the member, in check order."""

    @property
    def detail(self) -> str | None:
        if self.status != "excluded":
            return None
        return "pre_screen:" + ",".join(self.signals)


def pre_screen_report(
    *,
    policy: AccountingQualityPolicy,
    thresholds: Mapping[str, float],
    excluded: Sequence[str],
    exempted: Sequence[str],
) -> dict[str, Any]:
    """What one template's gate used and whom it excluded or exempted."""

    return {
        "kind": "accounting_quality",
        "calibration": policy.calibration,
        "note": UNCALIBRATED_NOTE,
        "exempt_groups": sorted(EXEMPT_GROUPS),
        "signals": list(SIGNAL_FACTORS),
        "thresholds": {name: float(thresholds[name]) for name in policy.params()},
        "excluded": sorted(excluded),
        "exempted": sorted(exempted),
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _plain(item)
            for key, item in value.items()
            if key not in _CONTEXT_REF_KEYS
        }
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


def _holds(actual: float, op: str, threshold: float) -> bool:
    if op == ">=":
        return actual >= threshold
    if op == "<=":
        return actual <= threshold
    raise ValueError(f"unsupported accounting-quality operator {op!r}")


def _reason(
    *,
    index: int,
    factor_name: str,
    op: str,
    threshold: float,
    param: str,
    factor: FactorValue | None,
    outcome: str,
) -> dict[str, Any]:
    if factor is None:
        actual = None
        quality = "missing"
        flags = ["factor_absent"]
        ref: dict[str, Any] = {}
    else:
        # A failed closed ``not_applicable`` keeps that quality so the record
        # shows why the value was unusable. A met check keeps the number.
        actual = factor.value if outcome in {"met", "not_met"} else None
        quality = factor.quality
        flags = list(factor.flags)
        ref = _plain(dict(factor.inputs_ref))
    return {
        "rule_path": f"pre_screen.all[{index}]",
        "factor": factor_name,
        "op": op,
        "threshold": threshold,
        "param": param,
        "actual": actual,
        "quality": quality,
        "flags": flags,
        "outcome": outcome,
        "snapshot_ref": ref,
    }


def assess_accounting_quality(
    *,
    industry_group: str,
    factors: Mapping[str, FactorValue],
    thresholds: Mapping[str, float],
) -> AccountingQualityDecision:
    """One member against the three signals.

    Exempt groups skip the gate entirely. Every other group must clear each
    signal that can be computed. A signal that cannot be computed fails.
    """

    if industry_group in EXEMPT_GROUPS:
        return AccountingQualityDecision("exempt", (), ())
    failing: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    signals: list[str] = []
    for index, (factor_name, op, param) in enumerate(_CHECKS):
        threshold = float(thresholds[param])
        factor = factors.get(factor_name)
        if factor is not None and factor.quality == "not_applicable":
            if _SKIP_FLAGS.intersection(factor.flags):
                skipped.append(
                    _reason(
                        index=index,
                        factor_name=factor_name,
                        op=op,
                        threshold=threshold,
                        param=param,
                        factor=factor,
                        outcome="not_applicable",
                    )
                )
                continue
            failing.append(
                _reason(
                    index=index,
                    factor_name=factor_name,
                    op=op,
                    threshold=threshold,
                    param=param,
                    factor=factor,
                    outcome="missing",
                )
            )
            signals.append(factor_name)
            continue
        if (
            factor is None
            or factor.quality == "missing"
            or factor.value is None
        ):
            failing.append(
                _reason(
                    index=index,
                    factor_name=factor_name,
                    op=op,
                    threshold=threshold,
                    param=param,
                    factor=factor,
                    outcome="missing",
                )
            )
            signals.append(factor_name)
            continue
        outcome = "met" if _holds(factor.value, op, threshold) else "not_met"
        if outcome == "not_met":
            failing.append(
                _reason(
                    index=index,
                    factor_name=factor_name,
                    op=op,
                    threshold=threshold,
                    param=param,
                    factor=factor,
                    outcome=outcome,
                )
            )
            signals.append(factor_name)
    if failing:
        return AccountingQualityDecision("excluded", tuple(failing), tuple(signals))
    return AccountingQualityDecision("pass", tuple(skipped), ())

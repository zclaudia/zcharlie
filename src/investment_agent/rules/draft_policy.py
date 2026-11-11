"""Rule draft contract (P1-C5): what the drafting LLM may return, and the checks.

The drafting agent (:mod:`investment_agent.rules.drafting`) parameterises a
template for one stock. It chooses **parameter values only**: conditions,
factors and zones come from the template, so an instance is
``template_ref + param overrides`` (funnel design 5.2 / 5.4). It never
states numbers as facts of its own - factor values are computed by the
deterministic factor library and may only be cited by name.

:func:`check_draft` validates a :class:`RuleDraftOutput` before anything is
stored; every problem is reported together (one retry gets all of them):

=========================  ===================================================
code                       meaning
=========================  ===================================================
``malformed_output``       not a JSON object matching the output schema
``unknown_param``          a param (or rationale entry) the template lacks
``param_not_finite``       NaN / infinity
``param_out_of_range``     value outside the template ``param_ranges``
``param_order``            a ``between`` condition's low param > high param
``rationale_missing``      an overridden param without a rationale entry
``rationale_duplicate``    two rationale entries for one param
``rationale_mismatch``     ``choice`` disagrees with ``params``
``citation_missing``       an adjusted param cites no factor
``citation_unknown``       cites a factor that is not in the draft input
``citation_null``          cites a factor whose value is null (missing / n.a.)
``trading_language``       trading-instruction wording in free text
``rule_<code>``            the built instance fails P1-C1 validation
                           (:mod:`investment_agent.rules.validation`)
=========================  ===================================================

**Trading language** (engineering baseline 3): free text (per-param reasons,
``overall_rationale``, ``watch_points``) may not contain order / position
wording such as 买入、卖出、加仓、减仓、清仓、建仓、止损、目标价、仓位建议, nor
``buy`` / ``sell`` / ``stop loss`` / ``target price`` in English. Zone
names are fixed identifiers (``buy_zone`` / ``drop`` / ``add`` / ``trim`` /
``exit_sell`` / ``exit_review``) and are allowed as identifiers; the English check treats
``_`` as part of a word, so ``buy_zone`` never matches ``buy``.

**Numeric overrides fail closed** (review 2026-11-07 P1-8). An LLM draft is a
single-stock, single-``as_of`` answer, so it is not evidence that a production
threshold should move. The project has no out-of-sample calibration artifact
(no backtest / IC / walk-forward output, no industry historical distribution
file), and this module does not invent one. Unless a caller supplies a
:class:`CalibrationEvidence` that passes :func:`calibration_allows_overrides`
*and* the template is at least ``backtested``, LLM drafts are normalized to
the template defaults: :func:`normalize_uncalibrated_output` drops every
numeric override and re-labels its rationale as ``choice=default``, while
keeping the proposed value, reason and citations in
``draft_rationale.suppressed_overrides`` as audit only. Explaining defaults and
data gaps is still a valid draft; changing a number is not.

``preference_notes`` (the user's dismiss / reject reasons) are background
only. They are never calibration evidence and never justify tightening or
loosening a threshold; with overrides disabled they cannot reach an instance
at all.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from investment_agent.factors.registry import FactorValue
from investment_agent.llm.glossary import readability_rules
from investment_agent.rules.schema import ConditionGroup, RuleInstance, RuleTemplate, param_ref
from investment_agent.rules.validation import RuleContext, check_instance

DRAFT_SCHEMA_VERSION = "rule_draft.v2"
DRAFT_SCOPE = "candidate"
"""The default drafting scope: candidate rules (``buy_zone`` / ``drop``).

Holding rules (``add`` / ``trim`` / ``exit_sell`` / ``exit_review``, WP8) reuse the same
drafting contract with ``scope="holding"``; conditions, factors and zones still
come from the template, so only the zone set and the stored scope differ.
"""
DEFAULTS_ONLY_POLICY: Literal["defaults_only"] = "defaults_only"
CALIBRATED_OVERRIDES_POLICY: Literal["calibrated_overrides"] = "calibrated_overrides"
"""``defaults_only``: LLM numeric overrides are suppressed (no calibration
evidence). ``calibrated_overrides``: a :class:`CalibrationEvidence` for this
exact template version passed :func:`calibration_allows_overrides`."""
MIN_CALIBRATION_SAMPLE = 100
"""Smallest recorded sample an override policy accepts (cross-sectional
periods, not single observations); a guess, not a substitute for the report."""
DraftScope = Literal["candidate", "holding"]


# --------------------------------------------------------------------------- #
# Output schema (PromptedOutput: the JSON schema is embedded in the prompt)
# --------------------------------------------------------------------------- #


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParamRationale(_Out):
    """Why one parameter keeps its template default or is adjusted."""

    param: str = Field(description="模板 params 中的参数名")
    choice: Literal["default", "adjusted"] = Field(
        description="default=保持模板默认值；adjusted=在 params 中给出了不同于默认值的取值"
    )
    reason: str = Field(min_length=1, description="中文理由；只引用输入中的因子，不给交易指令")
    cited_factors: list[str] = Field(
        default_factory=list,
        description="理由所依据的因子名；必须是输入 factors 中 value 非 null 的因子",
    )


class RuleDraftOutput(_Out):
    """The drafting agent's whole answer."""

    params: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "需要覆盖的模板参数及取值（必须在 param_ranges 内）。"
            "仅当 override_policy=calibrated_overrides 时允许非空；"
            "defaults_only 下必须为空"
        ),
    )
    rationale: list[ParamRationale] = Field(default_factory=list)
    overall_rationale: str = Field(min_length=1, description="整体参数化思路（中文）")
    watch_points: list[str] = Field(
        default_factory=list, description="后续需要复核的数据点或证伪信号（中文，无交易指令）"
    )


DEFAULTS_ONLY_PARAMS_DESCRIPTION = "未校准模板下必须为空：不得给出数值覆盖；解释默认参数即可"
CALIBRATED_PARAMS_DESCRIPTION = (
    "需要覆盖的模板参数及取值（必须在 param_ranges 内）；不调整的参数不要列出"
)


def output_json_schema(policy: str = DEFAULTS_ONLY_POLICY) -> str:
    """The embedded JSON Schema; ``params`` follows the active override policy."""

    schema = RuleDraftOutput.model_json_schema()
    schema["properties"]["params"]["description"] = (
        CALIBRATED_PARAMS_DESCRIPTION
        if policy == CALIBRATED_OVERRIDES_POLICY
        else DEFAULTS_ONLY_PARAMS_DESCRIPTION
    )
    return json.dumps(schema, ensure_ascii=False, sort_keys=True)


RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE = """你是个人投研系统中的"规则起草" Agent。你的输出只是一份"研究优先级规则"的参数化草稿，供用户审阅、修改或驳回；它不是投资建议，也不构成任何交易指令。

背景：
- 模板（template）由项目定义：筛选条件 screen、候选区间 candidate_zones（buy_zone / drop）、持仓区间 holding_zones（add / trim / exit_sell / exit_review），以及默认参数 params 和允许范围 param_ranges。条件中的 "$name" 引用参数 name。exit_sell 是论文被证伪时的卖出信号，不改账本；exit_review 只进入重审队列，不作为卖出信号。
{policy_background}
- 条件、因子、区间全部来自模板，不能新增、删除或改写任何条件。
- 输入中的因子值由确定性系统计算（含 pe_percentile_5y / pb_percentile_5y 等自身 5 年历史分位）。你不计算、不推测任何数值事实，只能按因子名引用输入中已有的值。

要求：
{policy_rules}
4. overall_rationale 概述参数化思路；watch_points 列出后续需要复核的数据点或证伪信号。
5. 禁止交易指令语言：不得出现"买入""卖出""加仓""减仓""清仓""建仓""止损""止盈""目标价""仓位建议"等字样，不得给出收益承诺。提到区间时使用中性中文名（可研究区间、移出候选、持仓 add 区间、持仓 trim 区间、证伪即卖、触发重审），不要用交易动作描述区间。
6. 输出中文，只返回满足以下 JSON Schema 的单个 JSON 对象，不要输出其他文字：
{output_schema}"""

_DEFAULTS_ONLY_BACKGROUND = """- 模板目前全部是 calibration_status=uncalibrated（未校准），也没有样本外校准证据：默认参数只是尚未经过回测的假设。
- 因此本次草稿不改变任何数值参数：params 必须为空，所有参数保持模板默认值。你只能解释默认参数在本股上的含义、指出数据缺口和后续复核点，不得生成数值覆盖。
- 规则实例 = 模板引用 + 模板默认参数。"""

_CALIBRATED_BACKGROUND = """- 该模板版本已有样本外校准证据（walk-forward / 样本外）：参数覆盖在允许范围内是合法的研究优先级调整。
- 你可以为该股选择模板参数的取值（params）；未调整的参数保持模板默认值。
- 规则实例 = 模板引用 + 参数覆盖。"""

_DEFAULTS_ONLY_RULES = """1. params 留空（{{}}）。不要为任何参数给出数值覆盖；数值覆盖会在入库前被丢弃，只作为审计记录。
2. rationale：为关键参数写 choice=default 的说明，解释该默认值在本股语境下的含义与不确定性。cited_factors 只能写输入 factors 中存在且 value 非 null 的因子名。value 为 null（quality 为 missing / not_applicable）的因子不能作为论据，只能在 watch_points 中作为数据缺口提及。
3. preference_notes（用户过去驳回或忽略时写下的理由）只是背景偏好，不是校准证据：不得据此收紧或放松任何阈值。结合 screen_result、行业组与申万行业解释默认参数即可。输入可能带 facts.business_segments（最新报告期主营构成）：只作为理解业务的事实背景，不是因子，不能写进 cited_factors，也不参与任何数值判断。"""

_CALIBRATED_RULES = """1. params 只能包含模板 params 中已有的参数名，取值必须落在 param_ranges 的 [min, max] 闭区间内；不需要调整的参数不要放进 params。between 条件的下限参数不得大于上限参数。
2. rationale：params 中的每个参数各写一条 choice=adjusted 的说明；也可以为保持默认的关键参数写 choice=default 的说明。cited_factors 只能写输入 factors 中存在且 value 非 null 的因子名，choice=adjusted 时至少引用一个因子。value 为 null（quality 为 missing / not_applicable）的因子不能作为论据，只能在 watch_points 中作为数据缺口提及。
3. preference_notes（用户过去驳回或忽略时写下的理由）只是背景偏好，不是校准证据：不得仅凭 preference_notes 收紧或放松任何阈值；参数松紧必须由该版本已有的样本外校准证据与输入因子支撑。结合 screen_result、行业组与申万行业选择取值。输入可能带 facts.business_segments（最新报告期主营构成）：只作为理解业务的事实背景，不是因子，不能写进 cited_factors，也不参与任何数值判断。"""


def system_prompt(policy: str = DEFAULTS_ONLY_POLICY) -> str:
    """The drafting system prompt for ``policy`` (defaults-only unless calibrated).

    An unknown policy falls back to the conservative ``defaults_only`` text,
    matching :func:`validate_draft`'s fail-closed behaviour.
    """

    calibrated = policy == CALIBRATED_OVERRIDES_POLICY
    return (
        RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE.format(
            policy_background=_CALIBRATED_BACKGROUND if calibrated else _DEFAULTS_ONLY_BACKGROUND,
            policy_rules=_CALIBRATED_RULES if calibrated else _DEFAULTS_ONLY_RULES,
            output_schema=output_json_schema(
                CALIBRATED_OVERRIDES_POLICY if calibrated else DEFAULTS_ONLY_POLICY
            ),
        )
        + "\n\n"
        + readability_rules()
    )


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DraftIssue:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


def feedback_message(issues: Iterable[DraftIssue]) -> str:
    """Retry feedback appended to the user message (Chinese, one issue per line)."""

    lines = "\n".join(f"- {issue}" for issue in issues)
    return (
        "\n\n上一次输出未通过校验，请逐条修正后重新输出完整的 JSON 对象"
        "（只选择参数取值，不得改写条件）：\n" + lines
    )


# --------------------------------------------------------------------------- #
# Trading-instruction language
# --------------------------------------------------------------------------- #

TRADING_TERMS_ZH: tuple[str, ...] = (
    "买入",
    "卖出",
    "加仓",
    "减仓",
    "清仓",
    "建仓",
    "平仓",
    "补仓",
    "满仓",
    "半仓",
    "重仓",
    "轻仓",
    "抄底",
    "止损",
    "止盈",
    "目标价",
    "仓位建议",
    "建议仓位",
    "下单",
    "调仓",
    "收益承诺",
    "保证收益",
    "稳赚",
    "必涨",
)
_TRADING_EN = re.compile(
    r"(?<![A-Za-z0-9_])(?:buy|sell|stop[- ]?loss|take[- ]?profit|target[- ]price|"
    r"price[- ]target|position[- ]siz(?:e|ing))(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def trading_terms(text: str) -> list[str]:
    """Trading-instruction terms found in ``text`` (empty when clean)."""

    found = [term for term in TRADING_TERMS_ZH if term in text]
    found.extend(match.group(0) for match in _TRADING_EN.finditer(text))
    return found


# --------------------------------------------------------------------------- #
# Calibration evidence (fail closed without a verifiable contract)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CalibrationEvidence:
    """A caller-supplied description of the out-of-sample study behind a
    template's parameter values.

    This is a *contract*, not an implementation: nothing in the repository
    produces or stores such a record today (no backtest / IC / walk-forward
    artifact, no industry historical distribution). A caller may pass one -
    for tests, or once a real study exists - and :func:`calibration_allows_overrides`
    decides whether it is strong enough to permit LLM overrides for this exact
    template version. Absent or malformed evidence fails closed.
    """

    template_ref: str
    """Exact ``<id>@<version>`` the evidence covers; another version never inherits it."""
    method: Literal["walk_forward", "out_of_sample"]
    sample_periods: int
    """Distinct cross-sectional periods in the out-of-sample window."""
    period_start: date
    period_end: date
    metrics: Mapping[str, float] = field(default_factory=dict)
    """e.g. ``{"hit_rate": 0.58, "ic": 0.06}``; only presence is required here."""
    report_ref: str = ""
    """Path / URL / hash of the study a human can audit."""


def calibration_allows_overrides(
    evidence: CalibrationEvidence | None, template: RuleTemplate
) -> bool:
    """``True`` only for strong, matching, non-fabricated evidence.

    Requirements (all must hold): the template is at least ``backtested``; the
    evidence names this exact template version; the method is walk-forward or
    out-of-sample; the sample covers at least :data:`MIN_CALIBRATION_SAMPLE`
    periods over a non-empty, ordered window; and it has at least one metric
    plus an audit reference. Anything else - including ``None`` - is ``False``.
    """

    if evidence is None:
        return False
    if template.calibration_status == "uncalibrated":
        return False
    if evidence.template_ref != template.ref:
        return False
    if evidence.method not in {"walk_forward", "out_of_sample"}:
        return False
    if evidence.sample_periods < MIN_CALIBRATION_SAMPLE:
        return False
    if evidence.period_start > evidence.period_end:
        return False
    if not evidence.metrics:
        return False
    return bool(evidence.report_ref.strip())


def override_policy(
    template: RuleTemplate, evidence: CalibrationEvidence | None = None
) -> Literal["defaults_only", "calibrated_overrides"]:
    """Which numeric-override policy applies to an LLM draft of ``template``."""

    if calibration_allows_overrides(evidence, template):
        return "calibrated_overrides"
    return DEFAULTS_ONLY_POLICY


def normalize_uncalibrated_output(
    output: RuleDraftOutput, template: RuleTemplate
) -> tuple[RuleDraftOutput, list[dict[str, Any]]]:
    """Drop numeric overrides; return ``(defaults-only output, suppressed audit)``.

    Each changed parameter becomes a ``choice=default`` rationale entry (its
    reason is kept and prefixed so the reviewer knows the value was not
    applied), and the proposed value / citations are returned separately for
    ``draft_rationale.suppressed_overrides``. The draft still explains the
    defaults and the data gaps - it just cannot move a number.
    """

    overrides = instance_params(output, template)
    if not overrides:
        return output, []
    suppressed: list[dict[str, Any]] = [
        {
            "param": name,
            "proposed": value,
            "default": template.params.get(name),
            "reason": _reason_for(output, name),
            "cited_factors": _factors_for(output, name),
        }
        for name, value in overrides.items()
    ]
    entries: list[ParamRationale] = []
    kept: set[str] = set()
    for entry in output.rationale:
        if entry.param in overrides and entry.param not in kept:
            kept.add(entry.param)
            entries.append(
                ParamRationale(
                    param=entry.param,
                    choice="default",
                    reason=(
                        "未采用该数值覆盖（模板未校准或缺少样本外校准证据），"
                        "保留模板默认值。原理由：" + entry.reason
                    ),
                    cited_factors=list(entry.cited_factors),
                )
            )
        elif entry.param not in overrides:
            entries.append(entry)
    for name in overrides:
        if name in kept:
            continue
        entries.append(
            ParamRationale(
                param=name,
                choice="default",
                reason="未采用该数值覆盖（模板未校准或缺少样本外校准证据），保留模板默认值。",
            )
        )
    normalized = RuleDraftOutput(
        params={},
        rationale=entries,
        overall_rationale=(
            output.overall_rationale
            + "\n（治理：无样本外校准证据，数值覆盖未应用，仅保留模板默认参数。）"
        ),
        watch_points=list(output.watch_points),
    )
    return normalized, suppressed


def _reason_for(output: RuleDraftOutput, name: str) -> str:
    for entry in output.rationale:
        if entry.param == name:
            return entry.reason
    return ""


def _factors_for(output: RuleDraftOutput, name: str) -> list[str]:
    for entry in output.rationale:
        if entry.param == name:
            return list(entry.cited_factors)
    return []


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _between_pairs(group: ConditionGroup, path: str) -> list[tuple[str, str, str]]:
    """``(path, low_param, high_param)`` of every ``between`` on two params."""

    pairs: list[tuple[str, str, str]] = []
    for index, child in enumerate(group.children):
        child_path = f"{path}.{group.mode}[{index}]"
        if isinstance(child, ConditionGroup):
            pairs.extend(_between_pairs(child, child_path))
        elif child.op == "between" and isinstance(child.value, list) and len(child.value) == 2:
            low, high = (param_ref(item) for item in child.value)
            if low is not None and high is not None:
                pairs.append((child_path, low, high))
    return pairs


def template_groups(template: RuleTemplate) -> list[tuple[str, ConditionGroup]]:
    groups = [("screen", template.screen)]
    groups += [(f"candidate_zones.{k}", v) for k, v in sorted(template.candidate_zones.items())]
    groups += [(f"holding_zones.{k}", v) for k, v in sorted(template.holding_zones.items())]
    return groups


def citable_factors(factors: Mapping[str, FactorValue]) -> frozenset[str]:
    return frozenset(name for name, item in factors.items() if item.value is not None)


def check_draft(
    output: RuleDraftOutput,
    *,
    template: RuleTemplate,
    factors: Mapping[str, FactorValue],
) -> list[DraftIssue]:
    """Every policy problem of ``output`` (parameters, rationale, citations, wording)."""

    issues: list[DraftIssue] = []

    def add(code: str, path: str, message: str) -> None:
        issues.append(DraftIssue(code, path, message))

    # -- parameters --------------------------------------------------------- #
    usable: dict[str, float] = {}
    for name in sorted(output.params):
        value = output.params[name]
        path = f"params.{name}"
        bounds = template.param_ranges.get(name)
        if name not in template.params or bounds is None:
            add("unknown_param", path, f"template {template.ref} has no param {name!r}")
        elif not math.isfinite(value):
            add("param_not_finite", path, f"{value} is not a finite number")
        elif not bounds.min <= value <= bounds.max:
            add(
                "param_out_of_range",
                path,
                f"{value} outside the allowed range [{bounds.min}, {bounds.max}]",
            )
        else:
            usable[name] = value
    merged = {**template.params, **usable}
    for group_path, group in template_groups(template):
        for path, low, high in _between_pairs(group, group_path):
            if low in merged and high in merged and merged[low] > merged[high]:
                add(
                    "param_order",
                    path,
                    f"between needs {low} <= {high} (got {merged[low]} > {merged[high]})",
                )

    # -- rationale ---------------------------------------------------------- #
    citable = citable_factors(factors)
    seen: set[str] = set()
    for index, entry in enumerate(output.rationale):
        path = f"rationale[{index}]"
        if entry.param not in template.params:
            add("unknown_param", f"{path}.param", f"template has no param {entry.param!r}")
        if entry.param in seen:
            add("rationale_duplicate", f"{path}.param", f"second entry for {entry.param!r}")
        seen.add(entry.param)
        overridden = entry.param in output.params and output.params[entry.param] != template.params.get(
            entry.param
        )
        if entry.choice == "adjusted" and not overridden:
            add(
                "rationale_mismatch",
                f"{path}.choice",
                f"'adjusted' but params does not change {entry.param!r} from its default",
            )
        if entry.choice == "default" and overridden:
            add(
                "rationale_mismatch",
                f"{path}.choice",
                f"'default' but params sets {entry.param!r} to {output.params[entry.param]}",
            )
        if entry.choice == "adjusted" and not entry.cited_factors:
            add("citation_missing", f"{path}.cited_factors", "an adjustment must cite a factor")
        for position, factor in enumerate(entry.cited_factors):
            cite_path = f"{path}.cited_factors[{position}]"
            if factor not in factors:
                add("citation_unknown", cite_path, f"{factor!r} is not in the input factors")
            elif factor not in citable:
                add(
                    "citation_null",
                    cite_path,
                    f"{factor!r} has no value ({factors[factor].quality}); "
                    "mention data gaps in watch_points only",
                )
    for name in sorted(set(output.params) - seen):
        add("rationale_missing", f"params.{name}", f"no rationale entry for {name!r}")

    # -- wording ------------------------------------------------------------ #
    texts: list[tuple[str, str]] = [("overall_rationale", output.overall_rationale)]
    texts += [(f"rationale[{i}].reason", entry.reason) for i, entry in enumerate(output.rationale)]
    texts += [(f"watch_points[{i}]", text) for i, text in enumerate(output.watch_points)]
    for path, text in texts:
        found = trading_terms(text)
        if found:
            add(
                "trading_language",
                path,
                "trading-instruction wording is not allowed: " + ", ".join(dict.fromkeys(found)),
            )
    return issues


def instance_params(output: RuleDraftOutput, template: RuleTemplate) -> dict[str, float]:
    """Overrides that differ from the template default (stable content hash)."""

    return {
        name: float(value)
        for name, value in sorted(output.params.items())
        if template.params.get(name) != value
    }


def rationale_document(
    output: RuleDraftOutput,
    template: RuleTemplate,
    *,
    extra: Mapping[str, Any] | None = None,
    policy: str = DEFAULTS_ONLY_POLICY,
    suppressed_overrides: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The ``draft_rationale`` JSON: per-param reasons, overall, watch points."""

    overrides = instance_params(output, template)
    params: dict[str, Any] = {}
    for entry in output.rationale:
        params[entry.param] = {
            "choice": entry.choice,
            "default": template.params.get(entry.param),
            "value": overrides.get(entry.param, template.params.get(entry.param)),
            "reason": entry.reason,
            "cited_factors": list(entry.cited_factors),
        }
    document: dict[str, Any] = {
        "schema": DRAFT_SCHEMA_VERSION,
        "template_ref": template.ref,
        "calibration_status": template.calibration_status,
        "override_policy": policy,
        "params": params,
        "overall_rationale": output.overall_rationale,
        "watch_points": list(output.watch_points),
        **dict(extra or {}),
    }
    suppressed = list(suppressed_overrides)
    if suppressed:
        document["suppressed_overrides"] = suppressed
    return document


def build_instance(
    output: RuleDraftOutput,
    template: RuleTemplate,
    *,
    symbol: str,
    drafted_by: str,
    rationale: Mapping[str, Any],
    context: RuleContext | None = None,
    scope: DraftScope = "candidate",
) -> tuple[RuleInstance | None, list[DraftIssue]]:
    """The scope's instance, or the P1-C1 issues that block it."""

    try:
        instance = RuleInstance(
            template_ref=template.ref,
            symbol=symbol,
            scope=scope,
            params=instance_params(output, template),
            drafted_by=drafted_by[:64],
            draft_rationale=json.dumps(rationale, ensure_ascii=False, sort_keys=True),
        )
    except ValueError as exc:
        return None, [DraftIssue("rule_schema", "$", str(exc))]
    issues = [
        DraftIssue(f"rule_{issue.code}", issue.path, issue.message)
        for issue in check_instance(instance, template, context)
    ]
    return (instance if not issues else None), issues


def validate_draft(
    output: RuleDraftOutput,
    *,
    template: RuleTemplate,
    factors: Mapping[str, FactorValue],
    symbol: str,
    drafted_by: str,
    extra: Mapping[str, Any] | None = None,
    context: RuleContext | None = None,
    scope: DraftScope = "candidate",
    evidence: CalibrationEvidence | None = None,
) -> tuple[RuleInstance | None, list[DraftIssue]]:
    """Policy checks, then (only when clean) full rule-instance validation.

    ``evidence`` is the caller's calibration contract. Without one that
    :func:`calibration_allows_overrides` accepts, the numeric overrides are
    normalized away first (``defaults_only``): the returned instance carries no
    ``params``, and the proposed values are recorded in
    ``draft_rationale.suppressed_overrides``.
    """

    # Validate the raw answer first: malformed / out-of-range / uncited
    # proposals still get the normal retry feedback instead of disappearing
    # into the suppression audit.
    issues = check_draft(output, template=template, factors=factors)
    if issues:
        return None, issues
    policy = override_policy(template, evidence)
    suppressed: list[dict[str, Any]] = []
    checked = output
    if policy == DEFAULTS_ONLY_POLICY:
        checked, suppressed = normalize_uncalibrated_output(output, template)
        issues = check_draft(checked, template=template, factors=factors)
        if issues:
            return None, issues
    return build_instance(
        checked,
        template,
        symbol=symbol,
        drafted_by=drafted_by,
        rationale=rationale_document(
            checked,
            template,
            extra=extra,
            policy=policy,
            suppressed_overrides=suppressed,
        ),
        context=context,
        scope=scope,
    )


__all__ = [
    "CALIBRATED_OVERRIDES_POLICY",
    "DEFAULTS_ONLY_POLICY",
    "DRAFT_SCHEMA_VERSION",
    "DRAFT_SCOPE",
    "MIN_CALIBRATION_SAMPLE",
    "CalibrationEvidence",
    "DraftIssue",
    "DraftScope",
    "ParamRationale",
    "RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE",
    "RuleDraftOutput",
    "build_instance",
    "calibration_allows_overrides",
    "check_draft",
    "citable_factors",
    "feedback_message",
    "instance_params",
    "normalize_uncalibrated_output",
    "output_json_schema",
    "override_policy",
    "rationale_document",
    "system_prompt",
    "trading_terms",
    "validate_draft",
]

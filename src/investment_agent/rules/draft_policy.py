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
``exit_review``) and are allowed as identifiers; the English check treats
``_`` as part of a word, so ``buy_zone`` never matches ``buy``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from investment_agent.factors.registry import FactorValue
from investment_agent.rules.schema import ConditionGroup, RuleInstance, RuleTemplate, param_ref
from investment_agent.rules.validation import RuleContext, check_instance

DRAFT_SCHEMA_VERSION = "rule_draft.v1"
DRAFT_SCOPE: Literal["candidate"] = "candidate"


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
        description="需要覆盖的模板参数及取值（必须在 param_ranges 内）；不调整的参数不要列出",
    )
    rationale: list[ParamRationale] = Field(default_factory=list)
    overall_rationale: str = Field(min_length=1, description="整体参数化思路（中文）")
    watch_points: list[str] = Field(
        default_factory=list, description="后续需要复核的数据点或证伪信号（中文，无交易指令）"
    )


def output_json_schema() -> str:
    return json.dumps(RuleDraftOutput.model_json_schema(), ensure_ascii=False, sort_keys=True)


RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE = """你是个人投研系统中的"规则起草" Agent。你的输出只是一份"研究优先级规则"的参数化草稿，供用户审阅、修改或驳回；它不是投资建议，也不构成任何交易指令。

背景：
- 模板（template）由项目定义：筛选条件 screen、候选区间 candidate_zones（buy_zone / drop）、持仓区间 holding_zones（add / trim / exit_review），以及默认参数 params 和允许范围 param_ranges。条件中的 "$name" 引用参数 name。
- 模板目前全部是 calibration_status=uncalibrated（未校准）：默认参数只是尚未经过回测的假设，参数调整同样是假设，理由要说明依据与不确定性。
- 你只能为这只股票选择模板参数的取值。条件、因子、区间全部来自模板，不能新增、删除或改写任何条件。规则实例 = 模板引用 + 参数覆盖。
- 输入中的因子值由确定性系统计算（含 pe_percentile_5y / pb_percentile_5y 等自身 5 年历史分位）。你不计算、不推测任何数值事实，只能按因子名引用输入中已有的值。

要求：
1. params 只能包含模板 params 中已有的参数名，取值必须落在 param_ranges 的 [min, max] 闭区间内；不需要调整的参数不要放进 params。between 条件的下限参数不得大于上限参数。
2. rationale：params 中的每个参数各写一条 choice=adjusted 的说明；也可以为保持默认的关键参数写 choice=default 的说明。cited_factors 只能写输入 factors 中存在且 value 非 null 的因子名，choice=adjusted 时至少引用一个因子。value 为 null（quality 为 missing / not_applicable）的因子不能作为论据，只能在 watch_points 中作为数据缺口提及。
3. 结合 screen_result（该股满足或未满足哪些筛选条件）、行业组与申万行业、preference_notes（用户过去驳回或忽略时写下的理由，代表其偏好）决定参数松紧。
4. overall_rationale 概述参数化思路；watch_points 列出后续需要复核的数据点或证伪信号。
5. 禁止交易指令语言：不得出现"买入""卖出""加仓""减仓""清仓""建仓""止损""止盈""目标价""仓位建议"等字样，不得给出收益承诺。提到区间时只使用标识符 buy_zone、drop、add、trim、exit_review。
6. 输出中文，只返回满足以下 JSON Schema 的单个 JSON 对象，不要输出其他文字：
{output_schema}"""


def system_prompt() -> str:
    return RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE.format(output_schema=output_json_schema())


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
    return {
        "schema": DRAFT_SCHEMA_VERSION,
        "template_ref": template.ref,
        "calibration_status": template.calibration_status,
        "params": params,
        "overall_rationale": output.overall_rationale,
        "watch_points": list(output.watch_points),
        **dict(extra or {}),
    }


def build_instance(
    output: RuleDraftOutput,
    template: RuleTemplate,
    *,
    symbol: str,
    drafted_by: str,
    rationale: Mapping[str, Any],
    context: RuleContext | None = None,
) -> tuple[RuleInstance | None, list[DraftIssue]]:
    """The candidate-scope instance, or the P1-C1 issues that block it."""

    try:
        instance = RuleInstance(
            template_ref=template.ref,
            symbol=symbol,
            scope=DRAFT_SCOPE,
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
) -> tuple[RuleInstance | None, list[DraftIssue]]:
    """Policy checks, then (only when clean) full rule-instance validation."""

    issues = check_draft(output, template=template, factors=factors)
    if issues:
        return None, issues
    return build_instance(
        output,
        template,
        symbol=symbol,
        drafted_by=drafted_by,
        rationale=rationale_document(output, template, extra=extra),
        context=context,
    )


__all__ = [
    "DRAFT_SCHEMA_VERSION",
    "DRAFT_SCOPE",
    "RULE_DRAFT_SYSTEM_PROMPT_TEMPLATE",
    "DraftIssue",
    "ParamRationale",
    "RuleDraftOutput",
    "build_instance",
    "check_draft",
    "citable_factors",
    "feedback_message",
    "instance_params",
    "output_json_schema",
    "rationale_document",
    "system_prompt",
    "trading_terms",
    "validate_draft",
]

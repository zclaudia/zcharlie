"""Rule validator (P1-C1): typed, aggregated semantic checks.

:func:`parse_template` / :func:`parse_instance` turn raw mappings (YAML /
JSON / an LLM draft) into models and run every check; all problems are
reported together in one :class:`RuleValidationError` so a draft can be
fixed in one round. Issue codes:

==========================  ==================================================
code                        meaning
==========================  ==================================================
``schema``                  wrong shape / type / unknown key (pydantic)
``unknown_factor``          factor is not in the factor registry
``pending_factor``          a P2 position factor outside a holding zone
``invalid_op``              operator not in ``< <= > >= == != between in``
``op_value_mismatch``       value type does not fit the operator
``free_text_value``         a string value that is not a ``$param`` reference
``param_undefined``         ``$param`` / override / range for an unknown param
``param_without_range``     a template param without ``param_ranges`` entry
``param_range_invalid``     ``min > max``
``param_out_of_range``      default or override outside ``[min, max]``
``zone_not_allowed``        zone name not in the scope's enum (``sell`` never)
``zone_missing``            a template lacks one of the scope's zones
``empty_group``             ``all`` / ``any`` with no conditions
``nesting_too_deep``        condition groups nested deeper than two levels
``unknown_industry_group``  ``applicable_groups`` entry not in the group map
``empty_applicable_groups`` no applicable group
``template_ref_mismatch``   instance ``template_ref`` is not ``<id>@<version>``
                            of the template it is validated against
``invalid_symbol``          instance symbol is not a canonical A-share ticker
==========================  ==================================================

**Pending factors.** Position factors (``position_weight``, ``gain_vs_cost``,
``holding_days``) come from holding snapshots in P2. Templates may already
use them in holding zones (P1 defines holding zones without executing them);
anywhere else - ``screen`` or candidate zones - they are rejected, and the
screen engine refuses any factor missing from the registry.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from investment_agent.domain.a_share import parse_a_share_ticker
from investment_agent.rules.schema import (
    COMPARISON_OPS,
    EQUALITY_OPS,
    OPS,
    SCOPE_ZONES,
    Condition,
    ConditionGroup,
    RuleInstance,
    RuleTemplate,
    param_ref,
)

IssueCode = Literal[
    "schema",
    "unknown_factor",
    "pending_factor",
    "invalid_op",
    "op_value_mismatch",
    "free_text_value",
    "param_undefined",
    "param_without_range",
    "param_range_invalid",
    "param_out_of_range",
    "zone_not_allowed",
    "zone_missing",
    "empty_group",
    "nesting_too_deep",
    "unknown_industry_group",
    "empty_applicable_groups",
    "template_ref_mismatch",
    "invalid_symbol",
]

PENDING_FACTORS: frozenset[str] = frozenset({"position_weight", "gain_vs_cost", "holding_days"})
"""Position factors defined in P2 (holding snapshots); holding zones only."""
MAX_GROUP_DEPTH = 2
"""A root group may contain groups; those may only contain conditions."""


@dataclass(frozen=True)
class RuleIssue:
    code: IssueCode
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


class RuleValidationError(ValueError):
    def __init__(self, issues: Iterable[RuleIssue], *, source: str | None = None) -> None:
        self.issues = tuple(issues)
        self.source = source
        head = f"{source}: " if source else ""
        super().__init__(
            head + f"{len(self.issues)} rule issue(s): " + "; ".join(map(str, self.issues))
        )

    @property
    def codes(self) -> set[str]:
        return {issue.code for issue in self.issues}


@dataclass(frozen=True)
class RuleContext:
    """What rules may reference: factor names and industry groups."""

    factor_names: frozenset[str]
    industry_groups: frozenset[str]
    pending_factors: frozenset[str] = field(default=PENDING_FACTORS)


def default_context() -> RuleContext:
    from investment_agent.factors.catalog import DEFAULT_REGISTRY
    from investment_agent.factors.industry_groups import default_industry_groups

    return RuleContext(
        factor_names=frozenset(DEFAULT_REGISTRY.names()),
        industry_groups=frozenset(default_industry_groups().groups),
    )


# --------------------------------------------------------------------------- #
# Condition trees
# --------------------------------------------------------------------------- #


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


class _Checker:
    def __init__(self, context: RuleContext, params: Collection[str]) -> None:
        self.context = context
        self.params = params
        self.issues: list[RuleIssue] = []

    def add(self, code: IssueCode, path: str, message: str) -> None:
        self.issues.append(RuleIssue(code, path, message))

    def group(
        self, group: ConditionGroup, path: str, *, allow_pending: bool, depth: int = 1
    ) -> None:
        if depth > MAX_GROUP_DEPTH:
            self.add(
                "nesting_too_deep",
                path,
                f"condition groups may nest at most {MAX_GROUP_DEPTH} levels",
            )
            return
        children = group.children
        node_path = f"{path}.{group.mode}"
        if not children:
            self.add("empty_group", node_path, f"'{group.mode}' has no conditions")
        for index, child in enumerate(children):
            child_path = f"{node_path}[{index}]"
            if isinstance(child, ConditionGroup):
                self.group(child, child_path, allow_pending=allow_pending, depth=depth + 1)
            else:
                self.condition(child, child_path, allow_pending=allow_pending)

    def condition(self, condition: Condition, path: str, *, allow_pending: bool) -> None:
        factor = condition.factor
        if factor in self.context.pending_factors:
            if not allow_pending:
                self.add(
                    "pending_factor",
                    path,
                    f"{factor!r} is a P2 position factor; only holding zones may use it",
                )
        elif factor not in self.context.factor_names:
            self.add("unknown_factor", path, f"unknown factor {factor!r}")
        if condition.op not in OPS:
            self.add("invalid_op", path, f"operator {condition.op!r} is not one of {OPS}")
            return
        self.value(condition, path)

    def _scalar(self, value: object, path: str, op: str, *, allow_bool: bool) -> bool:
        """Check one scalar operand; True when it is usable as a number."""

        if isinstance(value, str):
            name = param_ref(value)
            if name is None:
                self.add(
                    "free_text_value",
                    path,
                    f"{value!r} is free text; use a number or a $param reference",
                )
                return False
            if name not in self.params:
                self.add("param_undefined", path, f"parameter {name!r} is not defined")
                return False
            return True
        if isinstance(value, bool):
            if not allow_bool:
                self.add("op_value_mismatch", path, f"'{op}' needs a number, not a boolean")
                return False
            return True
        if _is_number(value):
            return True
        self.add("op_value_mismatch", path, f"'{op}' cannot take {type(value).__name__}")
        return False

    def value(self, condition: Condition, path: str) -> None:
        op, value = condition.op, condition.value
        value_path = f"{path}.value"
        if op in COMPARISON_OPS or op in EQUALITY_OPS:
            if isinstance(value, list):
                self.add("op_value_mismatch", value_path, f"'{op}' takes one value, not a list")
                return
            self._scalar(value, value_path, op, allow_bool=op in EQUALITY_OPS)
            return
        if not isinstance(value, list):
            self.add(
                "op_value_mismatch",
                value_path,
                f"'{op}' takes a list" + (" [low, high]" if op == "between" else ""),
            )
            if isinstance(value, str) and param_ref(value) is None:
                self.add("free_text_value", value_path, f"{value!r} is free text")
            return
        if op == "between":
            if len(value) != 2:
                self.add("op_value_mismatch", value_path, "'between' takes exactly [low, high]")
                return
            usable = [
                self._scalar(item, f"{value_path}[{i}]", op, allow_bool=False)
                for i, item in enumerate(value)
            ]
            low, high = value
            if all(usable) and _is_number(low) and _is_number(high) and float(low) > float(high):
                self.add("op_value_mismatch", value_path, "'between' needs low <= high")
            return
        # op == "in"
        if not value:
            self.add("op_value_mismatch", value_path, "'in' needs at least one value")
            return
        for index, item in enumerate(value):
            self._scalar(item, f"{value_path}[{index}]", op, allow_bool=False)


# --------------------------------------------------------------------------- #
# Templates and instances
# --------------------------------------------------------------------------- #


def _zones(
    checker: _Checker,
    zones: Mapping[str, ConditionGroup],
    scope: str,
    path: str,
    *,
    require_all: bool,
) -> None:
    allowed = SCOPE_ZONES[scope]
    for name in sorted(zones):
        if name not in allowed:
            hint = " (there is no sell zone; use exit_review)" if name == "sell" else ""
            checker.add(
                "zone_not_allowed",
                f"{path}.{name}",
                f"zone {name!r} is not allowed for scope {scope!r}; allowed: {allowed}{hint}",
            )
            continue
        checker.group(zones[name], f"{path}.{name}", allow_pending=scope == "holding")
    if require_all:
        for name in allowed:
            if name not in zones:
                checker.add("zone_missing", f"{path}.{name}", f"zone {name!r} is not defined")


def check_template(template: RuleTemplate, context: RuleContext | None = None) -> list[RuleIssue]:
    ctx = context or default_context()
    checker = _Checker(ctx, template.params)
    if not template.applicable_groups:
        checker.add("empty_applicable_groups", "applicable_groups", "no applicable group")
    for index, group in enumerate(template.applicable_groups):
        if group not in ctx.industry_groups:
            checker.add(
                "unknown_industry_group",
                f"applicable_groups[{index}]",
                f"{group!r} is not an industry group ({', '.join(sorted(ctx.industry_groups))})",
            )
    for name in sorted(template.params):
        default = template.params[name]
        bounds = template.param_ranges.get(name)
        if bounds is None:
            checker.add("param_without_range", f"params.{name}", "no param_ranges entry")
            continue
        if bounds.min > bounds.max:
            checker.add("param_range_invalid", f"param_ranges.{name}", "min > max")
        elif not bounds.min <= default <= bounds.max:
            checker.add(
                "param_out_of_range",
                f"params.{name}",
                f"default {default} outside [{bounds.min}, {bounds.max}]",
            )
    for name in sorted(set(template.param_ranges) - set(template.params)):
        checker.add("param_undefined", f"param_ranges.{name}", "range for an undefined param")
    checker.group(template.screen, "screen", allow_pending=False)
    _zones(checker, template.candidate_zones, "candidate", "candidate_zones", require_all=True)
    _zones(checker, template.holding_zones, "holding", "holding_zones", require_all=True)
    return checker.issues


def check_instance(
    instance: RuleInstance, template: RuleTemplate, context: RuleContext | None = None
) -> list[RuleIssue]:
    ctx = context or default_context()
    checker = _Checker(ctx, template.params)
    if instance.template_ref != template.ref:
        checker.add(
            "template_ref_mismatch",
            "template_ref",
            f"{instance.template_ref!r} does not name template {template.ref!r}",
        )
    if parse_a_share_ticker(instance.symbol) is None:
        checker.add("invalid_symbol", "symbol", f"{instance.symbol!r} is not like 600519.SH")
    for name in sorted(instance.params):
        value = instance.params[name]
        bounds = template.param_ranges.get(name)
        if name not in template.params or bounds is None:
            checker.add("param_undefined", f"params.{name}", f"template has no param {name!r}")
        elif not bounds.min <= value <= bounds.max:
            checker.add(
                "param_out_of_range",
                f"params.{name}",
                f"{value} outside the template range [{bounds.min}, {bounds.max}]",
            )
    _zones(checker, instance.zones, instance.scope, "zones", require_all=False)
    return checker.issues


def _schema_issues(exc: ValidationError) -> list[RuleIssue]:
    return [
        RuleIssue("schema", ".".join(str(part) for part in error["loc"]) or "$", error["msg"])
        for error in exc.errors()
    ]


def parse_template(
    data: Mapping[str, Any], *, context: RuleContext | None = None, source: str | None = None
) -> RuleTemplate:
    try:
        template = RuleTemplate.model_validate(data)
    except ValidationError as exc:
        raise RuleValidationError(_schema_issues(exc), source=source) from exc
    issues = check_template(template, context)
    if issues:
        raise RuleValidationError(issues, source=source)
    return template


def parse_instance(
    data: Mapping[str, Any],
    template: RuleTemplate,
    *,
    context: RuleContext | None = None,
    source: str | None = None,
) -> RuleInstance:
    try:
        instance = RuleInstance.model_validate(data)
    except ValidationError as exc:
        raise RuleValidationError(_schema_issues(exc), source=source) from exc
    issues = check_instance(instance, template, context)
    if issues:
        raise RuleValidationError(issues, source=source)
    return instance


# --------------------------------------------------------------------------- #
# Parameter resolution
# --------------------------------------------------------------------------- #


def effective_params(
    template: RuleTemplate, instance: RuleInstance | None = None
) -> dict[str, float]:
    """Template defaults overlaid with the instance's overrides."""

    params = dict(template.params)
    if instance is not None:
        params.update(instance.params)
    return params


def resolve_scalar(value: object, params: Mapping[str, float]) -> float | bool:
    """A literal or ``$param`` operand as a number (bools stay bools)."""

    name = param_ref(value)
    if name is not None:
        try:
            return params[name]
        except KeyError:
            raise KeyError(f"undefined rule parameter {name!r}") from None
    if isinstance(value, bool):
        return value
    if _is_number(value):
        return float(value)  # type: ignore[arg-type]
    raise ValueError(f"not a resolvable rule value: {value!r}")


def resolve_value(value: object, params: Mapping[str, float]) -> float | bool | list[float | bool]:
    if isinstance(value, list):
        return [resolve_scalar(item, params) for item in value]
    return resolve_scalar(value, params)


def factors_in(group: ConditionGroup) -> set[str]:
    """Every factor a condition tree references."""

    names: set[str] = set()
    for child in group.children:
        if isinstance(child, ConditionGroup):
            names |= factors_in(child)
        else:
            names.add(child.factor)
    return names

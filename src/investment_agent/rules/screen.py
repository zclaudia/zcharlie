"""Deterministic screen engine (P1-C3, funnel design 7.2).

:func:`run_screen` is a pure function of (as_of, target-pool members with
their factor values, templates, config). It never reads the database or
calls an LLM; the ``screen_run`` job (:mod:`investment_agent.rules.screen_job`)
feeds it. Same inputs -> byte-identical :func:`screen_json`.

Evaluation of one template's ``screen`` for one member
-----------------------------------------------------

1. **Global pre-filter** (``ScreenConfig.exclude_factors``, default
   ``report_overdue`` and ``is_st``): a member whose factor value is truthy
   (1) is ``excluded`` from every template.
2. **Applicability**: a member whose industry group is not in the template's
   ``applicable_groups`` is ``skipped``.
3. **Conditions** (three-valued, every leaf is evaluated - no short circuit -
   so reasons and miss counts are complete):

   * ``ok`` / ``approx`` factor -> compare; ``approx`` is allowed but the
     reason carries the quality and flags;
   * ``missing`` factor (or absent) -> the condition does **not** hold and
     is recorded (outcome ``missing``);
   * ``not_applicable`` factor -> the condition is **skipped** (neither
     true nor false; outcome ``not_applicable``);
   * ``all`` holds when no child is false and at least one is decided;
     ``any`` holds when one child holds; a group whose children are all
     skipped is skipped. A skipped root is ``skipped``
     (``all_conditions_not_applicable``), never a hit.

Ranking and cap
---------------

Hits are ranked by **strength**: the mean, over met comparison conditions
(``< <= > >= between``), of how far the actual value lies inside the
threshold, relative to the threshold: ``(actual - t) / max(|t|, 0.01)`` for
``>``/``>=`` (mirrored for ``<``/``<=``), ``min(actual - low, high -
actual) / ((high - low) / 2)`` for ``between``; each margin is clamped to
``[0, 1]``. Equality / ``in`` conditions contribute no margin. Ties break
by ticker. The first ``hit_cap`` (default 20) hits keep outcome ``hit``;
the rest become ``truncated`` and the template result notes how many.

Reasons
-------

Each reason is ``{rule_path, factor, op, threshold, param, actual, quality,
flags, outcome, snapshot_ref}``; ``rule_path`` is like
``screen.all[2].any[0]``, ``param`` names the template parameter that set
the threshold (or ``null`` for a literal) and ``snapshot_ref`` is the
factor's ``inputs_ref`` without the member-level context (provider, as_of,
industry), which is reported once per member. Hits list the met and
not-applicable conditions; misses list the failed and missing ones.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from investment_agent.factors.registry import FactorValue
from investment_agent.rules.schema import Condition, ConditionGroup, RuleTemplate, param_ref
from investment_agent.rules.validation import effective_params, factors_in, resolve_value

Outcome = Literal["hit", "truncated", "miss", "skipped", "excluded"]
LeafOutcome = Literal["met", "not_met", "missing", "not_applicable"]

DEFAULT_HIT_CAP = 20
DEFAULT_EXCLUDE_FACTORS: tuple[str, ...] = ("report_overdue", "is_st")
MARGIN_SCALE_FLOOR = 0.01
STRENGTH_DIGITS = 6
_CONTEXT_REF_KEYS = frozenset({"provider", "as_of", "industry_group", "industry"})

SKIP_GROUP = "industry_group_not_applicable"
SKIP_ALL_NA = "all_conditions_not_applicable"


class ScreenDefinitionError(ValueError):
    """A template cannot be screened (unknown / pending factor in ``screen``)."""


@dataclass(frozen=True)
class ScreenConfig:
    hit_cap: int = DEFAULT_HIT_CAP
    exclude_factors: tuple[str, ...] = DEFAULT_EXCLUDE_FACTORS

    def __post_init__(self) -> None:
        if self.hit_cap < 1:
            raise ValueError("hit_cap must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {"hit_cap": self.hit_cap, "exclude_factors": list(self.exclude_factors)}


@dataclass(frozen=True)
class ScreenMember:
    listing_id: int
    ticker: str
    industry_group: str
    factors: Mapping[str, FactorValue]
    context: Mapping[str, Any] = field(default_factory=dict)
    """Member-level provenance (provider, as_of, industry match)."""


@dataclass(frozen=True)
class MemberResult:
    ticker: str
    listing_id: int
    outcome: Outcome
    rank: int | None
    strength: float | None
    reasons: tuple[Mapping[str, Any], ...]
    industry_group: str
    detail: str | None = None
    """Why a member was skipped / excluded."""

    @property
    def hit(self) -> bool:
        return self.outcome in {"hit", "truncated"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "listing_id": self.listing_id,
            "outcome": self.outcome,
            "rank": self.rank,
            "strength": self.strength,
            "industry_group": self.industry_group,
            "detail": self.detail,
            "reasons": [dict(reason) for reason in self.reasons],
        }


@dataclass(frozen=True)
class TemplateResult:
    template_id: str
    template_version: int
    calibration_status: str
    members: tuple[MemberResult, ...]
    """Every member, ordered: hits by rank, then the rest by ticker."""
    misses_by_condition: Mapping[str, Mapping[str, Any]]
    missing_by_factor: Mapping[str, int]

    @property
    def template_ref(self) -> str:
        return f"{self.template_id}@{self.template_version}"

    def by_outcome(self, outcome: Outcome) -> list[MemberResult]:
        return [member for member in self.members if member.outcome == outcome]

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("hit", "truncated", "miss", "skipped", "excluded"), 0)
        for member in self.members:
            counts[member.outcome] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_ref": self.template_ref,
            "calibration_status": self.calibration_status,
            "counts": self.counts(),
            "truncated": len(self.by_outcome("truncated")),
            "hits": [member.to_dict() for member in self.members if member.hit],
            "misses": {
                "count": len(self.by_outcome("miss")),
                "by_condition": {key: dict(self.misses_by_condition[key])
                                 for key in sorted(self.misses_by_condition)},
                "missing_by_factor": dict(sorted(self.missing_by_factor.items())),
            },
            "skipped": [
                {"ticker": m.ticker, "detail": m.detail}
                for m in self.members
                if m.outcome == "skipped"
            ],
        }


@dataclass(frozen=True)
class ScreenRun:
    as_of: date
    config: ScreenConfig
    member_count: int
    excluded: Mapping[str, tuple[str, ...]]
    """ticker -> pre-filter factors that excluded it."""
    member_context: Mapping[str, Mapping[str, Any]]
    templates: tuple[TemplateResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "config": self.config.to_dict(),
            "member_count": self.member_count,
            "excluded": {ticker: list(self.excluded[ticker]) for ticker in sorted(self.excluded)},
            "member_context": {
                ticker: jsonable(self.member_context[ticker])
                for ticker in sorted(self.member_context)
            },
            "templates": [template.to_dict() for template in self.templates],
        }


def screen_json(run: ScreenRun) -> str:
    """Canonical serialisation: sorted keys, no whitespace variance."""

    return json.dumps(run.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _snapshot_ref(factor: FactorValue | None) -> dict[str, Any]:
    if factor is None:
        return {}
    ref = {key: item for key, item in factor.inputs_ref.items() if key not in _CONTEXT_REF_KEYS}
    result: dict[str, Any] = jsonable(ref)
    return result


def _as_number(value: float | bool) -> float:
    return 1.0 if value is True else 0.0 if value is False else float(value)


def _compare(actual: float, op: str, threshold: Any) -> bool:
    if op == "<":
        return actual < _as_number(threshold)
    if op == "<=":
        return actual <= _as_number(threshold)
    if op == ">":
        return actual > _as_number(threshold)
    if op == ">=":
        return actual >= _as_number(threshold)
    if op == "==":
        return actual == _as_number(threshold)
    if op == "!=":
        return actual != _as_number(threshold)
    if op == "between":
        low, high = threshold
        return _as_number(low) <= actual <= _as_number(high)
    if op == "in":
        return actual in {_as_number(item) for item in threshold}
    raise ValueError(f"unsupported operator {op!r}")


def _margin(actual: float, op: str, threshold: Any) -> float | None:
    if op in {">", ">="}:
        t = _as_number(threshold)
        raw = (actual - t) / max(abs(t), MARGIN_SCALE_FLOOR)
    elif op in {"<", "<="}:
        t = _as_number(threshold)
        raw = (t - actual) / max(abs(t), MARGIN_SCALE_FLOOR)
    elif op == "between":
        low, high = (_as_number(item) for item in threshold)
        half = (high - low) / 2
        if half <= 0:
            return None
        raw = min(actual - low, high - actual) / half
    else:
        return None
    return min(1.0, max(0.0, raw))


@dataclass
class _Leaf:
    path: str
    condition: Condition
    threshold: Any
    param: str | list[str | None] | None
    factor: FactorValue | None
    outcome: LeafOutcome

    def reason(self) -> dict[str, Any]:
        factor = self.factor
        return {
            "rule_path": self.path,
            "factor": self.condition.factor,
            "op": self.condition.op,
            "threshold": self.threshold,
            "param": self.param,
            "actual": factor.value if factor is not None else None,
            "quality": factor.quality if factor is not None else "missing",
            "flags": list(factor.flags) if factor is not None else ["factor_absent"],
            "outcome": self.outcome,
            "snapshot_ref": _snapshot_ref(factor),
        }


def _param_of(value: Any) -> str | list[str | None] | None:
    if isinstance(value, list):
        names = [param_ref(item) for item in value]
        return names if any(names) else None
    return param_ref(value)


def _evaluate(
    group: ConditionGroup,
    path: str,
    member: ScreenMember,
    params: Mapping[str, float],
    leaves: list[_Leaf],
) -> bool | None:
    node_path = f"{path}.{group.mode}"
    results: list[bool | None] = []
    for index, child in enumerate(group.children):
        child_path = f"{node_path}[{index}]"
        if isinstance(child, ConditionGroup):
            results.append(_evaluate(child, child_path, member, params, leaves))
            continue
        threshold = resolve_value(child.value, params)
        factor = member.factors.get(child.factor)
        outcome: LeafOutcome
        result: bool | None
        if factor is None or factor.quality == "missing" or factor.value is None:
            if factor is not None and factor.quality == "not_applicable":
                outcome, result = "not_applicable", None
            else:
                outcome, result = "missing", False
        else:
            result = _compare(factor.value, child.op, threshold)
            outcome = "met" if result else "not_met"
        leaves.append(_Leaf(child_path, child, threshold, _param_of(child.value), factor, outcome))
        results.append(result)
    decided = [result for result in results if result is not None]
    if not decided:
        return None
    if group.mode == "all":
        return all(decided)
    return any(decided)


def _strength(leaves: Sequence[_Leaf]) -> float:
    margins = [
        margin
        for leaf in leaves
        if leaf.outcome == "met" and leaf.factor is not None and leaf.factor.value is not None
        if (margin := _margin(leaf.factor.value, leaf.condition.op, leaf.threshold)) is not None
    ]
    if not margins:
        return 0.0
    return round(sum(margins) / len(margins), STRENGTH_DIGITS)


def evaluate_zone_conditions(
    *,
    zones: Mapping[str, ConditionGroup],
    factors: Mapping[str, FactorValue],
    params: Mapping[str, float],
) -> dict[str, list[dict[str, Any]]]:
    """Evaluate a rule's zone conditions against one stock's factors (pure).

    The candidate summary uses this to show a manual nomination's draft rule
    as the same per-factor checks a screen hit gets: reasons reuse the screen
    reason shape (factor / op / threshold / actual / outcome / snapshot_ref),
    keyed by zone name in ``zones`` order.
    """

    return {
        name: [dict(reason) for reason in outcome.reasons]
        for name, outcome in evaluate_zone_outcomes(
            zones=zones, factors=factors, params=params
        ).items()
    }


@dataclass(frozen=True)
class ZoneOutcome:
    """One zone's evaluation for one stock (the daily zone detector's kernel).

    ``holds`` is the group result under the same three-valued logic the screen
    engine uses: ``True`` (holds), ``False`` (does not hold) or ``None`` when
    every condition was skipped as ``not_applicable`` and the zone is
    therefore undecided. ``met`` carries the conditions that held, with their
    actual values, in the shared reason shape.
    """

    zone: str
    holds: bool | None
    reasons: tuple[Mapping[str, Any], ...]
    met: tuple[Mapping[str, Any], ...]


def evaluate_zone_outcomes(
    *,
    zones: Mapping[str, ConditionGroup],
    factors: Mapping[str, FactorValue],
    params: Mapping[str, float],
) -> dict[str, ZoneOutcome]:
    """Per-zone group result plus every leaf reason (pure, no short circuit)."""

    member = ScreenMember(listing_id=0, ticker="", industry_group="", factors=factors)
    outcomes: dict[str, ZoneOutcome] = {}
    for name, group in zones.items():
        leaves: list[_Leaf] = []
        holds = _evaluate(group, name, member, params, leaves)
        outcomes[name] = ZoneOutcome(
            zone=name,
            holds=holds,
            reasons=tuple(leaf.reason() for leaf in leaves),
            met=tuple(leaf.reason() for leaf in leaves if leaf.outcome == "met"),
        )
    return outcomes


def _excluded_by(member: ScreenMember, factors: Sequence[str]) -> tuple[str, ...]:
    hits: list[str] = []
    for name in factors:
        value = member.factors.get(name)
        if value is not None and value.value is not None and value.value != 0:
            hits.append(name)
    return tuple(hits)


def required_factors(templates: Sequence[RuleTemplate], config: ScreenConfig) -> set[str]:
    names: set[str] = set(config.exclude_factors)
    for template in templates:
        names |= factors_in(template.screen)
    return names


def run_screen(
    *,
    as_of: date,
    members: Sequence[ScreenMember],
    templates: Sequence[RuleTemplate],
    config: ScreenConfig | None = None,
    known_factors: Collection[str] | None = None,
) -> ScreenRun:
    """Screen ``members`` with every template's ``screen`` conditions (pure)."""

    cfg = config or ScreenConfig()
    if known_factors is None:
        from investment_agent.factors.catalog import DEFAULT_REGISTRY

        known_factors = frozenset(DEFAULT_REGISTRY.names())
    ordered_templates = sorted(templates, key=lambda item: (item.id, item.version))
    for template in ordered_templates:
        unknown = sorted(factors_in(template.screen) - set(known_factors))
        if unknown:
            raise ScreenDefinitionError(
                f"{template.ref}: screen references factors outside the registry "
                f"(pending or unknown): {unknown}"
            )
    ordered = sorted(members, key=lambda member: member.ticker)
    tickers = [member.ticker for member in ordered]
    if len(set(tickers)) != len(tickers):
        raise ValueError("duplicate tickers among screen members")
    excluded = {
        member.ticker: reasons
        for member in ordered
        if (reasons := _excluded_by(member, cfg.exclude_factors))
    }
    results = tuple(
        _screen_template(template, ordered, excluded, cfg) for template in ordered_templates
    )
    return ScreenRun(
        as_of=as_of,
        config=cfg,
        member_count=len(ordered),
        excluded=excluded,
        member_context={member.ticker: member.context for member in ordered},
        templates=results,
    )


def _screen_template(
    template: RuleTemplate,
    members: Sequence[ScreenMember],
    excluded: Mapping[str, tuple[str, ...]],
    config: ScreenConfig,
) -> TemplateResult:
    params = effective_params(template)
    groups = set(template.applicable_groups)
    hits: list[tuple[float, ScreenMember, list[_Leaf]]] = []
    others: list[MemberResult] = []
    misses: dict[str, dict[str, Any]] = {}
    missing_by_factor: dict[str, int] = {}
    for member in members:
        if member.ticker in excluded:
            others.append(
                MemberResult(
                    member.ticker,
                    member.listing_id,
                    "excluded",
                    None,
                    None,
                    (),
                    member.industry_group,
                    "prefilter:" + ",".join(excluded[member.ticker]),
                )
            )
            continue
        if member.industry_group not in groups:
            others.append(
                MemberResult(
                    member.ticker,
                    member.listing_id,
                    "skipped",
                    None,
                    None,
                    (),
                    member.industry_group,
                    SKIP_GROUP,
                )
            )
            continue
        leaves: list[_Leaf] = []
        result = _evaluate(template.screen, "screen", member, params, leaves)
        for leaf in leaves:
            if leaf.outcome == "missing":
                name = leaf.condition.factor
                missing_by_factor[name] = missing_by_factor.get(name, 0) + 1
        if result is None:
            others.append(
                MemberResult(
                    member.ticker,
                    member.listing_id,
                    "skipped",
                    None,
                    None,
                    tuple(leaf.reason() for leaf in leaves),
                    member.industry_group,
                    SKIP_ALL_NA,
                )
            )
            continue
        if result:
            hits.append((_strength(leaves), member, leaves))
            continue
        failing = [leaf for leaf in leaves if leaf.outcome in {"not_met", "missing"}]
        for leaf in failing:
            entry = misses.setdefault(
                leaf.path,
                {
                    "factor": leaf.condition.factor,
                    "op": leaf.condition.op,
                    "threshold": leaf.threshold,
                    "param": leaf.param,
                    "not_met": 0,
                    "missing": 0,
                },
            )
            entry[leaf.outcome] += 1
        others.append(
            MemberResult(
                member.ticker,
                member.listing_id,
                "miss",
                None,
                None,
                tuple(leaf.reason() for leaf in failing),
                member.industry_group,
            )
        )
    hits.sort(key=lambda item: (-item[0], item[1].ticker))
    ranked = tuple(
        MemberResult(
            member.ticker,
            member.listing_id,
            "hit" if rank <= config.hit_cap else "truncated",
            rank,
            strength,
            tuple(
                leaf.reason() for leaf in leaves if leaf.outcome in {"met", "not_applicable"}
            ),
            member.industry_group,
        )
        for rank, (strength, member, leaves) in enumerate(hits, start=1)
    )
    return TemplateResult(
        template_id=template.id,
        template_version=template.version,
        calibration_status=template.calibration_status,
        members=ranked + tuple(sorted(others, key=lambda item: item.ticker)),
        misses_by_condition=misses,
        missing_by_factor=missing_by_factor,
    )

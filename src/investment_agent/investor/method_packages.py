"""Method packages (schema v1): versioned research-step contracts.

A package is **data**: structured steps with typed output requirements, a
canonical content hash and aggregate validation. It answers "what steps and
requirements does this research method have"; approval, shadow runs and head
movement stay in the existing method lifecycle. Packages can never grant
tools, change formulas or relax source, budget or publication rules - the
tool-family, calculation-method and snapshot-field registries below are
code-owned and packages only reference them.

The contract layer is a pure library (no storage); its only IO is reading the
YAML authoring files (``config/methods/*.yaml``) via :func:`load_package_file`
and :func:`load_packages`. The canonical form follows ``rules/schema.py``:
JSON-compatible dict, sorted keys, compact separators, SHA-256 over the content. ``description`` is part of the content (review text
for users only, never rendered into model instructions); lifecycle fields
(artifact id, version, created_at, source, proposal, evidence hash) are
excluded, so re-importing identical content deduplicates to the same hash.

The virtual legacy baseline (:data:`LEGACY_BASELINE_DEFINITION`) restates what
``ResearchProgress.depth`` (``agent/research.py``) already requires of
``company_research`` / ``review`` tasks - one to one, nothing added.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args
from uuid import UUID

import yaml
from pydantic import Field, ValidationError

from investment_agent.investor.common import Contract, Text
from investment_agent.investor.contracts import DIMENSIONS, Dimension

if TYPE_CHECKING:
    from investment_agent.investor.methods import MethodConfig

PackageKind = Literal["company_research", "review"]
"""Research kinds a package may apply to; first version restricts to these two."""

# --------------------------------------------------------------------------- #
# Code-owned registries (packages reference, never extend)
# --------------------------------------------------------------------------- #

ToolFamily = Literal[
    "snapshot_read", "evidence", "valuation", "peer", "disclosure", "document", "portfolio"
]
TOOL_FAMILY_MEMBERS: dict[ToolFamily, tuple[str, ...]] = {
    "snapshot_read": ("get_snapshot", "capture_snapshot"),
    "evidence": ("get_evidence", "get_event", "search_saved_sources", "compare_saved_sources", "compare_stocks"),
    "valuation": ("historical_valuation_percentile", "reverse_dcf_fcfe"),
    "peer": ("get_industry_peer_context",),
    "disclosure": ("discover_disclosures", "fetch_disclosure_original"),
    "document": ("parse_disclosure_original", "read_disclosure_original"),
    "portfolio": ("get_portfolio_context", "calculate_portfolio_options", "prepare_portfolio_plan"),
}
"""Static family -> registered tool names (``agent/research.py registrations()``)."""


def resolve_family_members(families: Iterable[ToolFamily]) -> tuple[str, ...]:
    """Expand capability families into registered tool names, first occurrence wins."""

    resolved: list[str] = []
    for family in families:
        for name in TOOL_FAMILY_MEMBERS[family]:
            if name not in resolved:
                resolved.append(name)
    return tuple(resolved)


CalculationMethod = Literal["historical_valuation_percentile", "reverse_dcf_fcfe", "roe_dupont"]

# ``agent/builtin_tools.py SnapshotParameters`` derives its field enum from
# ``default_field_sources`` (financial/market tables); that registry is a
# superset - the direct research fields below are the ones packages reference.
# tests/unit/test_method_packages.py asserts the subset stays valid.
SnapshotField = Literal[
    "close",
    "roe",
    "operating_cash_flow",
    "capital_expenditure",
    "net_profit_parent",
    "net_profit_total",
    "revenue_main",
    "revenue_total",
    "total_assets",
]
SNAPSHOT_FIELDS: tuple[SnapshotField, ...] = (
    "close",
    "roe",
    "operating_cash_flow",
    "capital_expenditure",
    "net_profit_parent",
    "net_profit_total",
    "revenue_main",
    "revenue_total",
    "total_assets",
)

# --------------------------------------------------------------------------- #
# Output requirements: tagged union, no parameterless variants
# --------------------------------------------------------------------------- #

MinimumDimensionStatus = Literal["missing", "qualitative", "evidenced"]


class PlanRequirement(Contract):
    """Reuse of the full research-plan checks."""

    schema_version: Literal[1] = 1
    type: Literal["plan"]
    require_completion_criteria: bool
    require_stop_conditions: bool


class HypothesesRequirement(Contract):
    schema_version: Literal[1] = 1
    type: Literal["hypotheses"]
    min_count: Annotated[int, Field(ge=1)]


class CounterRefsRequirement(Contract):
    schema_version: Literal[1] = 1
    type: Literal["counter_refs"]
    min_count: Annotated[int, Field(ge=1)]
    verified_only: bool = False


class DimensionOutputRequirement(Contract):
    schema_version: Literal[1] = 1
    type: Literal["dimension"]
    dimension: Dimension
    minimum_status: MinimumDimensionStatus = "missing"


class CalculationRequirement(Contract):
    schema_version: Literal[1] = 1
    type: Literal["calculation"]
    method: CalculationMethod


class SnapshotFieldRequirement(Contract):
    schema_version: Literal[1] = 1
    type: Literal["snapshot_field"]
    field: SnapshotField


class PeerContextRequirement(Contract):
    schema_version: Literal[1] = 1
    type: Literal["peer_context"]
    require_calculation: bool = False


OutputRequirement = Annotated[
    PlanRequirement
    | HypothesesRequirement
    | CounterRefsRequirement
    | DimensionOutputRequirement
    | CalculationRequirement
    | SnapshotFieldRequirement
    | PeerContextRequirement,
    Field(discriminator="type"),
]


class MethodStep(Contract):
    """One reviewable research step; ``id`` keys plan mapping and settlement."""

    schema_version: Literal[1] = 1
    id: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]
    title: Annotated[str, Field(min_length=1, max_length=200)]  # review UI only
    instruction_kind: Literal[
        "falsifiable_plan",
        "counter_analysis",
        "dimension_assessment",
        "snapshot_capture",
        "calculation",
        "peer_context",
        "expectations_gap",
    ]
    blocking: bool
    depends_on: tuple[str, ...] = ()
    required_capabilities: tuple[ToolFamily, ...] = ()
    required_outputs: tuple[OutputRequirement, ...] = ()
    allow_missing: bool = False


# --------------------------------------------------------------------------- #
# Package definition and stored artifact payload
# --------------------------------------------------------------------------- #


class DimensionRequirement(Contract):
    schema_version: Literal[1] = 1
    dimension: Dimension
    minimum_status: MinimumDimensionStatus = "missing"
    allow_not_applicable: bool = True
    allow_missing: bool = False


def default_dimensions() -> tuple[DimensionRequirement, ...]:
    """Current-behavior default: one entry per dimension, no new minimum status."""

    return tuple(DimensionRequirement(dimension=dimension) for dimension in DIMENSIONS)


class MethodPackageDefinition(Contract):
    """What YAML authors and package proposals submit; content only."""

    schema_version: Literal[1] = 1
    package_key: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")]
    applicable_kinds: tuple[PackageKind, ...] = ()
    steps: tuple[MethodStep, ...]
    dimensions: tuple[DimensionRequirement, ...] | None = None  # None: current defaults
    # ``investor.methods`` imports this module for the proposal contract (§7),
    # so a module-level MethodConfig import would cycle; the name binds right
    # below this class and the factory defers to call time, which never happens
    # before that import (the legacy baseline is built afterwards).
    discipline: "MethodConfig" = Field(default_factory=lambda: MethodConfig())
    description: Annotated[str, Field(max_length=20_000)] = ""


# ``investor.methods`` needs this name for ``CreateProposal.package`` while this
# module needs ``MethodConfig`` for ``discipline``/``legacy_package_for``; the
# import resolves after both sides' contract classes exist, so either import
# order (methods-first, packages-first) converges. The rebuild makes the
# deferred ``discipline`` annotation concrete before any instance is built.
from investment_agent.investor.methods import MethodConfig  # noqa: E402

MethodPackageDefinition.model_rebuild()


LIFECYCLE_FIELDS: frozenset[str] = frozenset(
    {"artifact_id", "version", "content_hash", "created_at", "source", "proposal_id", "evidence_hash"}
)
"""Artifact-only fields; ``content_hash`` stores this hash, so it cannot be part of it."""


class MethodPackage(MethodPackageDefinition):
    """Stored artifact payload: definition content plus lifecycle fields."""

    artifact_id: UUID
    version: Annotated[int, Field(ge=1)]
    content_hash: str
    created_at: datetime
    source: Text
    proposal_id: UUID | None = None
    evidence_hash: Text | None = None


# --------------------------------------------------------------------------- #
# Canonical form (rules/schema.py pattern)
# --------------------------------------------------------------------------- #


def canonical_definition(
    definition: MethodPackageDefinition | Mapping[str, Any],
) -> dict[str, Any]:
    """JSON-compatible canonical dict of the definition content.

    Mapping input (e.g. ``yaml.safe_load`` output) is normalized through the
    contract first, so missing fields take their defaults and ``dimensions:
    None`` expands to the seven current-behavior entries. Lifecycle fields are
    dropped. Invalid mappings raise the raw pydantic error; use
    :func:`parse_definition` for aggregated messages.
    """

    model = (
        definition
        if isinstance(definition, MethodPackageDefinition)
        else MethodPackageDefinition.model_validate(dict(definition))
    )
    data = model.model_dump(mode="json")
    data = {key: value for key, value in data.items() if key not in LIFECYCLE_FIELDS}
    if data.get("dimensions") is None:
        data["dimensions"] = [
            requirement.model_dump(mode="json") for requirement in default_dimensions()
        ]
    return data


def canonical_json(definition: MethodPackageDefinition | Mapping[str, Any]) -> str:
    return json.dumps(
        canonical_definition(definition),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_hash(definition: MethodPackageDefinition | Mapping[str, Any]) -> str:
    """SHA-256 of the definition content (lifecycle fields excluded)."""

    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Aggregate validation
# --------------------------------------------------------------------------- #


class MethodPackageError(ValueError):
    """Every schema and aggregate problem of one package, reported together."""

    def __init__(self, errors: Iterable[str], *, source: str | None = None) -> None:
        self.errors = tuple(errors)
        self.source = source
        head = f"{source}: " if source else ""
        super().__init__(head + f"{len(self.errors)} method package issue(s): " + "; ".join(self.errors))


def validate_definition(definition: MethodPackageDefinition) -> list[str]:
    """Collect every cross-step problem at once; an empty list means valid."""

    errors: list[str] = []
    if not definition.applicable_kinds:
        errors.append("applicable_kinds: at least one applicable research kind is required")
    elif len(set(definition.applicable_kinds)) != len(definition.applicable_kinds):
        errors.append("applicable_kinds: duplicate research kinds are not allowed")
    if not definition.steps:
        errors.append("steps: at least one step is required")
    if definition.steps and not any(step.blocking for step in definition.steps):
        errors.append("steps: at least one blocking step is required")
    ids = [step.id for step in definition.steps]
    for step_id in sorted({value for value in ids if ids.count(value) > 1}):
        errors.append(f"steps: duplicate step id {step_id!r}")
    known = set(ids)
    for index, step in enumerate(definition.steps):
        for position, dependency in enumerate(step.depends_on):
            path = f"steps[{index}].depends_on[{position}]"
            if dependency == step.id:
                errors.append(f"{path}: step {step.id!r} depends on itself")
            elif dependency not in known:
                errors.append(f"{path}: unknown step id {dependency!r}")
    cycle = _cycle_members(definition.steps)
    if cycle:
        errors.append(f"steps: dependency cycle involving {', '.join(cycle)}")
    if definition.dimensions is not None:
        names = [requirement.dimension for requirement in definition.dimensions]
        missing = [name for name in DIMENSIONS if name not in names]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if missing:
            errors.append(f"dimensions: no requirement for {', '.join(missing)}")
        if repeated:
            errors.append(f"dimensions: duplicate requirement for {', '.join(repeated)}")
    return errors


def _cycle_members(steps: tuple[MethodStep, ...]) -> list[str]:
    """Step ids left over by Kahn's topological sort; empty when dependency-free."""

    ids = list(dict.fromkeys(step.id for step in steps))
    dependents: dict[str, list[str]] = {step_id: [] for step_id in ids}
    pending = {step_id: 0 for step_id in ids}
    for step in steps:
        for dependency in dict.fromkeys(step.depends_on):
            if dependency == step.id or dependency not in pending:
                continue  # self and unknown references are reported separately
            pending[step.id] += 1
            dependents[dependency].append(step.id)
    ready = [step_id for step_id in ids if pending[step_id] == 0]
    while ready:
        current = ready.pop()
        for follower in dependents[current]:
            pending[follower] -= 1
            if pending[follower] == 0:
                ready.append(follower)
    return sorted(step_id for step_id in ids if pending[step_id] > 0)


def _schema_errors(exc: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in error['loc']) or '$'}: {error['msg']}"
        for error in exc.errors()
    ]


def parse_definition(
    data: Mapping[str, Any], *, source: str | None = None
) -> MethodPackageDefinition:
    """Validate a mapping (``yaml.safe_load`` output) into a definition.

    Schema, enum and aggregate problems are all reported in one
    :class:`MethodPackageError`, each message carrying its field path.
    """

    try:
        definition = MethodPackageDefinition.model_validate(dict(data))
    except ValidationError as exc:
        raise MethodPackageError(_schema_errors(exc), source=source) from exc
    errors = validate_definition(definition)
    if errors:
        raise MethodPackageError(errors, source=source)
    return definition


# --------------------------------------------------------------------------- #
# YAML authoring files (config/methods/*.yaml)
# --------------------------------------------------------------------------- #


DEFAULT_METHODS_DIR = Path("config/methods")
"""Package authoring directory, resolved against the working directory."""


def load_package_file(path: Path) -> MethodPackageDefinition:
    """One package file: YAML -> :func:`parse_definition` -> validation.

    Schema, enum, dependency and reference problems are all raised together
    as one :class:`MethodPackageError` carrying ``source=str(path)``.
    """

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise MethodPackageError(
            ["$: a method package file must contain a mapping"], source=str(path)
        )
    return parse_definition(raw, source=str(path))


def load_packages(directory: Path = DEFAULT_METHODS_DIR) -> tuple[MethodPackageDefinition, ...]:
    """Every ``*.yaml`` package in ``directory``, ordered by file name."""

    files = sorted(directory.glob("*.yaml"), key=lambda path: path.name)
    return tuple(load_package_file(path) for path in files if path.is_file())


# --------------------------------------------------------------------------- #
# Virtual legacy baseline (design §8): current v1 requirements, restated
# --------------------------------------------------------------------------- #

LEGACY_BASELINE_DEFINITION = MethodPackageDefinition(
    package_key="company-research",
    applicable_kinds=("company_research", "review"),
    steps=(
        MethodStep(
            id="falsifiable_plan",
            title="Falsifiable research plan",
            instruction_kind="falsifiable_plan",
            blocking=True,
            required_outputs=(
                PlanRequirement(type="plan", require_completion_criteria=True, require_stop_conditions=True),
                HypothesesRequirement(type="hypotheses", min_count=1),
            ),
        ),
        MethodStep(
            id="counter_analysis",
            title="Counter-evidence analysis",
            instruction_kind="counter_analysis",
            blocking=False,
        ),
        MethodStep(
            id="dimension_assessment",
            title="Seven-dimension assessment",
            instruction_kind="dimension_assessment",
            blocking=True,
            required_outputs=tuple(
                DimensionOutputRequirement(type="dimension", dimension=dimension)
                for dimension in DIMENSIONS
            ),
        ),
    ),
)
# 与 ResearchProgress.depth(agent/research.py)对 company_research / review 的现行要求
# 一一对应,不多不少:
# - depth 要求 hypotheses / evidence_steps / completion_criteria / stop_conditions 非空
#   → blocking 的 falsifiable_plan:plan 输出绑定 completion_criteria 与 stop_conditions,
#     blocking 覆盖要求至少一个 evidence step,hypotheses 输出要求至少一个反向假设;
# - depth 要求七维恰好各一次 → blocking 的 dimension_assessment 逐维 required_output,
#   minimum_status="missing" 不新增状态要求,dimensions=None 用现状兼容默认值
#   (allow_not_applicable=True、allow_missing=False,即不放松也不加强现有规则);
# - counter_analysis 非 blocking、无输出:反证数量与已核要求在 v1 仅由 MethodConfig
#   三个加强开关表达,discipline=MethodConfig() 全关,即现状,无额外要求。


# --------------------------------------------------------------------------- #
# Strengthening partial order (design §8)
# --------------------------------------------------------------------------- #

_MINIMUM_STATUS_RANK: dict[str, int] = {
    status: rank for rank, status in enumerate(get_args(MinimumDimensionStatus))
}
_SINGLETON_OUTPUT_TYPES: frozenset[str] = frozenset({"plan", "hypotheses", "counter_refs", "peer_context"})


def _discipline_flags() -> tuple[str, ...]:
    """The strengthening switches in declaration order (v1's only mutable knobs)."""

    return tuple(name for name in MethodConfig.model_fields if name != "schema_version")


def _dimension_requirements(
    definition: MethodPackageDefinition,
) -> dict[str, DimensionRequirement]:
    """Package-level dimension requirements; ``None`` means the current defaults."""

    if definition.dimensions is None:
        return {requirement.dimension: requirement for requirement in default_dimensions()}
    return {requirement.dimension: requirement for requirement in definition.dimensions}


def _outputs_by_type(outputs: tuple[OutputRequirement, ...]) -> dict[str, list[OutputRequirement]]:
    grouped: dict[str, list[OutputRequirement]] = {}
    for output in outputs:
        grouped.setdefault(output.type, []).append(output)
    return grouped


def _output_key(output: OutputRequirement) -> str:
    """Per-entry identity of the keyed output types (dimension / method / field)."""

    if isinstance(output, DimensionOutputRequirement):
        return output.dimension
    if isinstance(output, CalculationRequirement):
        return output.method
    if isinstance(output, SnapshotFieldRequirement):
        return output.field
    raise AssertionError(f"{output.type} outputs are not keyed")


def _compare_param(
    step_id: str,
    output_type: str,
    param: str,
    was: bool,
    now: bool,
    violations: list[str],
    stronger: list[str],
) -> None:
    if now and not was:
        stronger.append(f"outputs:{step_id}:{output_type}:{param}")
    elif was and not now:
        violations.append(
            f"steps.{step_id}.required_outputs.{output_type}.{param}: relaxed from true to false"
        )


def _compare_min_count(
    step_id: str,
    output_type: str,
    was: int,
    now: int,
    violations: list[str],
    stronger: list[str],
) -> None:
    if now > was:
        stronger.append(f"outputs:{step_id}:{output_type}:min_count")
    elif now < was:
        violations.append(
            f"steps.{step_id}.required_outputs.{output_type}.min_count: lowered from {was} to {now}"
        )


def _compare_singleton_outputs(
    step_id: str,
    old_entry: OutputRequirement,
    new_entry: OutputRequirement,
    violations: list[str],
    stronger: list[str],
) -> None:
    """Parameter-level comparison of the at-most-one-per-step output types."""

    if isinstance(old_entry, PlanRequirement) and isinstance(new_entry, PlanRequirement):
        _compare_param(
            step_id,
            "plan",
            "require_completion_criteria",
            old_entry.require_completion_criteria,
            new_entry.require_completion_criteria,
            violations,
            stronger,
        )
        _compare_param(
            step_id,
            "plan",
            "require_stop_conditions",
            old_entry.require_stop_conditions,
            new_entry.require_stop_conditions,
            violations,
            stronger,
        )
    elif isinstance(old_entry, HypothesesRequirement) and isinstance(new_entry, HypothesesRequirement):
        _compare_min_count(step_id, "hypotheses", old_entry.min_count, new_entry.min_count, violations, stronger)
    elif isinstance(old_entry, CounterRefsRequirement) and isinstance(new_entry, CounterRefsRequirement):
        _compare_min_count(step_id, "counter_refs", old_entry.min_count, new_entry.min_count, violations, stronger)
        _compare_param(
            step_id,
            "counter_refs",
            "verified_only",
            old_entry.verified_only,
            new_entry.verified_only,
            violations,
            stronger,
        )
    elif isinstance(old_entry, PeerContextRequirement) and isinstance(new_entry, PeerContextRequirement):
        _compare_param(
            step_id,
            "peer_context",
            "require_calculation",
            old_entry.require_calculation,
            new_entry.require_calculation,
            violations,
            stronger,
        )


def _compare_dimension_outputs(
    step_id: str,
    old_group: list[OutputRequirement],
    new_group: list[OutputRequirement],
    violations: list[str],
    stronger: list[str],
) -> None:
    """Per-dimension presence and minimum-status comparison of one step's outputs."""

    new_by_dimension: dict[str, DimensionOutputRequirement] = {}
    for output in new_group:
        assert isinstance(output, DimensionOutputRequirement)
        new_by_dimension[output.dimension] = output
    for output in old_group:
        assert isinstance(output, DimensionOutputRequirement)
        successor = new_by_dimension.get(output.dimension)
        if successor is None:
            violations.append(
                f"steps.{step_id}.required_outputs.dimension.{output.dimension}: required output removed"
            )
        elif _MINIMUM_STATUS_RANK[successor.minimum_status] < _MINIMUM_STATUS_RANK[output.minimum_status]:
            violations.append(
                f"steps.{step_id}.required_outputs.dimension.{output.dimension}.minimum_status:"
                f" lowered from {output.minimum_status!r} to {successor.minimum_status!r}"
            )
        elif _MINIMUM_STATUS_RANK[successor.minimum_status] > _MINIMUM_STATUS_RANK[output.minimum_status]:
            stronger.append(f"dimension_min:{output.dimension}")


def _partial_order(
    old: MethodPackageDefinition, new: MethodPackageDefinition
) -> tuple[list[str], list[str]]:
    """Walk both definitions once, collecting violations and strict enhancements.

    Mirrors the design §8 table: a change is either a violation (relaxation or
    structural break) or a strict research-requirement enhancement; presentation
    fields (``description``, step ``title``) and lifecycle/version fields are
    neither. Returns ``(violations, stronger)`` in traversal order.
    """

    violations: list[str] = []
    stronger: list[str] = []

    # applicable_kinds: the new package must cover the old one's research kinds.
    removed_kinds = [kind for kind in old.applicable_kinds if kind not in new.applicable_kinds]
    if removed_kinds:
        violations.append(f"applicable_kinds: removed kind(s) {', '.join(removed_kinds)}")
    for kind in new.applicable_kinds:
        if kind not in old.applicable_kinds:
            stronger.append(f"kinds:{kind}")

    # discipline: the per-task switches may only move false -> true.
    for flag in _discipline_flags():
        was = getattr(old.discipline, flag)
        now = getattr(new.discipline, flag)
        if now and not was:
            stronger.append(f"discipline:{flag}")
        elif was and not now:
            violations.append(f"discipline.{flag}: switch cannot be turned off (was true)")

    old_steps = {step.id: step for step in old.steps}
    new_steps = {step.id: step for step in new.steps}
    for step in old.steps:
        successor = new_steps.get(step.id)
        if successor is None:
            violations.append(f"steps.{step.id}: step removed or renamed")
            continue
        if successor.instruction_kind != step.instruction_kind:
            violations.append(
                f"steps.{step.id}.instruction_kind: fixed semantics changed from"
                f" {step.instruction_kind!r} to {successor.instruction_kind!r}"
            )
        if step.blocking and not successor.blocking:
            violations.append(f"steps.{step.id}.blocking: blocking step cannot become optional")
        elif successor.blocking and not step.blocking:
            stronger.append(f"blocking:{step.id}")
        if successor.allow_missing and not step.allow_missing:
            violations.append(
                f"steps.{step.id}.allow_missing: gap allowance cannot be introduced (was false)"
            )
        elif step.allow_missing and not successor.allow_missing:
            stronger.append(f"allow_missing:{step.id}")
        removed_dependencies = [
            dependency for dependency in dict.fromkeys(step.depends_on) if dependency not in successor.depends_on
        ]
        if removed_dependencies:
            violations.append(
                f"steps.{step.id}.depends_on: removed dependency {', '.join(removed_dependencies)}"
            )
        for dependency in dict.fromkeys(successor.depends_on):
            if dependency not in step.depends_on:
                stronger.append(f"depends_on:{step.id}:{dependency}")
        if set(step.required_capabilities) != set(successor.required_capabilities):
            violations.append(
                f"steps.{step.id}.required_capabilities: applicability precondition must stay unchanged"
                f" (was {', '.join(step.required_capabilities) or 'none'},"
                f" now {', '.join(successor.required_capabilities) or 'none'})"
            )
        old_grouped = _outputs_by_type(step.required_outputs)
        new_grouped = _outputs_by_type(successor.required_outputs)
        for output_type, old_group in old_grouped.items():
            new_group = new_grouped.get(output_type, [])
            if output_type in _SINGLETON_OUTPUT_TYPES:
                if not new_group:
                    violations.append(f"steps.{step.id}.required_outputs.{output_type}: required output removed")
                    continue
                _compare_singleton_outputs(step.id, old_group[0], new_group[0], violations, stronger)
            elif output_type == "dimension":
                _compare_dimension_outputs(step.id, old_group, new_group, violations, stronger)
            else:  # calculation / snapshot_field: per-key presence checks only
                new_keys = {_output_key(output) for output in new_group}
                for output in old_group:
                    key = _output_key(output)
                    if key not in new_keys:
                        violations.append(
                            f"steps.{step.id}.required_outputs.{output_type}.{key}:"
                            " required output removed"
                        )
        for output_type, new_group in new_grouped.items():
            old_group = old_grouped.get(output_type, [])
            if output_type in _SINGLETON_OUTPUT_TYPES:
                if not old_group:  # a wholly new singleton requirement on the step
                    stronger.append(f"outputs:{step.id}:{output_type}")
                continue
            old_keys = {_output_key(output) for output in old_group}
            for output in new_group:
                key = _output_key(output)
                if key in old_keys:
                    continue  # present in both: handled by the comparison above
                stronger.append(f"outputs:{step.id}:{output_type}:{key}")
                if (
                    isinstance(output, DimensionOutputRequirement)
                    and _MINIMUM_STATUS_RANK[output.minimum_status] > 0
                ):
                    stronger.append(f"dimension_min:{key}")

    # New steps strengthen the contract by blocking or demanding outputs; their
    # capability preconditions go through admission and shadow validation (§8).
    for step in new.steps:
        if step.id in old_steps:
            continue
        if step.blocking:
            stronger.append(f"blocking:{step.id}")
        for output_type, group in _outputs_by_type(step.required_outputs).items():
            if output_type in _SINGLETON_OUTPUT_TYPES:
                stronger.append(f"outputs:{step.id}:{output_type}")
                continue
            for output in group:
                key = _output_key(output)
                stronger.append(f"outputs:{step.id}:{output_type}:{key}")
                if (
                    isinstance(output, DimensionOutputRequirement)
                    and _MINIMUM_STATUS_RANK[output.minimum_status] > 0
                ):
                    stronger.append(f"dimension_min:{key}")

    # Package-level dimension requirements (design §8 gap row).
    old_dimensions = _dimension_requirements(old)
    new_dimensions = _dimension_requirements(new)
    for dimension in DIMENSIONS:
        was = old_dimensions.get(dimension)
        now = new_dimensions.get(dimension)
        if was is None or now is None:
            continue  # completeness of the seven entries is validate_definition's job
        was_rank = _MINIMUM_STATUS_RANK[was.minimum_status]
        now_rank = _MINIMUM_STATUS_RANK[now.minimum_status]
        if now_rank < was_rank:
            violations.append(
                f"dimensions.{dimension}.minimum_status: lowered from"
                f" {was.minimum_status!r} to {now.minimum_status!r}"
            )
        elif now_rank > was_rank:
            stronger.append(f"dimension_min:{dimension}")
        if now.allow_missing and not was.allow_missing:
            violations.append(f"dimensions.{dimension}.allow_missing: relaxed from false to true")
        elif was.allow_missing and not now.allow_missing:
            stronger.append(f"dimension_allow_missing:{dimension}")
        if now.allow_not_applicable and not was.allow_not_applicable:
            violations.append(f"dimensions.{dimension}.allow_not_applicable: relaxed from false to true")
        elif was.allow_not_applicable and not now.allow_not_applicable:
            stronger.append(f"dimension_allow_not_applicable:{dimension}")

    # The new dependency graph must stay a DAG (design §8 dependency row).
    cycle = _cycle_members(new.steps)
    if cycle:
        violations.append(f"steps: dependency cycle involving {', '.join(cycle)}")

    return violations, stronger


def strengthening_violations(
    old: MethodPackageDefinition, new: MethodPackageDefinition
) -> list[str]:
    """Every §8 strengthening-path violation of ``new`` against ``old``, in one pass.

    Violations are relaxations or structural breaks: shrunk applicable kinds,
    weakened discipline switches or outputs, removed or renamed steps, changed
    fixed semantics or capability preconditions, introduced gap allowances,
    lowered minimum statuses and dependency cycles. Each message carries its
    field path. An empty list only means nothing was relaxed - the candidate may
    still be a no-op, which the caller rejects via
    :func:`strictly_stronger_requirements`.
    """

    return _partial_order(old, new)[0]


def strictly_stronger_requirements(
    old: MethodPackageDefinition, new: MethodPackageDefinition
) -> tuple[str, ...]:
    """Stable ids of the research requirements ``new`` tightens over ``old``.

    One id per strict enhancement, sorted and deduplicated: ``blocking:<step_id>``,
    ``outputs:<step_id>:<type>[:param-or-key]``, ``depends_on:<step_id>:<dependency>``,
    ``dimension_min:<dimension>``, ``dimension_allow_missing:<dimension>``,
    ``dimension_allow_not_applicable:<dimension>``, ``allow_missing:<step_id>``,
    ``discipline:<flag>`` and ``kinds:<kind>``. Version, hash and presentation
    changes (``description``, step ``title``) strengthen nothing, so an empty
    tuple means the proposal has no substantive enhancement.
    """

    return tuple(sorted(set(_partial_order(old, new)[1])))


def legacy_package_for(binding_config: MethodConfig) -> MethodPackageDefinition:
    """Virtual v1 comparison baseline (design §8) for old-method proposals.

    The shared legacy steps with the binding's discipline switches: v1's three
    switches are its only mutable research knobs, so new v2 metadata itself is
    never a research enhancement.
    """

    return LEGACY_BASELINE_DEFINITION.model_copy(update={"discipline": binding_config})


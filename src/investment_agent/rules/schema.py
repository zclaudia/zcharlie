"""Rule schema (P1-C1): templates, instances and their condition trees.

Rules are declarative data (funnel design 5.4): a condition is
``{factor, op, value}``, conditions combine with ``all`` / ``any`` groups
(at most two levels deep), and zones are fixed enums per scope - candidate
``buy_zone`` / ``drop``, holding ``add`` / ``trim`` / ``exit_review``.
There is no ``sell`` zone; a falsification condition triggers a review.

**Parameters.** A condition value may be ``"$name"``: a reference to a
template parameter. Templates declare ``params`` (defaults) and
``param_ranges`` (``{min, max}``); an instance may only override parameter
numbers within those ranges, so an LLM draft cannot loosen a rule beyond
what the template allows.

These models check the *shape* only (types, unknown keys). Semantic checks -
factor existence, op/value agreement, parameter ranges, zone names, industry
groups - live in :mod:`investment_agent.rules.validation`, which aggregates
every problem into one typed error.

**Authoring metadata (template format v3).** Templates also carry what used
to live in YAML comments, so the UI and the drafting agent can read it:
strategy notes (``thesis``, ``fits``, ``avoids``, ``risks``,
``falsification``, ``horizon``, ``examples``), per-condition ``label`` /
``why`` / ``data_note``, and per-parameter ``label`` / ``help``
(``param_meta``). A template file may write a parameter as
``{default, range: [min, max], label, help}``; it is split into ``params`` /
``param_ranges`` / ``param_meta`` on load, so the stored canonical form keeps
the two flat maps every consumer reads. Unset metadata is ``None`` and is
dropped from the canonical form, so a template without it hashes as before.

**Canonical form.** :func:`canonical_definition` dumps a model with sorted
keys, ``all`` / ``any`` aliases, ``None`` fields dropped and every non-bool
number as a float (``20`` and ``20.0`` are the same rule). Its SHA-256
(:func:`canonical_hash`) identifies a rule version's content; lifecycle
fields (``version``, ``status``, approvals, draft provenance) are excluded
so re-approving or re-numbering never changes the hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)

Op = Literal["<", "<=", ">", ">=", "==", "!=", "between", "in"]
OPS: tuple[str, ...] = ("<", "<=", ">", ">=", "==", "!=", "between", "in")
COMPARISON_OPS = frozenset({"<", "<=", ">", ">="})
EQUALITY_OPS = frozenset({"==", "!="})

Scope = Literal["candidate", "holding"]
CANDIDATE_ZONES: tuple[str, ...] = ("buy_zone", "drop")
HOLDING_ZONES: tuple[str, ...] = ("add", "trim", "exit_review")
SCOPE_ZONES: dict[str, tuple[str, ...]] = {
    "candidate": CANDIDATE_ZONES,
    "holding": HOLDING_ZONES,
}

CalibrationStatus = Literal["uncalibrated", "backtested", "validated"]
CALIBRATION_STATUSES: tuple[str, ...] = ("uncalibrated", "backtested", "validated")
InstanceStatus = Literal["draft", "active", "retired"]
INSTANCE_STATUSES: tuple[str, ...] = ("draft", "active", "retired")

PARAM_REF = re.compile(r"^\$(?P<name>[a-z][a-z0-9_]*)$")
TEMPLATE_ID = r"^[a-z][a-z0-9_]{1,63}$"
TEMPLATE_REF = re.compile(r"^(?P<id>[a-z][a-z0-9_]{1,63})@(?P<version>[1-9]\d*)$")

Scalar = bool | int | float | str
ConditionValue = Scalar | list[Scalar]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Condition(_Model):
    """One comparison of a factor with a literal or a ``$param`` value.

    ``op`` is kept as a plain string here so an unknown operator surfaces as
    a typed ``invalid_op`` issue from the validator.
    """

    factor: str
    op: str
    value: ConditionValue
    label: str | None = None
    """Short Chinese name of what the condition tests (e.g. 竞争优势)."""
    why: str | None = None
    """Why the condition is in the rule."""
    data_note: str | None = None
    """Known data caveat (proxy field, provider-only factor ...)."""


def _node_kind(raw: Any) -> str:
    if isinstance(raw, Mapping):
        return "condition" if "factor" in raw else "group"
    if isinstance(raw, Condition):
        return "condition"
    return "group"


ConditionNode = Annotated[
    Annotated[Condition, Tag("condition")] | Annotated["ConditionGroup", Tag("group")],
    Discriminator(_node_kind),
]


class ConditionGroup(_Model):
    """``{all: [...]}`` or ``{any: [...]}`` - exactly one of the two."""

    all_: list[ConditionNode] | None = Field(default=None, alias="all")
    any_: list[ConditionNode] | None = Field(default=None, alias="any")

    @model_validator(mode="after")
    def _one_mode(self) -> ConditionGroup:
        if (self.all_ is None) == (self.any_ is None):
            raise ValueError("a condition group needs exactly one of 'all' or 'any'")
        return self

    @property
    def mode(self) -> Literal["all", "any"]:
        return "all" if self.all_ is not None else "any"

    @property
    def children(self) -> list[Condition | ConditionGroup]:
        return list(self.all_ if self.all_ is not None else self.any_ or [])


ConditionGroup.model_rebuild()


class ParamRange(_Model):
    min: float
    max: float


class ParamMeta(_Model):
    label: str
    help: str | None = None


class TemplateExample(_Model):
    ticker: str
    note: str | None = None


def _split_param_specs(data: Any) -> Any:
    """``params: {name: {default, range, label, help}}`` -> the flat maps."""

    if not isinstance(data, Mapping):
        return data
    params = data.get("params")
    if not isinstance(params, Mapping) or not any(isinstance(v, Mapping) for v in params.values()):
        return data
    if not all(isinstance(v, Mapping) for v in params.values()):
        raise ValueError("params must be all numbers or all {default, range, label} mappings")
    if data.get("param_ranges") or data.get("param_meta"):
        raise ValueError(
            "param_ranges / param_meta must not be given when params use the "
            "{default, range, label} form"
        )
    defaults: dict[str, Any] = {}
    ranges: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    for name, spec in params.items():
        unknown = set(spec) - {"default", "range", "label", "help"}
        if unknown:
            raise ValueError(f"params.{name}: unknown keys {sorted(unknown)}")
        if "default" not in spec:
            raise ValueError(f"params.{name}: missing 'default'")
        defaults[name] = spec["default"]
        bounds = spec.get("range")
        if bounds is not None:
            if not isinstance(bounds, list | tuple) or len(bounds) != 2:
                raise ValueError(f"params.{name}.range must be [min, max]")
            ranges[name] = {"min": bounds[0], "max": bounds[1]}
        if spec.get("label") is not None or spec.get("help") is not None:
            meta[name] = {"label": spec.get("label") or "", "help": spec.get("help")}
    return {**data, "params": defaults, "param_ranges": ranges, "param_meta": meta or None}


class RuleTemplate(_Model):
    """Template layer (funnel design 5.1): one strategy archetype."""

    id: str = Field(pattern=TEMPLATE_ID)
    version: int = Field(default=1, ge=1)
    """Assigned by the rule store (template sync); a file's value is informational."""
    name: str = Field(min_length=1)
    description: str = ""
    applicable_groups: list[str]
    screen: ConditionGroup
    candidate_zones: dict[str, ConditionGroup]
    holding_zones: dict[str, ConditionGroup]
    params: dict[str, float] = Field(default_factory=dict)
    param_ranges: dict[str, ParamRange] = Field(default_factory=dict)
    calibration_status: CalibrationStatus = "uncalibrated"
    param_meta: dict[str, ParamMeta] | None = None
    # Strategy notes (authoring metadata; see module docstring).
    thesis: str | None = None
    fits: list[str] | None = None
    avoids: list[str] | None = None
    risks: list[str] | None = None
    falsification: str | None = None
    horizon: str | None = None
    examples: list[TemplateExample] | None = None

    @model_validator(mode="before")
    @classmethod
    def _param_specs(cls, data: Any) -> Any:
        return _split_param_specs(data)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def zones_for(self, scope: str) -> dict[str, ConditionGroup]:
        return dict(self.candidate_zones if scope == "candidate" else self.holding_zones)


class RuleInstance(_Model):
    """Instance layer (funnel design 5.2): one stock's rule for one scope.

    ``zones`` empty means "the template's zones for this scope"; ``params``
    overrides template parameter numbers within ``param_ranges``.
    """

    template_ref: str
    symbol: str
    scope: Scope
    params: dict[str, float] = Field(default_factory=dict)
    zones: dict[str, ConditionGroup] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    status: InstanceStatus = "draft"
    drafted_by: str = Field(min_length=1)
    draft_rationale: str = ""
    approved_by: str | None = None
    approved_at: datetime | None = None

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    def template_id_version(self) -> tuple[str, int] | None:
        match = TEMPLATE_REF.fullmatch(self.template_ref)
        if match is None:
            return None
        return match["id"], int(match["version"])


# --------------------------------------------------------------------------- #
# Canonical serialisation
# --------------------------------------------------------------------------- #

TEMPLATE_LIFECYCLE_FIELDS = frozenset({"version"})
INSTANCE_LIFECYCLE_FIELDS = frozenset(
    {"version", "status", "approved_by", "approved_at", "drafted_by", "draft_rationale"}
)


def _normalise(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalise(item) for item in value]
    return value


def canonical_definition(
    model: RuleTemplate | RuleInstance, *, include_lifecycle: bool = True
) -> dict[str, Any]:
    """JSON-compatible dict: aliases, no ``None`` fields, numbers as floats."""

    data = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not include_lifecycle:
        excluded = (
            TEMPLATE_LIFECYCLE_FIELDS
            if isinstance(model, RuleTemplate)
            else INSTANCE_LIFECYCLE_FIELDS
        )
        data = {key: item for key, item in data.items() if key not in excluded}
    normalised: dict[str, Any] = _normalise(data)
    for key in ("version",):
        if key in normalised:
            normalised[key] = int(normalised[key])
    return normalised


def canonical_json(model: RuleTemplate | RuleInstance, *, include_lifecycle: bool = True) -> str:
    return json.dumps(
        canonical_definition(model, include_lifecycle=include_lifecycle),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_hash(model: RuleTemplate | RuleInstance) -> str:
    """SHA-256 of the rule content (lifecycle fields excluded)."""

    return hashlib.sha256(
        canonical_json(model, include_lifecycle=False).encode("utf-8")
    ).hexdigest()


def to_yaml(model: RuleTemplate | RuleInstance) -> str:
    """Deterministic YAML (sorted keys) of the canonical definition."""

    return yaml.safe_dump(
        canonical_definition(model),
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    )


def param_ref(value: object) -> str | None:
    """``"$pe_pct_max"`` -> ``"pe_pct_max"``; anything else -> ``None``."""

    if isinstance(value, str):
        match = PARAM_REF.fullmatch(value)
        if match is not None:
            return match["name"]
    return None

"""Field source registry (P1-A12): which provider supplies each canonical field.

``config/field_sources.yaml`` (versioned) declares, per factor-layer field,
``primary`` and ``fallback`` sources, the definition (口径), a relative
``tolerance`` for cross-source comparison, whether it is a ``key`` field, and
per-provider ``sanity`` rules / ``approx`` markers. The factor view
(:mod:`investment_agent.factors.view`) resolves financial fields with it:

    iFinD-verified value > primary > fallback(s)

iFinD is never ``primary`` nor ``fallback`` (decision D6 rev 2): it only
verifies values on demand (P1-A13). Validation rejects such a registry.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

DEFAULT_FIELD_SOURCES_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "field_sources.yaml"
)

FieldTable = Literal["financial", "derived", "market", "report_overdue"]
FIELD_TABLES: frozenset[str] = frozenset({"financial", "derived", "market", "report_overdue"})
FREE_PROVIDERS: frozenset[str] = frozenset({"baostock", "akshare"})
VERIFIER = "ifind"
DERIVED = "derived"
DEFAULT_TOLERANCE = 0.02

Verification = Literal["unverified", "verified", "conflict"]

_FORMULA = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*([-+*/])\s*([a-z_][a-z0-9_]*)\s*$")


class FieldSourceError(ValueError):
    """The registry file is malformed or violates a rule."""


@dataclass(frozen=True)
class SanityRule:
    minimum: float | None = None
    maximum: float | None = None
    reason: str = ""

    def problem(self, value: float) -> str | None:
        """Why ``value`` is implausible, or ``None``."""

        if self.minimum is not None and value < self.minimum:
            return f"below plausible minimum {self.minimum}"
        if self.maximum is not None and value > self.maximum:
            return f"above plausible maximum {self.maximum}"
        return None


@dataclass(frozen=True)
class Formula:
    left: str
    op: str
    right: str

    def evaluate(self, left: float | None, right: float | None) -> float | None:
        if left is None or right is None:
            return None
        if self.op == "+":
            result = left + right
        elif self.op == "-":
            result = left - right
        elif self.op == "*":
            result = left * right
        else:
            if right == 0:
                return None
            result = left / right
        return result if math.isfinite(result) else None

    def __str__(self) -> str:
        return f"{self.left} {self.op} {self.right}"


@dataclass(frozen=True)
class FieldSource:
    name: str
    table: FieldTable
    primary: str
    definition: str
    fallback: tuple[str, ...] = ()
    tolerance: float = DEFAULT_TOLERANCE
    key: bool = False
    sanity: Mapping[str, SanityRule] = field(default_factory=dict)
    approx: frozenset[str] = frozenset()
    formula: Formula | None = None

    @property
    def order(self) -> tuple[str, ...]:
        """Free sources in precedence order (primary first)."""

        return (self.primary, *self.fallback)

    def sanity_problem(self, provider: str, value: float) -> str | None:
        rule = self.sanity.get(provider)
        return rule.problem(value) if rule is not None else None

    def is_approx(self, provider: str) -> bool:
        return provider in self.approx


def relative_difference(a: float, b: float) -> float:
    """``|a - b| / max(|a|, |b|)``; 0 when both are 0."""

    scale = max(abs(a), abs(b))
    return 0.0 if scale == 0 else abs(a - b) / scale


def beyond_tolerance(a: float, b: float, tolerance: float) -> bool:
    return relative_difference(a, b) > tolerance


@dataclass(frozen=True)
class FieldSourceRegistry:
    version: str
    fields: Mapping[str, FieldSource]
    default_tolerance: float = DEFAULT_TOLERANCE

    def __contains__(self, name: object) -> bool:
        return name in self.fields

    def __iter__(self) -> Iterator[FieldSource]:
        return iter(self.fields.values())

    def get(self, name: str) -> FieldSource | None:
        return self.fields.get(name)

    def require(self, name: str) -> FieldSource:
        try:
            return self.fields[name]
        except KeyError:
            raise KeyError(f"field {name!r} is not registered in field_sources") from None

    def by_table(self, *tables: str) -> list[FieldSource]:
        return [source for source in self if source.table in tables]

    def tolerance(self, name: str) -> float:
        source = self.fields.get(name)
        return source.tolerance if source is not None else self.default_tolerance

    def sanity_problem(self, provider: str, name: str, value: float) -> str | None:
        source = self.fields.get(name)
        return source.sanity_problem(provider, value) if source is not None else None


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FieldSourceError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _provider_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FieldSourceError(f"{where}: expected a list of provider names")
    return tuple(value)


def _parse_field(name: str, raw: Any, default_tolerance: float) -> FieldSource:
    where = f"field_sources.{name}"
    if not isinstance(raw, dict):
        raise FieldSourceError(f"{where}: expected a mapping")
    unknown = set(raw) - {
        "table", "primary", "fallback", "definition", "tolerance", "key", "sanity",
        "approx", "formula",
    }
    if unknown:
        raise FieldSourceError(f"{where}: unknown keys {sorted(unknown)}")
    table = raw.get("table")
    if table not in FIELD_TABLES:
        raise FieldSourceError(f"{where}.table: one of {sorted(FIELD_TABLES)}, got {table!r}")
    primary = raw.get("primary")
    if not isinstance(primary, str):
        raise FieldSourceError(f"{where}.primary is required")
    fallback = _provider_list(raw.get("fallback"), f"{where}.fallback")
    if primary == VERIFIER or VERIFIER in fallback:
        raise FieldSourceError(
            f"{where}: iFinD only verifies (P1-A13); it cannot be a primary or fallback source"
        )
    allowed = FREE_PROVIDERS | {DERIVED} if table == "derived" else FREE_PROVIDERS
    for provider in (primary, *fallback):
        if provider not in allowed:
            raise FieldSourceError(f"{where}: unknown source {provider!r}")
    if len(set((primary, *fallback))) != 1 + len(fallback):
        raise FieldSourceError(f"{where}: a source is listed twice")
    formula: Formula | None = None
    if primary == DERIVED:
        text = raw.get("formula")
        match = _FORMULA.fullmatch(text) if isinstance(text, str) else None
        if match is None:
            raise FieldSourceError(f"{where}.formula: expected '<field> <op> <field>'")
        formula = Formula(match.group(1), match.group(2), match.group(3))
    elif "formula" in raw:
        raise FieldSourceError(f"{where}.formula is only allowed with primary 'derived'")
    definition = raw.get("definition")
    if not isinstance(definition, str) or not definition.strip():
        raise FieldSourceError(f"{where}.definition (口径) is required")
    tolerance = _number(raw.get("tolerance", default_tolerance), f"{where}.tolerance")
    if not 0 < tolerance <= 1:
        raise FieldSourceError(f"{where}.tolerance must be in (0, 1]")
    key = raw.get("key", False)
    if not isinstance(key, bool):
        raise FieldSourceError(f"{where}.key must be a boolean")
    sources = set((primary, *fallback))
    sanity: dict[str, SanityRule] = {}
    for provider, rule in (raw.get("sanity") or {}).items():
        if provider not in sources:
            raise FieldSourceError(f"{where}.sanity.{provider}: not a source of this field")
        if not isinstance(rule, dict) or not set(rule) <= {"min", "max", "reason"}:
            raise FieldSourceError(f"{where}.sanity.{provider}: expected min / max / reason")
        sanity[provider] = SanityRule(
            minimum=_number(rule["min"], f"{where}.sanity.{provider}.min")
            if "min" in rule
            else None,
            maximum=_number(rule["max"], f"{where}.sanity.{provider}.max")
            if "max" in rule
            else None,
            reason=str(rule.get("reason", "")).strip(),
        )
    approx_raw = raw.get("approx") or {}
    if not isinstance(approx_raw, dict):
        raise FieldSourceError(f"{where}.approx: expected a mapping provider -> bool")
    for provider in approx_raw:
        if provider not in sources:
            raise FieldSourceError(f"{where}.approx.{provider}: not a source of this field")
    return FieldSource(
        name=name,
        table=table,
        primary=primary,
        definition=" ".join(definition.split()),
        fallback=fallback,
        tolerance=tolerance,
        key=key,
        sanity=sanity,
        approx=frozenset(provider for provider, flag in approx_raw.items() if flag),
        formula=formula,
    )


def parse_field_sources(document: Any) -> FieldSourceRegistry:
    if not isinstance(document, dict):
        raise FieldSourceError("field_sources: expected a mapping")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise FieldSourceError("field_sources.version is required")
    default_tolerance = _number(
        document.get("default_tolerance", DEFAULT_TOLERANCE), "field_sources.default_tolerance"
    )
    raw_fields = document.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise FieldSourceError("field_sources.fields is required")
    fields = {
        str(name): _parse_field(str(name), raw, default_tolerance)
        for name, raw in raw_fields.items()
    }
    for source in fields.values():
        if source.formula is None:
            continue
        for operand in (source.formula.left, source.formula.right):
            target = fields.get(operand)
            if target is None or target.table != "financial":
                raise FieldSourceError(
                    f"field_sources.{source.name}.formula: {operand!r} is not a registered "
                    "financial field"
                )
    return FieldSourceRegistry(version=version, fields=fields, default_tolerance=default_tolerance)


def load_field_sources(path: Path | str | None = None) -> FieldSourceRegistry:
    target = Path(path) if path is not None else DEFAULT_FIELD_SOURCES_PATH
    return parse_field_sources(yaml.safe_load(target.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def default_field_sources() -> FieldSourceRegistry:
    return load_field_sources()

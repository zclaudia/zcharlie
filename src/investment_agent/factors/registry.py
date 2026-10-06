"""Factor registry: metadata, applicability and the ``FactorValue`` contract.

Every factor declares ``name``, ``version``, ``unit``, ``inputs`` (factor-
layer field names per provider), ``applicability`` (industry groups) and a
pure ``compute(view) -> FactorValue``. Rules reference factors by ``name``.

Quality vocabulary:

* ``ok`` - computed from complete inputs as defined;
* ``approx`` - computed, but from an approximating input or a shortened
  history (``flags`` say why);
* ``missing`` - no value: inputs absent, or the value is not meaningful for
  this company (e.g. PE of a loss-maker; ``flags`` say which). A rule
  condition on a missing factor does not hold;
* ``not_applicable`` - the factor does not apply to the company's industry
  group (rules skip the condition).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from investment_agent.factors.industry_groups import FLAG_INDUSTRY_FALLBACK as INDUSTRY_FALLBACK
from investment_agent.factors.view import ProviderName, SnapshotView

Quality = Literal["ok", "missing", "not_applicable", "approx"]
QUALITIES: frozenset[str] = frozenset({"ok", "missing", "not_applicable", "approx"})

# Shared flag vocabulary.
FLAG_REPORT_OVERDUE = "report_overdue"
FLAG_NEGATIVE_EARNINGS = "negative_earnings"
FLAG_NEGATIVE_BOOK_VALUE = "negative_book_value"
FLAG_NON_POSITIVE_REVENUE = "non_positive_revenue"
FLAG_SHORT_HISTORY = "short_history"
FLAG_UNSUPPORTED_PROVIDER = "unsupported_provider"
FLAG_INDUSTRY_FALLBACK = INDUSTRY_FALLBACK
"""The industry group came from CSRC (or nothing), not Shenwan 2021 (P1-B3)."""


@dataclass(frozen=True)
class FactorValue:
    name: str
    version: str
    value: float | None
    quality: Quality
    flags: tuple[str, ...] = ()
    inputs_ref: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quality not in QUALITIES:
            raise ValueError(f"unknown quality {self.quality!r}")
        if self.quality in {"missing", "not_applicable"} and self.value is not None:
            raise ValueError(f"{self.name}: a {self.quality} factor carries no value")
        if self.quality in {"ok", "approx"} and self.value is None:
            raise ValueError(f"{self.name}: quality {self.quality} needs a value")


@dataclass(frozen=True)
class FactorInput:
    """One input: a factor-layer field read from one provider's snapshots."""

    provider: ProviderName
    field: str
    source: str
    """Snapshot location, e.g. ``financial_snapshot.fields`` / ``market_snapshot.pe_ttm``."""


@dataclass(frozen=True)
class Applicability:
    """Industry groups a factor applies to (``groups=None`` = every group)."""

    groups: frozenset[str] | None = None
    excluded: frozenset[str] = frozenset()

    def applies_to(self, group: str) -> bool:
        if group in self.excluded:
            return False
        return self.groups is None or group in self.groups

    def describe(self) -> str:
        if self.groups is None and not self.excluded:
            return "all"
        if self.groups is None:
            return "all except " + ", ".join(sorted(self.excluded))
        return ", ".join(sorted(self.groups - self.excluded))


ALL_GROUPS = Applicability()


@dataclass(frozen=True)
class Partial:
    """What a factor body returns; the registry wraps it into a ``FactorValue``."""

    value: float | None
    quality: Quality | None = None
    """``None`` -> ``ok`` with a value, ``missing`` without."""
    flags: tuple[str, ...] = ()
    inputs_ref: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Factor:
    name: str
    version: str
    unit: str
    description: str
    inputs: tuple[FactorInput, ...]
    body: Callable[[SnapshotView], Partial]
    applicability: Applicability = ALL_GROUPS
    carries_overdue_flag: bool = False
    """Add ``report_overdue`` when the listing has an open overdue report."""

    @property
    def providers(self) -> frozenset[str]:
        return frozenset(item.provider for item in self.inputs)

    def compute(self, view: SnapshotView, *, enforce_applicability: bool = True) -> FactorValue:
        """Evaluate on ``view``.

        ``enforce_applicability=False`` computes even for industry groups the
        factor does not apply to (observation-path parity with canonical
        fields, which do not know industry groups).
        """

        base_ref: dict[str, Any] = {
            "provider": view.provider,
            "as_of": view.as_of.isoformat(),
            "industry_group": view.industry_group,
        }
        if view.industry_match is not None:
            base_ref["industry"] = view.industry_match.describe()
        context_flags: tuple[str, ...] = ()
        if view.industry_fallback:
            context_flags = (FLAG_INDUSTRY_FALLBACK,)
        if self.carries_overdue_flag and view.overdue_periods:
            context_flags = (*context_flags, FLAG_REPORT_OVERDUE)
            base_ref["overdue_periods"] = [period.isoformat() for period in view.overdue_periods]
        if enforce_applicability and not self.applicability.applies_to(view.industry_group):
            return FactorValue(
                self.name, self.version, None, "not_applicable", context_flags, base_ref
            )
        if view.provider not in self.providers:
            return FactorValue(
                self.name,
                self.version,
                None,
                "missing",
                (FLAG_UNSUPPORTED_PROVIDER, *context_flags),
                base_ref,
            )
        result = self.body(view)
        quality: Quality
        if result.quality is not None:
            quality = result.quality
        else:
            quality = "ok" if result.value is not None else "missing"
        flags = tuple(dict.fromkeys((*result.flags, *context_flags)))
        return FactorValue(
            self.name,
            self.version,
            result.value,
            quality,
            flags,
            {**base_ref, **result.inputs_ref},
        )


class FactorRegistry:
    def __init__(self, factors: Iterable[Factor] = ()) -> None:
        self._factors: dict[str, Factor] = {}
        for factor in factors:
            self.register(factor)

    def register(self, factor: Factor) -> Factor:
        if factor.name in self._factors:
            raise ValueError(f"factor {factor.name!r} is already registered")
        if not factor.inputs:
            raise ValueError(f"factor {factor.name!r} declares no inputs")
        self._factors[factor.name] = factor
        return factor

    def get(self, name: str) -> Factor:
        try:
            return self._factors[name]
        except KeyError:
            raise KeyError(f"unknown factor {name!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._factors

    def __iter__(self) -> Iterator[Factor]:
        return iter(self._factors.values())

    def __len__(self) -> int:
        return len(self._factors)

    def names(self) -> list[str]:
        return list(self._factors)

    def compute_all(
        self, view: SnapshotView, names: Iterable[str] | None = None
    ) -> dict[str, FactorValue]:
        selected = self.names() if names is None else list(names)
        return {name: self.get(name).compute(view) for name in selected}

    def catalogue(self) -> list[dict[str, Any]]:
        """Metadata of every factor (for docs, rule validation and the UI)."""

        return [
            {
                "name": factor.name,
                "version": factor.version,
                "unit": factor.unit,
                "description": factor.description,
                "applicability": factor.applicability.describe(),
                "inputs": [
                    {"provider": item.provider, "field": item.field, "source": item.source}
                    for item in factor.inputs
                ],
            }
            for factor in self
        ]

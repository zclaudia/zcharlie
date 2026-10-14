"""Factor-layer field resolution over provider rows (P1-A12).

Stored provider fields are mapped to factor-layer names and resolved across
providers; this module is pure - it operates on :class:`ProviderFields`
values, never on a session.

**Field semantics.** Provider fields that share a stored name but not a
meaning are renamed here and only here; the stored snapshot JSON keeps its
names. Factors only see the factor-layer names:

=================  ========================  =========================
stored name        baostock meaning          iFinD meaning
=================  ========================  =========================
``net_profit``     ``net_profit_total``      ``net_profit_parent``
                   (净利润 incl. minority)   (归母净利润)
``revenue``        ``revenue_main``          ``revenue_total``
                   (主营营业收入)            (营业总收入)
=================  ========================  =========================

The ambiguous stored names are never exposed. iFinD per-share source values
(basic EPS, net assets per share, revenue per share of a fiscal year) become
``eps_basic_fy`` / ``bvps_fy`` / ``revenue_ps_fy``.

**Field resolution** (:func:`resolve_period_fields`). Report periods come
from the policy's anchor provider (baostock: its ``pubDate`` decides when a
period is public); other providers only contribute fields to those periods,
each row gated by its own ``available_from``. Per registered financial field:

1. an iFinD row whose ``fields.verification[<field>] == "verified"`` wins
   (written by P1-A13);
2. otherwise the first source in ``primary`` + ``fallback`` order with a
   value that passes the provider's ``sanity`` rule; a failing value is kept
   as *rejected* (e.g. baostock ``debt_ratio < 0.02``) and the next source is
   tried;
3. if iFinD verification marked the field ``conflict``, the iFinD value wins
   (D6 rev 2) and the free-source values are kept as ``conflicts``;
4. otherwise the chosen value is ``conflict`` when another free source
   differs beyond the field's ``tolerance`` (approximate sources excluded) or
   a collector recorded a ``provider_conflicts`` entry; else ``unverified``.

Each resolved field carries a :class:`FieldResolution` (``provider``,
``verification``, rejected values, conflicts) that factors copy into
``FactorValue.inputs_ref``. ``derived`` registry fields are computed from
resolved fields (``approx``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from investment_agent.factors.field_sources import (
    VERIFIER,
    FieldSource,
    FieldSourceRegistry,
    Formula,
    Verification,
    default_field_sources,
    relative_difference,
)
from investment_agent.factors.metrics import parse_number

if TYPE_CHECKING:  # the ORM row type is annotation-only: this module stays database-free
    from investment_agent.storage.models.pit_snapshot import FinancialSnapshot

# Stored field name -> factor-layer name, per provider. Names absent here keep
# their stored name; names mapped to ``None`` are dropped.
PROVIDER_FIELD_RENAMES: dict[str, dict[str, str]] = {
    "baostock": {"net_profit": "net_profit_total", "revenue": "revenue_main"},
    "ifind": {"net_profit": "net_profit_parent", "revenue": "revenue_total"},
    # AkShare (Eastmoney) rows are stored under factor-layer names already.
    "akshare": {},
}
AMBIGUOUS_STORED_FIELDS = frozenset({"net_profit", "revenue"})
# iFinD ``source_values`` (raw registry source fields) -> factor-layer names.
IFIND_SOURCE_VALUE_FIELDS: dict[str, str] = {
    "ths_eps_basic_stock": "eps_basic_fy",
    "ths_nav_ps_stock": "bvps_fy",
    "ths_revenue_ps_stock": "revenue_ps_fy",
    "ths_cash_paid_for_assets_stock": "capital_expenditure",
}
_NON_FIELD_KEYS = frozenset(
    {
        "missing_fields",
        "multi_period_fields",
        "source_values",
        "pub_date",
        "stat_date",
        "tables",
        # verification / cross-check bookkeeping (P1-A12 / A11 / A13)
        "verification",
        "provider_conflicts",
        # AkShare (Eastmoney) capture metadata (P1-A11)
        "part",
        "notice_date",
        "update_date",
        "restated",
        "restated_basis",
        "availability_basis",
        "endpoints",
        "pre_listing",
    }
)
# Fields a provider can only approximate from its own other fields (a
# registry fallback marked ``approx``): baostock has no absolute cash flows,
# but CFOToNP (operating cash flow / net profit) × netProfit recovers it.
PROVIDER_DERIVED_FIELDS: dict[tuple[str, str], Formula] = {
    ("baostock", "operating_cash_flow"): Formula("cfo_to_np", "*", "net_profit_total"),
}


def factor_fields(provider: str, stored: Mapping[str, object]) -> dict[str, float | None]:
    """Stored ``financial_snapshot.fields`` -> factor-layer numeric fields.

    Renames provider-ambiguous names (see module docstring); bookkeeping keys
    and non-numeric values are dropped.
    """

    renames = PROVIDER_FIELD_RENAMES.get(provider)
    if renames is None:
        raise ValueError(f"unknown provider {provider!r}")
    fields: dict[str, float | None] = {}
    for name, value in stored.items():
        if name in _NON_FIELD_KEYS:
            continue
        target = renames.get(name, name)
        if target in AMBIGUOUS_STORED_FIELDS:
            raise ValueError(f"ambiguous field {name!r} has no {provider} factor-layer name")
        if value is None or isinstance(value, bool):
            fields[target] = None
        elif isinstance(value, int | float):
            fields[target] = float(value)
    if provider == "ifind":
        source_values = stored.get("source_values")
        if isinstance(source_values, Mapping):
            for source, target in IFIND_SOURCE_VALUE_FIELDS.items():
                if source in source_values and target not in fields:
                    fields[target] = parse_number(source_values[source])
    return fields


@dataclass(frozen=True)
class RejectedValue:
    """A provider value skipped by a registry sanity rule."""

    provider: str
    value: float
    reason: str
    ref: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "value": self.value,
            "reason": self.reason,
            "ref": self.ref,
        }


@dataclass(frozen=True)
class FieldResolution:
    """Where one resolved financial field came from (P1-A12)."""

    provider: str | None
    """Chosen source (``derived`` for registry formulas); ``None`` = no usable value."""
    verification: Verification = "unverified"
    ref: str | None = None
    approx: bool = False
    rejected: tuple[RejectedValue, ...] = ()
    conflicts: tuple[Mapping[str, Any], ...] = ()

    def describe(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "verification": self.verification,
            "ref": self.ref,
        }
        if self.approx:
            result["approx"] = True
        if self.rejected:
            result["rejected"] = [item.describe() for item in self.rejected]
        if self.conflicts:
            result["conflicts"] = [dict(item) for item in self.conflicts]
        return result


@dataclass(frozen=True)
class ProviderFields:
    """One provider's factor-layer fields for a report period (resolver input)."""

    provider: str
    fields: Mapping[str, float | None]
    ref: str | None = None
    verification: Mapping[str, str] = field(default_factory=dict)
    """iFinD rows: ``{field: "verified" | "conflict"}`` (P1-A13 contract)."""
    recorded_conflicts: Mapping[str, Any] = field(default_factory=dict)
    """``fields.provider_conflicts`` written by a collector cross-check (P1-A11)."""

    def value(self, name: str) -> float | None:
        value = self.fields.get(name)
        if value is not None:
            return value
        formula = PROVIDER_DERIVED_FIELDS.get((self.provider, name))
        if formula is None:
            return None
        return formula.evaluate(self.fields.get(formula.left), self.fields.get(formula.right))

    def is_derived(self, name: str) -> bool:
        return self.fields.get(name) is None and (self.provider, name) in PROVIDER_DERIVED_FIELDS


def _ifind_conflict_value(name: str, verifier: ProviderFields | None) -> float | None:
    if verifier is None or verifier.verification.get(name) != "conflict":
        return None
    return verifier.value(name)


def _resolve_one(
    source: FieldSource, by_provider: Mapping[str, ProviderFields]
) -> tuple[float | None, FieldResolution]:
    name = source.name
    verifier = by_provider.get(VERIFIER)
    if verifier is not None and verifier.verification.get(name) == "verified":
        verified = verifier.value(name)
        if verified is not None:
            return verified, FieldResolution(VERIFIER, "verified", verifier.ref)

    rejected: list[RejectedValue] = []
    chosen: tuple[str, float, ProviderFields] | None = None
    sane: dict[str, float] = {}
    for provider in source.order:
        candidate = by_provider.get(provider)
        if candidate is None:
            continue
        value = candidate.value(name)
        if value is None:
            continue
        problem = source.sanity_problem(provider, value)
        if problem is not None:
            reason = source.sanity[provider].reason or problem
            rejected.append(RejectedValue(provider, value, f"{problem}: {reason}", candidate.ref))
            continue
        sane[provider] = value
        if chosen is None:
            chosen = (provider, value, candidate)
    ifind_conflict = _ifind_conflict_value(name, verifier)
    if chosen is None:
        if ifind_conflict is not None:
            return ifind_conflict, FieldResolution(
                VERIFIER, "conflict", verifier.ref if verifier else None, rejected=tuple(rejected)
            )
        return None, FieldResolution(None, "unverified", rejected=tuple(rejected))

    provider, value, candidate = chosen
    if ifind_conflict is not None:
        # D6 rev 2 (p1-plan 2.3): when iFinD verification disagrees with the
        # free sources, iFinD wins; the free values stay on record.
        free_values = [
            {"provider": other, "value": other_value, "verification": "conflict"}
            for other, other_value in sane.items()
        ]
        return ifind_conflict, FieldResolution(
            VERIFIER,
            "conflict",
            verifier.ref if verifier else None,
            rejected=tuple(rejected),
            conflicts=tuple(free_values),
        )
    approx = source.is_approx(provider) or candidate.is_derived(name)
    conflicts: list[dict[str, Any]] = []
    if not approx:
        for other, other_value in sane.items():
            other_candidate = by_provider[other]
            if other == provider or source.is_approx(other) or other_candidate.is_derived(name):
                continue
            difference = relative_difference(value, other_value)
            if difference > source.tolerance:
                conflicts.append(
                    {
                        "provider": other,
                        "value": other_value,
                        "relative_difference": round(difference, 6),
                        "tolerance": source.tolerance,
                    }
                )
    for candidate_fields in by_provider.values():
        recorded = candidate_fields.recorded_conflicts.get(name)
        if recorded:
            conflicts.append({"recorded_by": candidate_fields.provider, "detail": recorded})
    verification: Verification = "conflict" if conflicts else "unverified"
    return value, FieldResolution(
        provider,
        verification,
        candidate.ref,
        approx=approx,
        rejected=tuple(rejected),
        conflicts=tuple(conflicts),
    )


def resolve_period_fields(
    candidates: Iterable[ProviderFields],
    *,
    anchor: str,
    registry: FieldSourceRegistry | None = None,
) -> tuple[dict[str, float | None], dict[str, FieldResolution]]:
    """Resolve one report period's fields across providers (P1-A12).

    Unregistered fields of the ``anchor`` provider pass through unchanged
    (e.g. baostock ``current_ratio``); registered ``financial`` fields follow
    the registry precedence; ``derived`` fields are computed afterwards.
    Returns the factor-layer fields and the per-field resolution.
    """

    sources = registry or default_field_sources()
    by_provider = {candidate.provider: candidate for candidate in candidates}
    fields: dict[str, float | None] = {}
    anchor_fields = by_provider.get(anchor)
    if anchor_fields is not None:
        fields.update(
            (name, value) for name, value in anchor_fields.fields.items() if name not in sources
        )
    resolutions: dict[str, FieldResolution] = {}
    for source in sources.by_table("financial"):
        if not any(
            candidate.value(source.name) is not None for candidate in by_provider.values()
        ):
            if any(source.name in candidate.fields for candidate in by_provider.values()):
                fields[source.name] = None
            continue
        value, resolution = _resolve_one(source, by_provider)
        fields[source.name] = value
        resolutions[source.name] = resolution
    for source in sources.by_table("derived"):
        formula = source.formula
        assert formula is not None  # enforced by the registry parser
        derived = formula.evaluate(fields.get(formula.left), fields.get(formula.right))
        if derived is None:
            continue
        fields[source.name] = derived
        resolutions[source.name] = FieldResolution("derived", "unverified", str(formula), True)
    return fields, resolutions


def _provider_fields(provider: str, rows: Sequence[FinancialSnapshot]) -> ProviderFields:
    """Merge one provider's rows of a period (parts by capture time, later wins)."""

    fields: dict[str, float | None] = {}
    verification: dict[str, str] = {}
    conflicts: dict[str, Any] = {}
    for row in sorted(rows, key=lambda item: (item.captured_at, item.version)):
        for name, value in factor_fields(provider, row.fields).items():
            if value is not None or name not in fields:
                fields[name] = value
        stored_verification = row.fields.get("verification")
        if isinstance(stored_verification, Mapping):
            verification.update({str(k): str(v) for k, v in stored_verification.items()})
        stored_conflicts = row.fields.get("provider_conflicts")
        if isinstance(stored_conflicts, Mapping):
            conflicts.update({str(k): v for k, v in stored_conflicts.items()})
    ref = "+".join(f"financial_snapshot:{row.id}" for row in rows)
    return ProviderFields(provider, fields, ref, verification, conflicts)


def provider_fields_from_rows(provider: str, rows: Sequence[FinancialSnapshot]) -> ProviderFields:
    """Public form of the resolver input for one provider's rows of a period
    (used by the iFinD verification job, P1-A13, to compare against the free
    sources exactly as the view resolves them)."""

    return _provider_fields(provider, rows)

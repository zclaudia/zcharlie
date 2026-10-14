"""The shared metric vocabulary and result builders of the material layer.

``CompanyProfile``, ``MetricResult`` and ``MetricSet`` are the result language every
material-metric module speaks: the six deterministic rules in
:mod:`investment_agent.materials.metrics`, the injected-input PE in
:mod:`investment_agent.materials.valuation` and the dividend yield in
:mod:`investment_agent.materials.dividends` all produce ``MetricResult`` values, and
``valuation``/``dividends`` take the ``CompanyProfile`` as an argument. The vocabulary
therefore lives here, *below* the rule modules, instead of inside the rules module: the
dependency direction is leaf -> vocabulary, never a pure calculation leaf -> the rules.

This module is a pure leaf: no IO, no database, no network, no HTTP client and no new
dependency. It imports only the standard library, the shared ``investor`` contract types
(``Quantity``, ``CalculationStatus``) and
:mod:`investment_agent.materials.contracts` (``FactPeriod``/``FactRecord`` are what the
helpers read), and it imports no rule module, so nothing here can pull a rule, a store or
a handler into a caller. The display-label tables stay with the modules that own their
keys — ``METRIC_LABELS`` in ``metrics.py``, ``LABELS`` in ``valuation.py`` and the two
name constants in ``dividends.py``: :func:`build_result` takes the label as an argument
(the label is a ``MetricResult`` field, not a lookup this module performs), so this module
never knows a metric key or what a key is called in the product.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from investment_agent.investor.common import Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.contracts import FactPeriod, FactRecord


@dataclass(frozen=True)
class CompanyProfile:
    """A security's applicability profile, mapped from task 01's controlled identity.

    ``is_bank`` and ``is_ads`` drive the applicability rules; neither is inferred from the
    numbers. ``report_currency`` is the reporting currency of the statements (not the
    quote currency of the listing), and every amount metric must be stated in it.
    """

    company_key: str
    is_bank: bool
    is_ads: bool
    ads_ratio: str | None
    report_currency: str
    quote_currency: str
    fiscal_year_end_note: str


@dataclass(frozen=True)
class MetricResult:
    """One metric for one window: status, value (when computable), full basis text.

    ``basis`` is never empty and always names numerator/denominator, the period, the
    definition used and whether the amounts are in the original reporting currency.
    ``notes`` carries comparability, non-applicability and loss-year explanations.
    ``inputs`` lists ``"metric@period_end"`` for input-hash and traceability use.
    """

    key: str
    label: str
    status: CalculationStatus
    value: Quantity | None
    basis: str
    periods: tuple[FactPeriod, ...]
    notes: tuple[str, ...]
    inputs: tuple[str, ...]

    @property
    def period_end(self) -> date | None:
        """The end of the last period this result describes (``None`` without periods)."""

        return self.periods[-1].end if self.periods else None


@dataclass(frozen=True)
class MetricSet:
    """All metric results for one company plus the human-readable gap list."""

    metrics: tuple[MetricResult, ...]
    gaps: tuple[str, ...]


def build_result(
    key: str,
    label: str,
    *,
    status: CalculationStatus,
    basis: str,
    value: Quantity | None = None,
    periods: tuple[FactPeriod, ...] = (),
    notes: tuple[str, ...] = (),
    inputs: tuple[str, ...] = (),
) -> MetricResult:
    """Build one ``MetricResult`` from the caller's key/label and the rule's evidence.

    ``label`` is passed in because the display naming of a key belongs to the module that
    owns the key; the only invariant enforced here is the shared one every rule module
    relies on: a result without a basis text is a bug, not an empty result.
    """

    if not basis:
        raise ValueError(f"metric {key} requires a non-empty basis")
    return MetricResult(
        key=key,
        label=label,
        status=status,
        value=value,
        basis=basis,
        periods=periods,
        notes=notes,
        inputs=inputs,
    )


def _ratio_quantity(value: Decimal) -> Quantity:
    return Quantity(value=value, unit="ratio")


def _amount_quantity(value: Decimal, currency: str) -> Quantity:
    return Quantity(value=value, unit=currency, currency=currency)


def _amount(fact: FactRecord) -> Decimal | None:
    raw = fact.value.raw_value
    if raw is None:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    return parsed * fact.value.scale


def _ordered_years(fiscal_years: Sequence[FactPeriod]) -> tuple[FactPeriod, ...]:
    unique: dict[date, FactPeriod] = {}
    for period in fiscal_years:
        unique.setdefault(period.end, period)
    return tuple(unique[end] for end in sorted(unique))


def _span_days(period: FactPeriod) -> int | None:
    if period.days is not None:
        return period.days
    if period.start is None:
        return None
    return (period.end - period.start).days


def _period_label(period: FactPeriod | None) -> str:
    if period is None:
        return "the previous period"
    if period.label:
        return period.label
    return period.end.isoformat()


def _unique_periods(*periods: FactPeriod) -> tuple[FactPeriod, ...]:
    """The involved periods, deduplicated and ordered so the last one ends the latest."""

    unique: dict[tuple[date | None, date, object, str | None], FactPeriod] = {}
    for period in periods:
        unique.setdefault((period.start, period.end, period.basis, period.label), period)
    return tuple(
        sorted(unique.values(), key=lambda period: (period.end, period.start or period.end))
    )


def _signed(value: Decimal) -> str:
    return f"+{value}" if value >= 0 else str(value)

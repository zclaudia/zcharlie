"""Verified dividend events with an explicit final-amount determination date (plan 14).

What is checkable, and what is not
----------------------------------
For the pilots the dividend events themselves are checkable against the issuers' own
documents: the declared amount and its currency, ordinary versus special, per ordinary
share versus per ADS, the announcement / ex-dividend / record / payment dates, and — kept
separately — the date on which the *final* amount in the payment currency was determined.
Two things are **not** verified and are therefore never invented here:

* complete historical coverage of a company's dividends (the first delivery shows the
  checked events and the gaps), and
* the moment within a day at which a publication became available: dates stay dates and
  this module never fabricates a clock time.

Time discipline
---------------
:func:`known_amount_at` is the only outward time gate:

* ``at < announced_on`` -> unknown (``None`` with ``"unknown"``);
* ``announced_on <= at < final_amount_determined_on`` -> the declared amount with
  ``"declared:<currency>"``;
* ``at >= final_amount_determined_on`` -> the final amount with ``"final:<currency>"``.

The ex-dividend date never moves that boundary. For HSBC's 2024 fourth interim the
declared US$0.36 is all that is knowable on the 6 March 2025 ex-dividend date; the final
HK$2.791007 is knowable only from 14 April 2025, when the issuer determined the rate.

Yield discipline
----------------
:func:`dividend_yield` computes one **named** definition over an explicitly covered
window, using only amounts knowable at the stated as-of date. Nothing is estimated: a
missing event, a missing price, a missing currency conversion or an event that cannot be
placed in the window gives ``MISSING_INPUTS`` with a gap list in ``notes`` and no value.
A caller that cannot state coverage gets no yield at all — that is the point of this
delivery. The ``basis`` of every result names the definition, whether special dividends
are included, whether the amounts are original-currency or converted at a caller-stated
rate, and that the amounts are pre-tax. :func:`net_dividend_yield` never produces a
number: withholding tax and ADR fees are unverified.

Capability
----------
:data:`CAPABILITY` keeps both new capabilities off: no online dividend source is enabled
and no verified complete history exists yet. Events arrive through an offline import and
the page (task 11) shows the checked events plus the gaps.

Contract notes recorded deliberately
------------------------------------
* ``DividendEvent.announced_on`` is optional: an event known only from its ex-dividend
  date (the first delivery has one) must be representable with an unknown availability
  date instead of a fabricated announcement date.
* ``DividendEvent.fiscal_year`` is the issuer's fiscal-year label of the dividend (a
  fourth interim announced in February 2025 still belongs to FY2024). ``fiscal_year_declared``
  cannot be defined without it, and it is never back-computed from the announcement date.
* ``event_view`` refuses to guess: a dividend without a stated ordinary/special kind, a
  per-share amount that is a float, a non-positive declared amount, a final amount without
  a currency, a final-amount date without the final amount, or an FX rate without a stated
  source raises ``ValueError`` rather than being imported with a silent default.

Pure computation: no IO, no database, no network, no new dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from investment_agent.investor.common import Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.contracts import DatePrecision, FactPeriod, PeriodBasis
from investment_agent.materials.metric_core import MetricResult, build_result

#: The two capabilities this module would back. Both stay off in the first delivery: the
#: online dividend event source is not enabled and complete historical coverage is not
#: accepted, so the page only shows the checked events and the coverage gaps.
CAPABILITY: Final[dict[str, bool]] = {"dividends": False, "dividend_yield": False}

DIVIDEND_YIELD_KEY: Final[str] = "dividend_yield"
DIVIDEND_YIELD_LABEL: Final[str] = "股息率（税前，发行人宣告口径）"
NET_DIVIDEND_YIELD_LABEL: Final[str] = "股息率（税后到账，不交付）"

#: Amounts in these currencies are the ones the pilots declare dividends in.
_DECLARED_CURRENCIES: Final[frozenset[str]] = frozenset({"USD", "HKD", "CNY", "TWD"})


class DividendType(StrEnum):
    """Ordinary versus special. Kept separate so a total never mixes them silently."""

    ORDINARY = "ordinary"
    SPECIAL = "special"


class ShareClass(StrEnum):
    """Which security the per-share amount belongs to.

    An ADS entitlement is not an ordinary-share amount (HSBC's ADS represents five
    ordinary shares), so the two are never summed together or priced with each other.
    """

    ORDINARY = "ordinary"
    ADS = "ads"


class YieldDefinition(StrEnum):
    """The three named yield definitions; a fraction is never called just "dividend yield"."""

    FISCAL_YEAR_DECLARED = "fiscal_year_declared"
    TRAILING_TWELVE_MONTHS_EX_DATE = "trailing_twelve_months_ex_date"
    TRAILING_TWELVE_MONTHS_PAID = "trailing_twelve_months_paid"


@dataclass(frozen=True)
class DividendEvent:
    """One verified dividend event, with the final-amount determination date kept apart.

    ``declared_amount`` is the issuer's announced per-share amount in
    ``declared_currency``; ``final_amount`` (with ``final_currency``, ``final_fx_rate``
    and ``final_fx_source``) is the amount actually payable in the payment currency once
    the issuer determined it, and ``final_amount_determined_on`` is the date that
    happened. The final amount is the issuer's stated (rounded) figure and is never
    re-derived from the declared amount and the rate.

    ``available_from`` and ``date_precision`` are derived by :func:`event_view` from the
    two dated facts only — never from ``ex_date``: the final amount enters availability
    only when ``final_amount_determined_on`` exists, and the declared amount otherwise.
    """

    declared_currency: str
    declared_amount: Decimal
    share_class: ShareClass
    kind: DividendType
    announced_on: date | None
    ex_date: date | None
    record_date: date | None
    pay_date: date | None
    final_currency: str | None
    final_amount: Decimal | None
    final_fx_rate: Decimal | None
    final_fx_source: str | None
    final_amount_determined_on: date | None
    available_from: date | None
    date_precision: DatePrecision
    source_ref: str
    locator: str | None
    fiscal_year: int | None

    @property
    def availability_basis(self) -> str:
        """Which dated fact backs ``available_from``: ``final``, ``declared`` or ``unknown``.

        ``declared`` only: the final amount is not part of availability while
        ``final_amount_determined_on`` is missing, whatever the ex-dividend date says.
        """

        if self.final_amount_determined_on is not None and self.final_amount is not None:
            return "final"
        if self.announced_on is not None:
            return "declared"
        return "unknown"


@dataclass(frozen=True)
class DividendCoverage:
    """What a caller states the imported event set covers for one security.

    The module cannot discover a dividend that was never imported, so completeness is
    never inferred from the events it happens to hold: the caller states how many events
    the verified source contains in the window (``expected_events``) and, optionally,
    which source that statement comes from.
    """

    expected_events: int
    source_ref: str | None = None


@dataclass(frozen=True)
class FxInput:
    """The yield denominator: one share's price plus the conversions the caller states.

    ``price`` is the price of one ``listing_share_class`` security in ``price_currency``
    as of ``as_of``. ``rates`` maps a dividend currency to the number of
    ``price_currency`` units one unit of it is worth (``{"USD": Decimal("7.77")}`` for a
    HKD-quoted price). This module never looks up a rate: an unstated conversion is a
    reported gap, never an estimate.
    """

    price: Decimal
    price_currency: str
    as_of: date
    source_ref: str
    rates: Mapping[str, Decimal] = field(default_factory=dict)
    listing_share_class: ShareClass = ShareClass.ORDINARY


def event_view(raw: Mapping[str, Any]) -> DividendEvent:
    """Build one event from an imported raw mapping; absent fields stay absent.

    Required: ``declared_currency``, ``declared_amount``, ``share_class``, ``kind`` and
    ``source_ref``. Everything else may be missing and stays ``None`` — no default is
    filled in. Dates must be ``date`` objects or ISO ``YYYY-MM-DD`` strings (a clock time
    is refused), amounts must be strings, integers or ``Decimal`` (a float is refused),
    and internally inconsistent combinations raise ``ValueError`` instead of being
    imported with a guessed value.
    """

    declared_currency = _required_text(raw, "declared_currency")
    if declared_currency not in _DECLARED_CURRENCIES:
        raise ValueError(
            f"declared_currency {declared_currency!r} is not one of the pilot dividend "
            f"currencies ({', '.join(sorted(_DECLARED_CURRENCIES))})"
        )
    declared_amount = _decimal(raw, "declared_amount")
    if declared_amount is None:
        raise ValueError("event is missing 'declared_amount'; a dividend without its amount is unusable")
    if declared_amount <= 0:
        raise ValueError(
            f"declared_amount {declared_amount} is not positive; a missing or zero amount is never "
            "imported as a dividend"
        )
    share_class = _enum(raw, "share_class", ShareClass)
    kind = _enum(raw, "kind", DividendType)
    source_ref = _required_text(raw, "source_ref")
    announced_on = _optional_date(raw, "announced_on")
    ex_date = _optional_date(raw, "ex_date")
    record_date = _optional_date(raw, "record_date")
    pay_date = _optional_date(raw, "pay_date")
    final_amount_determined_on = _optional_date(raw, "final_amount_determined_on")
    final_currency = _optional_text(raw, "final_currency")
    final_amount = _decimal(raw, "final_amount")
    final_fx_rate = _decimal(raw, "final_fx_rate")
    final_fx_source = _optional_text(raw, "final_fx_source")
    fiscal_year = _optional_int(raw, "fiscal_year")
    locator = _optional_text(raw, "locator")

    if final_amount_determined_on is not None and final_amount is None:
        raise ValueError(
            "final_amount_determined_on is set but final_amount is missing; the determination date "
            "is not imported as a final amount"
        )
    if final_amount is not None and final_currency is None:
        raise ValueError("final_amount is set but final_currency is missing")
    if final_fx_rate is not None and final_fx_rate <= 0:
        raise ValueError(f"final_fx_rate {final_fx_rate} is not positive")
    if final_fx_rate is not None and final_fx_source is None:
        raise ValueError(
            "final_fx_rate is set but final_fx_source is missing; a rate without a stated source is "
            "not usable"
        )
    if (
        final_amount_determined_on is not None
        and announced_on is not None
        and final_amount_determined_on < announced_on
    ):
        raise ValueError(
            f"final_amount_determined_on {final_amount_determined_on.isoformat()} precedes "
            f"announced_on {announced_on.isoformat()}"
        )

    available_from, precision = _availability(
        announced_on=announced_on,
        final_amount_determined_on=final_amount_determined_on,
        final_amount=final_amount,
    )
    return DividendEvent(
        declared_currency=declared_currency,
        declared_amount=declared_amount,
        share_class=share_class,
        kind=kind,
        announced_on=announced_on,
        ex_date=ex_date,
        record_date=record_date,
        pay_date=pay_date,
        final_currency=final_currency,
        final_amount=final_amount,
        final_fx_rate=final_fx_rate,
        final_fx_source=final_fx_source,
        final_amount_determined_on=final_amount_determined_on,
        available_from=available_from,
        date_precision=precision,
        source_ref=source_ref,
        locator=locator,
        fiscal_year=fiscal_year,
    )


def known_amount_at(event: DividendEvent, at: date) -> tuple[Decimal | None, str]:
    """The amount knowable at ``at`` plus its basis label.

    ``(None, "unknown")`` before the announcement; ``(declared_amount,
    "declared:<currency>")`` from the announcement on; ``(final_amount,
    "final:<currency>")`` from ``final_amount_determined_on`` on. An amount that is not
    knowable is ``None``, never ``0``. The ex-dividend date is deliberately not consulted,
    so it can never pull the final amount forward.
    """

    if event.announced_on is None or at < event.announced_on:
        return None, "unknown"
    if (
        event.final_amount_determined_on is not None
        and event.final_amount is not None
        and event.final_currency is not None
        and at >= event.final_amount_determined_on
    ):
        return event.final_amount, f"final:{event.final_currency}"
    return event.declared_amount, f"declared:{event.declared_currency}"


def event_gaps(events: Sequence[DividendEvent]) -> tuple[str, ...]:
    """Everything that keeps this event set from being a complete, uniform basis.

    Per event: an unknown announcement date, an ex-dividend date no exchange source
    confirmed, a determination date without the final amount, or a final amount whose
    currency is unstated. Across the set: mixed share classes and mixed declared
    currencies. The list is what the page shows next to the events.
    """

    gaps: list[str] = []
    for event in events:
        reference = _event_ref(event)
        if event.announced_on is None:
            gaps.append(f"{reference}: no announcement date, so available_from is unknown")
        if event.ex_date is None:
            gaps.append(
                f"{reference}: no ex-dividend date confirmed by an exchange source; it is not inferred"
            )
        if event.final_amount_determined_on is not None and event.final_amount is None:
            gaps.append(f"{reference}: a final-amount determination date without the final amount")
        if event.final_amount is not None and event.final_currency is None:
            gaps.append(f"{reference}: a final amount without its currency")
    classes = sorted({event.share_class.value for event in events})
    if len(classes) > 1:
        gaps.append(
            f"share classes are mixed ({', '.join(classes)}); an ADS entitlement is not an "
            "ordinary-share amount and the per-share figures are not combined"
        )
    currencies = sorted({event.declared_currency for event in events})
    if len(currencies) > 1:
        gaps.append(
            f"declared currencies differ ({', '.join(currencies)}); a total needs a stated conversion"
        )
    return tuple(gaps)


def dividend_yield(
    events: Sequence[DividendEvent],
    *,
    definition: str,
    include_special: bool,
    price: FxInput | None,
    fiscal_year: int | None = None,
    as_of: date | None = None,
    coverage: DividendCoverage | None = None,
) -> MetricResult:
    """One named yield definition over an explicitly covered window.

    ``definition`` is one of :class:`YieldDefinition` (anything else raises
    ``ValueError``). ``fiscal_year`` is required by ``fiscal_year_declared``; ``as_of``
    (the date knowability is judged at) and ``coverage`` are required by every definition.
    The numerator is the sum of the amounts knowable at ``as_of``; the denominator is the
    caller's price. Amounts in the price's currency need no conversion, other currencies
    need a rate stated with the price. Any gap — missing event, missing price, missing
    conversion, amount not knowable at ``as_of``, an event that cannot be placed in the
    window — returns ``MISSING_INPUTS`` with the gap list in ``notes`` and no value.
    """

    named = _definition(definition)
    basis = _yield_basis(
        named,
        include_special=include_special,
        fiscal_year=fiscal_year,
        as_of=as_of,
        price=price,
        coverage=coverage,
    )
    if as_of is None:
        return _yield_result(
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            notes=(
                "no as-of date was stated; which of the events were knowable is undefined without it",
            ),
        )
    window, periods, window_gaps = _yield_window(
        events, named, fiscal_year=fiscal_year, as_of=as_of
    )
    if window_gaps:
        return _yield_result(
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=window_gaps,
        )
    if coverage is None:
        return _yield_result(
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=(
                "no coverage was stated for the imported events; this delivery never infers "
                "completeness from the events it happens to hold",
            ),
        )
    if coverage.expected_events <= 0:
        raise ValueError(
            "coverage.expected_events must be positive; an empty window is not a covered one"
        )
    notes: list[str] = []
    window_label = _window_label(named, fiscal_year=fiscal_year, as_of=as_of)
    if coverage.source_ref is not None:
        notes.append(
            f"coverage stated by the caller: {coverage.expected_events} event(s) in {window_label} "
            f"(source: {coverage.source_ref})"
        )
    missing_events = coverage.expected_events - len(window)
    if missing_events > 0:
        notes.append(
            f"{missing_events} of the {coverage.expected_events} event(s) the coverage statement "
            f"expects in {window_label} are missing; no partial total is produced"
        )
        return _yield_result(
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
        )
    if len(window) > coverage.expected_events:
        notes.append(
            f"the window holds {len(window)} events while the coverage statement expects "
            f"{coverage.expected_events}; the statement is a lower bound and is stale"
        )
    classes = sorted({event.share_class.value for event in window})
    if len(classes) > 1:
        notes.append(
            f"share classes are mixed ({', '.join(classes)}); an ADS entitlement is not an "
            "ordinary-share amount, so the per-share amounts are not combined"
        )
        return _yield_result(
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
        )
    specials = [event for event in window if event.kind is DividendType.SPECIAL]
    contributing = tuple(
        event for event in window if include_special or event.kind is not DividendType.SPECIAL
    )
    if include_special and not specials:
        notes.append("no special dividend is present in the window; the total is ordinary only")
    if not include_special and specials:
        notes.append(
            f"{len(specials)} special dividend event(s) are excluded because include_special is False"
        )
    if not contributing:
        notes.append("the window holds no dividend event to total for this include_special setting")
        return _yield_result(
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
        )
    if price is None:
        notes.append(
            "no price (yield denominator) was provided with the events; the events are not turned "
            "into a percentage on their own"
        )
        return _yield_result(
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
        )
    event_class = window[0].share_class
    if event_class is not price.listing_share_class:
        notes.append(
            f"the events are per {event_class.value} security while the price is per "
            f"{price.listing_share_class.value} security; the two are not combined"
        )
        return _yield_result(
            status=CalculationStatus.NOT_APPLICABLE,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
        )
    if price.price <= 0:
        notes.append(
            f"the stated price {price.price} {price.price_currency} is not positive; no yield is "
            "produced"
        )
        return _yield_result(
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=tuple(notes),
        )
    gaps: list[str] = []
    reported_pairs: set[tuple[str, str]] = set()
    inputs: list[str] = []
    labels: list[str] = []
    source_totals: dict[str, Decimal] = {}
    converted: list[str] = []
    total = Decimal(0)
    for event in contributing:
        amount, label = known_amount_at(event, as_of)
        inputs.append(f"{_event_ref(event)} -> {label} {amount if amount is not None else ''}".strip())
        if amount is None:
            gaps.append(
                f"{_event_ref(event)}: the amount is not knowable at {as_of}; it is not totalled as "
                "zero and the remaining events are not shown as a total"
            )
            continue
        currency = label.split(":", 1)[1]
        factor = _conversion_to_price_currency(currency, price, gaps, reported_pairs)
        if factor is None:
            continue
        labels.append(label)
        source_totals[currency] = source_totals.get(currency, Decimal(0)) + amount
        if factor != 1 and currency not in converted:
            converted.append(currency)
        total += amount * factor
    if gaps:
        return _yield_result(
            status=CalculationStatus.MISSING_INPUTS,
            basis=basis,
            periods=periods,
            notes=tuple(notes + gaps),
        )
    label_summary = ", ".join(f"{label} ×{labels.count(label)}" for label in sorted(set(labels)))
    rendered_totals = ", ".join(
        f"{amount} {currency}" for currency, amount in sorted(source_totals.items())
    )
    if converted:
        rates = ", ".join(
            f"1 {currency} = {price.rates[currency]} {price.price_currency}" for currency in converted
        )
        currency_clause = (
            f"converted at the caller-stated rate(s) {rates}; a converted numerator is an estimate, "
            "not the issuer's final-rate amount"
        )
    else:
        currency_clause = (
            f"all amounts and the price are in {price.price_currency} (original currency, no conversion)"
        )
    basis = (
        f"{basis}; numerator: {rendered_totals} over {len(contributing)} event(s) "
        f"(knowable-amount labels: {label_summary}); {currency_clause}"
    )
    notes.append(
        f"{len(contributing)} event(s) contributed as of {as_of} (knowable-amount labels: "
        f"{label_summary})"
    )
    return _yield_result(
        status=CalculationStatus.OK,
        value=Quantity(value=total / price.price, unit="ratio"),
        basis=basis,
        periods=periods,
        notes=tuple(notes),
        inputs=tuple(inputs),
    )


def net_dividend_yield(
    events: Sequence[DividendEvent],
    *,
    definition: str,
    include_special: bool,
    price: FxInput | None,
    fiscal_year: int | None = None,
    as_of: date | None = None,
    coverage: DividendCoverage | None = None,
) -> MetricResult:
    """The after-tax, after-fee yield. Never produced in this delivery.

    Withholding tax at source and the fees the depositary charges between the issuer's
    payment and the holder's account are unverified, so no net amount is stated — also on
    the ADS side, where each ADS represents five ordinary shares and the ADR fees are not
    verified. The result is always ``NOT_APPLICABLE`` and names the reasons; the gross
    definitions in :func:`dividend_yield` are the pre-tax figures.
    """

    named = _definition(definition)
    ads = [event for event in events if event.share_class is ShareClass.ADS]
    notes = [
        "net-of-fees dividend yield is not produced: withholding tax at source is not verified for "
        "this delivery, so a post-tax amount is not derived from the declared one"
    ]
    if ads:
        notes.append(
            f"{len(ads)} ADS event(s) are in scope: each ADS represents five ordinary shares in the "
            "issuer's own announcement, and the ADR fees charged by the depositary are not verified, "
            "so no net proceeds per ADS are stated"
        )
    notes.append(
        f"the gross definition {named.value} (include_special={include_special}) states its amounts "
        "before tax; it is the definition this delivery reports"
    )
    if price is None:
        notes.append("no price was provided with the events; the denominator is missing as well")
    basis = (
        f"{DIVIDEND_YIELD_KEY}[{named.value}] net of withholding tax and depositary fees is not "
        "applicable in this delivery: the tax withheld at source and the ADR fees are unverified, "
        "and no net amount is estimated or derived"
    )
    return MetricResult(
        key=DIVIDEND_YIELD_KEY,
        label=NET_DIVIDEND_YIELD_LABEL,
        status=CalculationStatus.NOT_APPLICABLE,
        value=None,
        basis=basis,
        periods=(),
        notes=tuple(notes),
        inputs=tuple(_event_ref(event) for event in events),
    )


# --- raw mapping helpers -------------------------------------------------------


def _availability(
    *,
    announced_on: date | None,
    final_amount_determined_on: date | None,
    final_amount: Decimal | None,
) -> tuple[date | None, DatePrecision]:
    """``available_from`` and its precision, from the two dated facts only.

    The final amount can only make the event available from the date it was determined;
    without that date the declared amount is what the announcement makes available. An
    event with neither date stays unknown — the ex-dividend date is not consulted.
    """

    if final_amount_determined_on is not None and final_amount is not None:
        return final_amount_determined_on, DatePrecision.DATE
    if announced_on is not None:
        return announced_on, DatePrecision.DATE
    return None, DatePrecision.UNKNOWN


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None:
        raise ValueError(f"event is missing {key!r}; a dividend event without it is unusable")
    text = str(value).strip()
    if not text:
        raise ValueError(f"event field {key!r} is empty; an empty value is not a value")
    return text


def _optional_text(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _optional_date(raw: Mapping[str, Any], key: str) -> date | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValueError(f"event field {key!r} must be a calendar date, not a datetime")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if "T" in text or ":" in text:
            raise ValueError(
                f"event field {key!r}={text!r} carries a clock time; only calendar dates are stored"
            )
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"event field {key!r}={text!r} is not an ISO date") from exc
    raise ValueError(f"event field {key!r} must be a date or an ISO date string")


def _decimal(raw: Mapping[str, Any], key: str) -> Decimal | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, (bool, float)):
        raise ValueError(
            f"event field {key!r} must be a string, integer or Decimal, not {type(value).__name__}"
        )
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"event field {key!r}={value!r} is not a decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"event field {key!r}={value!r} is not finite")
    return parsed


def _optional_int(raw: Mapping[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"event field {key!r} must be an integer fiscal-year label")
    return value


def _enum[EnumT: StrEnum](raw: Mapping[str, Any], key: str, kind: type[EnumT]) -> EnumT:
    text = _required_text(raw, key)
    try:
        return kind(text)
    except ValueError as exc:
        legal = ", ".join(member.value for member in kind)
        raise ValueError(f"event field {key!r}={text!r} is not one of: {legal}") from exc


# --- yield helpers -------------------------------------------------------------


def _definition(value: str) -> YieldDefinition:
    try:
        return YieldDefinition(value)
    except ValueError as exc:
        legal = ", ".join(member.value for member in YieldDefinition)
        raise ValueError(
            f"unknown dividend-yield definition {value!r}; expected one of: {legal}"
        ) from exc


def _yield_window(
    events: Sequence[DividendEvent],
    named: YieldDefinition,
    *,
    fiscal_year: int | None,
    as_of: date,
) -> tuple[tuple[DividendEvent, ...], tuple[FactPeriod, ...], tuple[str, ...]]:
    """The events a definition may total, the period it covers, and what cannot be placed."""

    if named is YieldDefinition.FISCAL_YEAR_DECLARED:
        if fiscal_year is None:
            return (), (), (
                "no fiscal year was stated; the declared total of 'the latest year' is never inferred",
            )
        problems: list[str] = []
        placed: list[DividendEvent] = []
        for event in events:
            if event.fiscal_year is None:
                problems.append(
                    f"{_event_ref(event)}: no fiscal-year label, so it cannot be placed in the "
                    f"declared total of fiscal year {fiscal_year}"
                )
            elif event.fiscal_year == fiscal_year:
                placed.append(event)
        return tuple(placed), (), tuple(problems)
    start = _one_year_before(as_of)
    field_name = (
        "ex-date"
        if named is YieldDefinition.TRAILING_TWELVE_MONTHS_EX_DATE
        else "payment date"
    )
    problems = []
    placed = []
    for event in events:
        value = _window_date(event, named)
        if value is None:
            problems.append(
                f"{_event_ref(event)}: no {field_name}, so it cannot be placed in the trailing "
                f"twelve months to {as_of}"
            )
        elif start < value <= as_of:
            placed.append(event)
    period = FactPeriod(
        start=start + timedelta(days=1),
        end=as_of,
        basis=PeriodBasis.ANNUAL,
        label=f"trailing twelve months to {as_of} (by {field_name})",
        days=None,
    )
    return tuple(placed), (period,), tuple(problems)


def _window_date(event: DividendEvent, named: YieldDefinition) -> date | None:
    if named is YieldDefinition.TRAILING_TWELVE_MONTHS_EX_DATE:
        return event.ex_date
    return event.pay_date


def _one_year_before(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _conversion_to_price_currency(
    currency: str,
    price: FxInput,
    gaps: list[str],
    reported_pairs: set[tuple[str, str]],
) -> Decimal | None:
    if currency == price.price_currency:
        return Decimal(1)
    pair = (currency, price.price_currency)
    rate = price.rates.get(currency)
    if rate is None:
        if pair not in reported_pairs:
            reported_pairs.add(pair)
            gaps.append(
                f"no {currency}/{price.price_currency} conversion is stated with the price; the "
                f"{currency} amounts are not converted and no total is produced"
            )
        return None
    if rate <= 0:
        if pair not in reported_pairs:
            reported_pairs.add(pair)
            gaps.append(
                f"the stated {currency}/{price.price_currency} conversion {rate} is not positive; no "
                "total is produced"
            )
        return None
    return rate


def _window_label(
    named: YieldDefinition, *, fiscal_year: int | None, as_of: date | None
) -> str:
    if named is YieldDefinition.FISCAL_YEAR_DECLARED:
        if fiscal_year is None:
            return "an unstated fiscal year"
        return f"issuer fiscal year {fiscal_year}"
    if as_of is None:
        return "an unstated trailing twelve months"
    field_name = (
        "ex-date" if named is YieldDefinition.TRAILING_TWELVE_MONTHS_EX_DATE else "payment date"
    )
    return f"the trailing twelve months to {as_of} (by {field_name})"


def _yield_basis(
    named: YieldDefinition,
    *,
    include_special: bool,
    fiscal_year: int | None,
    as_of: date | None,
    price: FxInput | None,
    coverage: DividendCoverage | None,
) -> str:
    window = _window_label(named, fiscal_year=fiscal_year, as_of=as_of)
    specials = (
        "ordinary and special dividends included"
        if include_special
        else "ordinary dividends only; special dividends excluded"
    )
    if price is None:
        denominator = "denominator: no price was provided, so the events are not turned into a ratio"
    else:
        denominator = (
            f"denominator: price {price.price} {price.price_currency} per "
            f"{price.listing_share_class.value} security as of {price.as_of} ({price.source_ref})"
        )
    if coverage is None:
        covered = "coverage: not stated by the caller, so completeness is not assumed"
    else:
        stated = f"coverage: the caller states {coverage.expected_events} event(s) in the window"
        covered = f"{stated} ({coverage.source_ref})" if coverage.source_ref else stated
    return (
        f"{DIVIDEND_YIELD_KEY}[{named.value}] over {window} = the sum of the per-share amounts "
        "knowable at the stated as-of date divided by the price; amounts are used in the original "
        "currency when the price is in it and otherwise converted only at a rate the caller states "
        "(never a rate of this module's own), so a converted numerator is an estimate and not the "
        "issuer's final-rate amount; "
        f"{specials}; pre-tax issuer-declared gross amounts, withholding tax and ADR fees not "
        f"deducted; {denominator}; {covered}; the value is a fraction of the price (×100 for a percent)"
    )


def _yield_result(
    *,
    status: CalculationStatus,
    basis: str,
    value: Quantity | None = None,
    periods: tuple[FactPeriod, ...] = (),
    notes: tuple[str, ...] = (),
    inputs: tuple[str, ...] = (),
) -> MetricResult:
    """Build a result for this module's one key through the shared builder."""

    return build_result(
        DIVIDEND_YIELD_KEY,
        DIVIDEND_YIELD_LABEL,
        status=status,
        basis=basis,
        value=value,
        periods=periods,
        notes=notes,
        inputs=inputs,
    )


def _event_ref(event: DividendEvent) -> str:
    when = (
        event.announced_on.isoformat()
        if event.announced_on is not None
        else "unknown announcement date"
    )
    where = f" [{event.locator}]" if event.locator else ""
    return (
        f"{event.kind.value} {event.declared_currency} {event.declared_amount} announced {when}{where} "
        f"({event.source_ref})"
    )

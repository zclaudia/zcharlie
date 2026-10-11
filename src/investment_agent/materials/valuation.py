"""Conditional annual PE for the HK/US material layer (plan 13, optional tail).

This module carries the *arithmetic and the gating* of an annual price-to-earnings
ratio; it is not a delivered valuation capability. It is a pure computation layer:

* no IO, no database, no network, no HTTP client and no new dependency — the price
  record, the EPS fact, the company profile and (only when the EPS currency differs
  from the quote currency) a dated FX record are all arguments;
* every capability bit in :data:`CAPABILITY` is ``False`` by decision: the first
  release has no verified daily price source and no verified same-date FX
  capability. :func:`annual_pe` therefore only runs when a caller explicitly
  injects those inputs; the default product path never calls it and states
  :data:`DISABLED_REASONS` instead;
* missing is never zero and nothing is converted for convenience: a null or
  non-positive EPS, a missing price, a missing or undated FX record and a rate
  further than :data:`FX_MAX_LAG_DAYS` days from the price trade date all produce a
  documented non-OK status, never a number;
* the share basis is checked by rule. A split that falls after the EPS period's end
  leaves the price (split adjusted or as traded) and the EPS on different share
  counts, so no ratio is produced. :data:`SPLIT_EVENTS` is a hand-checked constant
  with its source recorded below — it is never derived from a price series.

Naming: the measure is an *annual fiscal-year* figure. The basic and the diluted
per-share bases are named separately (``annual_pe_basic`` / ``annual_pe_diluted``)
and the number is never presented as a trailing four-quarter measure.

Not supported, and repeated in every result's notes: see :data:`UNSUPPORTED_GAPS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Final

from investment_agent.investor.common import Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.materials.contracts import FactPeriod, FactRecord, PerShareBasis
from investment_agent.materials.metrics import CompanyProfile, MetricResult

#: The capability bits the read model must consult; all false by decision.
CAPABILITY: Final[dict[str, bool]] = {
    "annual_pe": False,
    "annual_pe_basic": False,
    "annual_pe_diluted": False,
}

#: Why the capability is off: no accepted price source and no accepted same-date FX.
DISABLED_REASONS: Final[tuple[str, ...]] = (
    "no verified daily price source in the first release",
    "quote-currency conversion requires a verified same-date FX record",
)

#: Valuation outputs that are deliberately not implemented; listed, never implied.
UNSUPPORTED_GAPS: Final[tuple[str, ...]] = (
    "average PE: not supported in the first release (no verified multi-date price series)",
    "historical PE/PB percentile: not supported in the first release (no long verified history)",
    "trailing-twelve-month PE: not supported in the first release (only the annual "
    "fiscal-year measure is defined)",
    "reverse DCF: not supported in the first release (no verified assumption set)",
    "PB: not supported in the first release (ordinary-share equity and the applicable "
    "share count are not verified)",
)

#: The metric keys of the EPS facts this module accepts.
EPS_METRICS: Final[frozenset[str]] = frozenset({"eps_basic", "eps_diluted"})

#: Display labels, matching the ``METRIC_LABELS`` convention of the material layer.
LABELS: Final[dict[str, str]] = {
    "annual_pe": "年度市盈率",
    "annual_pe_basic": "年度市盈率（基本每股）",
    "annual_pe_diluted": "年度市盈率（稀释每股）",
}

#: An FX record more than this many days away from the price trade date is rejected:
#: a stale rate is never silently carried over to another day.
FX_MAX_LAG_DAYS: Final[int] = 3

#: Source date precisions that establish *when* the rate was quoted. ``unknown``
#: (and anything else) does not, so the record cannot support a dated conversion.
FX_DATED_PRECISIONS: Final[frozenset[str]] = frozenset({"date", "timestamp"})

#: Division precision of the ratio (the default decimal context precision, set
#: explicitly so the result does not depend on ambient context changes).
_DIVISION_PRECISION: Final[int] = 28

#: Recorded splits, by controlled key, as ``((effective split date, ratio), ...)``.
#:
#: Source: ``research/market-data-validation/remaining-validation.md`` (the "Yahoo
#: Close 按拆股调整" note): Apple's 4-for-1 split, with 2020-08-31 as the first
#: split-adjusted trading day, cross-checked against Apple's own announcement of
#: 2020-07-30 — the Yahoo ``Close`` of 2020-08-28 is already stated on the
#: post-split share count. Only this case is decidable in the first release. A new
#: entry is added only from the issuer's own filing; it is never back-inferred
#: from a price series.
SPLIT_EVENTS: Final[dict[str, tuple[tuple[str, int], ...]]] = {
    "AAPL": (("2020-08-31", 4),),
}

#: ``CompanyProfile.company_key`` to :data:`SPLIT_EVENTS` key, only for issuers
#: with a recorded split. The controlled identity list keys Apple as ``apple``
#: while its ticker is ``AAPL`` (``config/research_material/pilot_list.yaml``), and
#: the frozen profile carries the company key, not the ticker. This is not a
#: general identity map: a further split adds one entry here and one in
#: :data:`SPLIT_EVENTS`, after the filing check.
SPLIT_EVENT_KEYS: Final[dict[str, str]] = {"apple": "AAPL"}

#: Added to every ``OK`` result: the number exists only because a caller injected
#: the inputs, not because the product delivers valuation.
INJECTED_NOTE: Final[str] = (
    "computed from explicitly injected price/EPS inputs only; the annual-PE capability stays "
    "disabled until a daily price source and a same-date FX record pass acceptance"
)


@dataclass(frozen=True)
class PriceInput:
    """One explicitly injected close, with the share basis it is stated on.

    ``share_basis`` is free text naming the basis (for example ``"split_adjusted"``
    or ``"as_traded"``); ``is_split_adjusted`` states whether the series has been
    restated across splits, which decides whether a price date before a recorded
    split is comparable with an older EPS period. Neither field is inferred.
    """

    close: Decimal
    trade_date: date
    currency: str
    share_basis: str
    is_split_adjusted: bool


@dataclass(frozen=True)
class FxInput:
    """One explicitly injected FX record for a dated conversion.

    ``pair`` is written ``"<converted currency>/<quote currency>"`` — the plan's
    ``"TWD/USD"`` example — so the quote (price) currency is the numerator of the
    rate's unit: ``rate`` is quote-currency units per one unit of the first
    currency (``USD per TWD``), and the EPS amount is multiplied by it.
    ``source_date_precision`` must be ``"date"`` or ``"timestamp"``: an unknown
    precision does not establish a dated rate. ``source`` is the caller's
    provenance string; it is kept out of the rendered ``basis`` text.
    """

    pair: str
    rate: Decimal
    as_of: date
    source: str
    source_date_precision: str


def annual_pe(
    *,
    price: PriceInput | None,
    eps: FactRecord,
    profile: CompanyProfile,
    fx: FxInput | None = None,
) -> MetricResult:
    """Price close / annual fiscal-year EPS, only when every input is injected.

    Check precedence, each step documented in the returned ``basis`` and ``notes``:

    1. an ADS listing (``profile.is_ads``) produces no ADS PE in the first release:
       the result is ``NOT_APPLICABLE`` and the notes are :func:`ads_pe_gap`;
    2. ``eps`` must be an ``eps_basic`` / ``eps_diluted`` fact and its
       ``semantics.per_share_basis`` must name the share count used (``NOT_APPLICABLE``
       when it is ``NA``); the key is ``annual_pe_basic`` / ``annual_pe_diluted``;
    3. a missing EPS value is ``MISSING_INPUTS``, a non-positive EPS is
       ``NOT_APPLICABLE`` ("EPS non-positive" — a multiple of a loss has no meaning);
    4. a missing price is ``MISSING_INPUTS`` (:data:`DISABLED_REASONS` names why the
       product has none), a non-positive or non-finite close is not a price;
    5. a recorded split after the EPS period end (and, for a split-adjusted series,
       any split after the EPS period end) makes the share bases incomparable →
       ``NOT_APPLICABLE`` with a reason containing "split";
    6. a different EPS currency requires an FX record: dated precision, the exact
       ``<EPS currency>/<quote currency>`` pair, a positive rate and at most
       :data:`FX_MAX_LAG_DAYS` days from the price trade date — anything else is
       ``MISSING_INPUTS`` or ``STALE_INPUTS``, and no convenience conversion or
       fixed rate is ever applied.

    ``OK`` therefore only happens for explicitly injected, same-basis inputs, and
    the result still repeats :data:`UNSUPPORTED_GAPS`.
    """

    key = _key_for(eps)
    periods = (eps.period,)
    inputs = _input_refs(price=price, eps=eps, fx=fx)
    definition = _definition(key)

    if profile.is_ads:
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=_not_computed(
                definition, "an ADS listing produces no ADS PE in the first release"
            ),
            periods=periods,
            notes=(
                f"{profile.company_key} is an ADS listing: no ADS PE is produced in the "
                "first release",
                *ads_pe_gap(profile, fx=fx),
                *UNSUPPORTED_GAPS,
            ),
            inputs=inputs,
        )

    if eps.metric not in EPS_METRICS:
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=_not_computed(
                definition, f"{eps.metric} is not a basic/diluted EPS metric"
            ),
            periods=periods,
            notes=(
                f"the fact metric {eps.metric!r} is not an EPS metric; annual PE needs "
                "eps_basic or eps_diluted",
                *UNSUPPORTED_GAPS,
            ),
            inputs=inputs,
        )

    if eps.semantics.per_share_basis == PerShareBasis.NA:
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=_not_computed(
                definition,
                "the fact's per_share_basis is not_applicable (neither basic nor diluted)",
            ),
            periods=periods,
            notes=(
                "the EPS fact does not state a basic or diluted per-share basis, so the "
                "share count behind a ratio is undefined",
                *UNSUPPORTED_GAPS,
            ),
            inputs=inputs,
        )

    eps_amount = _eps_amount(eps)
    if eps_amount is None:
        reason = eps.value.missing_reason or "no value with an unstated reason"
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=_not_computed(definition, f"the EPS fact carries no value ({reason})"),
            periods=periods,
            notes=(
                f"missing input: the EPS fact at {eps.period.end.isoformat()} has no value "
                f"({reason}); a missing value is never treated as zero",
                *UNSUPPORTED_GAPS,
            ),
            inputs=inputs,
        )

    if eps_amount <= 0:
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=_not_computed(definition, "EPS non-positive"),
            periods=periods,
            notes=(
                "EPS non-positive: a price-to-earnings multiple of a zero or loss-making "
                "period has no meaning",
                *UNSUPPORTED_GAPS,
            ),
            inputs=inputs,
        )

    if price is None:
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=_not_computed(definition, "no price input was injected"),
            periods=periods,
            notes=(DISABLED_REASONS[0], *UNSUPPORTED_GAPS),
            inputs=inputs,
        )

    if not price.close.is_finite():
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=_not_computed(definition, f"the price close {price.close} is not finite"),
            periods=periods,
            notes=(
                f"the injected close {price.close} is not a finite decimal; it is not a price",
                *UNSUPPORTED_GAPS,
            ),
            inputs=inputs,
        )

    if price.close <= 0:
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=_not_computed(definition, "the price close is non-positive"),
            periods=periods,
            notes=(
                f"the injected close {price.close} is not a traded price (non-positive)",
                *UNSUPPORTED_GAPS,
            ),
            inputs=inputs,
        )

    mismatch = _split_mismatch(profile, eps, price)
    if mismatch is not None:
        return _result(
            key,
            status=CalculationStatus.NOT_APPLICABLE,
            basis=_not_computed(definition, "share basis mismatch between the price and the EPS"),
            periods=periods,
            notes=(mismatch, *UNSUPPORTED_GAPS),
            inputs=inputs,
        )

    eps_currency = eps.value.currency
    if eps_currency is None or not eps_currency.strip():
        return _result(
            key,
            status=CalculationStatus.MISSING_INPUTS,
            basis=_not_computed(definition, "the EPS currency is unknown"),
            periods=periods,
            notes=(
                "missing input: the EPS fact states no currency, so it cannot be compared "
                "with the price currency",
                *UNSUPPORTED_GAPS,
            ),
            inputs=inputs,
        )

    converted = eps_amount
    conversion_note: str | None = None
    if eps_currency != price.currency:
        expected_pair = f"{eps_currency}/{price.currency}"
        if fx is None:
            return _result(
                key,
                status=CalculationStatus.MISSING_INPUTS,
                basis=_not_computed(
                    definition,
                    f"EPS is stated in {eps_currency} while the price is quoted in "
                    f"{price.currency} and no FX record was injected",
                ),
                periods=periods,
                notes=(
                    "missing input: no FX record for "
                    f"{expected_pair}; the convenience reading conversion of a filing and any "
                    "fixed or 1.0 rate are not valuation-date rates and are not applied",
                    DISABLED_REASONS[1],
                    *UNSUPPORTED_GAPS,
                ),
                inputs=inputs,
            )
        precision = fx.source_date_precision.strip().lower()
        if precision not in FX_DATED_PRECISIONS:
            return _result(
                key,
                status=CalculationStatus.MISSING_INPUTS,
                basis=_not_computed(
                    definition,
                    "the FX record's source date precision "
                    f"{fx.source_date_precision!r} does not establish a dated rate",
                ),
                periods=periods,
                notes=(
                    "missing input: the FX record has no usable quote date "
                    f"(source_date_precision {fx.source_date_precision!r}); a rate of unknown "
                    "date is not used for a dated valuation",
                    DISABLED_REASONS[1],
                    *UNSUPPORTED_GAPS,
                ),
                inputs=inputs,
            )
        if fx.pair.strip() != expected_pair:
            return _result(
                key,
                status=CalculationStatus.MISSING_INPUTS,
                basis=_not_computed(
                    definition,
                    f"the FX pair {fx.pair!r} is not {expected_pair!r}",
                ),
                periods=periods,
                notes=(
                    f"the injected FX pair {fx.pair!r} does not convert {eps_currency} into "
                    f"{price.currency}; the required pair is {expected_pair!r} (quote currency "
                    "as the numerator of the rate)",
                    DISABLED_REASONS[1],
                    *UNSUPPORTED_GAPS,
                ),
                inputs=inputs,
            )
        if not fx.rate.is_finite() or fx.rate <= 0:
            return _result(
                key,
                status=CalculationStatus.MISSING_INPUTS,
                basis=_not_computed(definition, f"the FX rate {fx.rate} is not usable"),
                periods=periods,
                notes=(
                    f"the injected FX rate {fx.rate} is not a positive finite decimal",
                    DISABLED_REASONS[1],
                    *UNSUPPORTED_GAPS,
                ),
                inputs=inputs,
            )
        lag = abs((price.trade_date - fx.as_of).days)
        if lag > FX_MAX_LAG_DAYS:
            return _result(
                key,
                status=CalculationStatus.STALE_INPUTS,
                basis=_not_computed(
                    definition,
                    f"the FX record of {fx.as_of.isoformat()} is {lag} days from the price "
                    f"trade date {price.trade_date.isoformat()} (limit {FX_MAX_LAG_DAYS})",
                ),
                periods=periods,
                notes=(
                    f"the FX rate is dated {fx.as_of.isoformat()}, {lag} days from the price "
                    f"trade date {price.trade_date.isoformat()}; the older rate is not silently "
                    "carried over to another day",
                    DISABLED_REASONS[1],
                    *UNSUPPORTED_GAPS,
                ),
                inputs=inputs,
            )
        with localcontext() as context:
            context.prec = _DIVISION_PRECISION
            converted = eps_amount * fx.rate
        conversion_note = (
            f"EPS converted from {eps_currency} to {price.currency} at the injected rate "
            f"{fx.rate} ({fx.pair}) as of {fx.as_of.isoformat()}"
        )

    with localcontext() as context:
        context.prec = _DIVISION_PRECISION
        ratio = price.close / converted
    notes = (conversion_note, INJECTED_NOTE) if conversion_note is not None else (INJECTED_NOTE,)
    return _result(
        key,
        status=CalculationStatus.OK,
        basis=(
            f"{key}[{_period_label(eps.period)}] = {price.close} {price.currency} close on "
            f"{price.trade_date.isoformat()} / {eps.metric} "
            f"{_plain(eps_amount)} {eps_currency} per share"
            + (f" ({conversion_note})" if conversion_note is not None else "")
            + f"; {_share_basis_text(profile, eps, price)}; annual fiscal-year EPS, not a "
            "trailing four-quarter figure"
        ),
        value=Quantity(value=ratio, unit="ratio"),
        periods=periods,
        notes=(*notes, *UNSUPPORTED_GAPS),
        inputs=inputs,
    )


def ads_pe_gap(profile: CompanyProfile, *, fx: FxInput | None) -> tuple[str, ...]:
    """Why no ADS PE is produced for an ADS listing, as an explicit gap list.

    Covers the ratio (and its source), the ratio's effective window, the same-date
    FX record and the share basis. An empty tuple is returned for a profile that is
    not an ADS listing: there is no ADS PE to produce, so there is nothing to gap.
    The ratio text states whether the controlled identity list carries one; in the
    first release the list's ratio is not applied either way, because the profile
    does not carry the filing evidence or the ratio's effective window.
    """

    if not profile.is_ads:
        return ()
    ratio = (profile.ads_ratio or "").strip()
    if not ratio:
        ratio_line = (
            "ADS ratio: the controlled identity list carries no ordinary-shares-per-ADS "
            "ratio for this listing, so no ADS PE can be derived"
        )
    else:
        ratio_line = (
            f"ADS ratio: {ratio} ordinary shares per ADS comes from the controlled identity "
            "list (the issuer's own filing); the filing evidence and the ratio's effective "
            "window are not part of the profile, so the ratio is not applied"
        )
    validity_line = (
        "ADS ratio validity: no verified effective window for the ratio at the price date"
    )
    if fx is None:
        fx_line = (
            "same-date FX: no FX record with a dated rate was injected; the convenience "
            "reading conversion of a filing is not a valuation-date rate"
        )
    else:
        fx_line = (
            f"same-date FX: the injected record ({fx.pair} as of {fx.as_of.isoformat()}) still "
            "comes from no accepted daily rate source in the first release and is not used"
        )
    basis_line = (
        "share basis: the ordinary-share EPS period, the ADS ratio and the quoted ADS price "
        "have not been verified together on one share basis at one date"
    )
    return (ratio_line, validity_line, fx_line, basis_line)


# --- selection and text helpers -------------------------------------------------


def _key_for(eps: FactRecord) -> str:
    if eps.semantics.per_share_basis == PerShareBasis.BASIC:
        return "annual_pe_basic"
    if eps.semantics.per_share_basis == PerShareBasis.DILUTED:
        return "annual_pe_diluted"
    return "annual_pe"


def _definition(key: str) -> str:
    return (
        f"{key} = the trade-date close divided by the annual fiscal-year EPS of the same "
        "per-share basis, both on one share basis"
    )


def _not_computed(definition: str, reason: str) -> str:
    return f"{definition}; not computed: {reason}"


def _eps_amount(eps: FactRecord) -> Decimal | None:
    raw = eps.value.raw_value
    if raw is None:
        return None
    try:
        parsed = Decimal(raw)
    except InvalidOperation:
        return None
    if not parsed.is_finite():
        return None
    return parsed * eps.value.scale


def _split_events(profile: CompanyProfile) -> tuple[tuple[date, int], ...]:
    key = SPLIT_EVENT_KEYS.get(profile.company_key, profile.company_key)
    events = SPLIT_EVENTS.get(key) or SPLIT_EVENTS.get(key.upper())
    if events is None:
        return ()
    return tuple((date.fromisoformat(day), ratio) for day, ratio in events)


def _split_mismatch(profile: CompanyProfile, eps: FactRecord, price: PriceInput) -> str | None:
    """The share-basis rejection for a recorded split, or ``None`` when comparable.

    Two incomparable cases in the first release:

    * the split falls between the EPS period end and the price date — the price
      (restated or as traded) is on the post-split count, the EPS is not;
    * the price series is split adjusted and the EPS period ends before a split
      that happened at any time — a split-adjusted close is stated on the newest
      share count, whatever date it is quoted for (the recorded 2020-08-28 Yahoo
      close is the documented case).

    A period that ends on or after the split date is already stated on the
    post-split count and stays comparable.
    """

    for split_date, ratio in _split_events(profile):
        if eps.period.end >= split_date:
            continue
        detail = (
            f"the price is stated as {price.share_basis!r} "
            f"(is_split_adjusted={price.is_split_adjusted}) while the EPS carries the "
            "pre-split share count of its own period"
        )
        if split_date <= price.trade_date:
            return (
                "share basis mismatch: split between EPS period and price date — the "
                f"{ratio}-for-1 split on {split_date.isoformat()} falls between the EPS period "
                f"end {eps.period.end.isoformat()} and the price date "
                f"{price.trade_date.isoformat()}; {detail}"
            )
        if price.is_split_adjusted:
            return (
                "share basis mismatch: the split-adjusted price series is stated on the "
                f"{ratio}-for-1 post-split share count of {split_date.isoformat()} while the "
                f"EPS period ended {eps.period.end.isoformat()}, before that split; {detail}"
            )
    return None


def _share_basis_text(profile: CompanyProfile, eps: FactRecord, price: PriceInput) -> str:
    events = _split_events(profile)
    if not events:
        return (
            f"no split event is recorded for {profile.company_key} in this release, so the "
            f"{price.share_basis!r} price and the EPS period are taken as comparable"
        )
    dates = ", ".join(split_date.isoformat() for split_date, _ in events)
    post_split = [split_date for split_date, _ in events if eps.period.end >= split_date]
    if post_split:
        return (
            f"the EPS period end {eps.period.end.isoformat()} is on or after the recorded split "
            f"date(s) {', '.join(day.isoformat() for day in post_split)}, so this EPS is stated "
            "on the post-split share count"
        )
    return (
        f"the EPS period end {eps.period.end.isoformat()} is before every recorded split date "
        f"({dates}) and the {price.share_basis!r} price is not restated across a split, so both "
        "sides are on the same pre-split share count"
    )


def _period_label(period: FactPeriod) -> str:
    return period.label or period.end.isoformat()


def _input_refs(
    *, price: PriceInput | None, eps: FactRecord, fx: FxInput | None
) -> tuple[str, ...]:
    refs = [f"{eps.metric}@{eps.period.end.isoformat()}"]
    if price is not None:
        refs.append(f"close@{price.trade_date.isoformat()}")
    if fx is not None:
        refs.append(f"fx@{fx.as_of.isoformat()}")
    return tuple(refs)


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _result(
    key: str,
    *,
    status: CalculationStatus,
    basis: str,
    value: Quantity | None = None,
    periods: tuple[FactPeriod, ...] = (),
    notes: tuple[str, ...] = (),
    inputs: tuple[str, ...] = (),
) -> MetricResult:
    if not basis:
        raise ValueError(f"metric {key} requires a non-empty basis")
    return MetricResult(
        key=key,
        label=LABELS[key],
        status=status,
        value=value,
        basis=basis,
        periods=periods,
        notes=notes,
        inputs=inputs,
    )

"""Point-in-time snapshot view: the only data entry for factors (P1-B1).

A :class:`SnapshotView` is an immutable, read-only picture of one listing on
``as_of`` under one provider policy (decision D6):

* ``TARGET_POOL`` reads baostock bars and resolves every financial field
  through the field source registry (P1-A12, ``config/field_sources.yaml``):
  iFinD-verified value > primary > fallback(s), over baostock, AkShare
  (Eastmoney) and verified iFinD rows of the same report period;
* ``OBSERVATION`` reads iFinD rows only (watchlist / candidate depth, golden
  corpus parity, decision D3); no resolution.

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
3. the chosen value is ``conflict`` when another free source differs beyond
   the field's ``tolerance`` (approximate sources excluded), when a collector
   recorded a ``provider_conflicts`` entry, or when iFinD marked it
   ``conflict``; else ``unverified``.

Each resolved field carries a :class:`FieldResolution` (``provider``,
``verification``, rejected values, conflicts) that factors copy into
``FactorValue.inputs_ref``. ``derived`` registry fields are computed from
resolved fields (``approx``).

Point-in-time rules (enforced by every constructor):

* market bars: ``trade_date <= as_of``;
* financial periods: public by ``as_of`` (``available_from <= as_of``,
  ``unknown`` availability excluded) - the pubDate gating of
  :meth:`FinancialSnapshotRepository.latest_available`;
* report-overdue records open on ``as_of``;
* dividend events (baostock, F2 / Q2): public by ``as_of``
  (``available_from <= as_of``) with ``ex_date <= as_of``, plus the ex-date
  years whose collection is known by ``as_of`` (``dividend_coverage``), so a
  factor can tell "no dividend" from "not collected".

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

Two ways to build a view: :func:`load_snapshot_view` (database, async) and
:meth:`SnapshotView.build` (pure, in memory - tests, golden corpus);
:func:`ifind_research_view` builds the observation view from decoded iFinD
research rows (golden corpus parity, decision D3).

This module is the only place in the factor layer that touches snapshot
repositories, so a repository signature change is a one-file change.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.factors.field_sources import (
    VERIFIER,
    FieldSource,
    FieldSourceRegistry,
    Formula,
    Verification,
    default_field_sources,
    relative_difference,
)
from investment_agent.factors.industry_groups import (
    IndustryGroupMap,
    IndustryGroupMatch,
    default_industry_groups,
    usable_sw_code,
)
from investment_agent.factors.metrics import parse_number, percent_to_fraction
from investment_agent.providers.shenwan_directory import (
    SW2021_SYSTEM,
    ShenwanDirectory,
    default_shenwan_directory,
)
from investment_agent.storage.models.pit_snapshot import (
    DividendEventSnapshot,
    FinancialSnapshot,
    MarketSnapshot,
)
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pit import end_of_business_day_utc
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository

ProviderName = Literal["baostock", "ifind"]


@dataclass(frozen=True)
class ProviderPolicy:
    """Which provider's snapshots a view reads (decision D6).

    ``provider`` is the anchor: its bars and report periods define the view.
    ``sources`` (non-empty) turns on field resolution over those providers'
    financial rows with the field source registry (P1-A12).
    """

    name: str
    provider: ProviderName
    sources: tuple[str, ...] = ()

    @property
    def resolves(self) -> bool:
        return bool(self.sources)


TARGET_POOL = ProviderPolicy("target_pool", "baostock", sources=("baostock", "akshare", "ifind"))
OBSERVATION = ProviderPolicy("observation", "ifind")

# Price history loaded for the 5-year valuation percentiles (plus a margin so
# the window start is covered) and for the trailing one-year statistics.
PRICE_HISTORY_DAYS = 5 * 366 + 7
ADJUSTED_HISTORY_DAYS = 400
# Dividend events loaded for the trailing-365-day dividend yield.
DIVIDEND_HISTORY_DAYS = 366

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
class Bar:
    """One daily bar. Valuation / status columns are baostock-only."""

    trade_date: date
    close: float
    pe_ttm: float | None = None
    pb_mrq: float | None = None
    ps_ttm: float | None = None
    turnover: float | None = None
    trade_status: int | None = None
    is_st: bool | None = None
    total_shares: float | None = None
    """iFinD rows only (baostock market cap uses financial total shares)."""
    ref: str | None = None


@dataclass(frozen=True)
class Dividend:
    """One distribution as usable on ``as_of`` (``dividend_event`` latest version)."""

    ex_date: date
    cash_per_share: float | None
    """Pre-tax cash per share on the share base before the event."""
    share_multiplier: float = 1.0
    """Shares after / before the event (1 + bonus + capitalisation shares per share)."""
    available_from: date | None = None
    ref: str | None = None


@dataclass(frozen=True)
class DividendHistory:
    """Dividend events of one listing plus the collected ex-date years."""

    events: tuple[Dividend, ...] = ()
    """Ascending by ex-date."""
    covered_years: Mapping[int, date] = field(default_factory=dict)
    """Ex-date year -> date (Asia/Shanghai) of the latest collection of that year."""
    provider: str = "baostock"

    def describe_coverage(self) -> dict[str, str]:
        return {str(year): day.isoformat() for year, day in sorted(self.covered_years.items())}


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
class FinancialPeriod:
    """One report period's factor-layer fields as usable on ``as_of``."""

    report_period: date
    period_kind: str
    available_from: date | None
    availability: str
    fields: Mapping[str, float | None]
    ref: str | None = None
    sources: Mapping[str, FieldResolution] = field(default_factory=dict)
    """Per resolved field: provider, verification, rejected values (P1-A12).
    Empty for unresolved (single-provider) periods."""

    def value(self, name: str) -> float | None:
        if name in AMBIGUOUS_STORED_FIELDS:
            raise KeyError(f"{name!r} is ambiguous across providers; use a factor-layer name")
        return self.fields.get(name)

    def source(self, name: str) -> FieldResolution | None:
        return self.sources.get(name)

    def describe(self, fields: Iterable[str] = ()) -> dict[str, Any]:
        result: dict[str, Any] = {
            "report_period": self.report_period.isoformat(),
            "period_kind": self.period_kind,
            "available_from": self.available_from.isoformat() if self.available_from else None,
            "availability": self.availability,
            "ref": self.ref,
        }
        sources = {
            name: resolution.describe()
            for name in dict.fromkeys(fields)
            if (resolution := self.sources.get(name)) is not None
        }
        if sources:
            result["sources"] = sources
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
    if chosen is None:
        return None, FieldResolution(None, "unverified", rejected=tuple(rejected))

    provider, value, candidate = chosen
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
    if verifier is not None and verifier.verification.get(name) == "conflict":
        conflicts.append({"provider": VERIFIER, "verification": "conflict"})
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


@dataclass(frozen=True)
class SnapshotView:
    ticker: str
    as_of: date
    policy: ProviderPolicy
    industry_code: str | None
    industry_name: str | None
    industry_group: str
    raw_bars: tuple[Bar, ...]
    """Unadjusted daily bars, ascending, all ``<= as_of``."""
    adjusted_bars: tuple[Bar, ...]
    """Return-series bars (baostock: backward-adjusted), ascending, ``<= as_of``."""
    periods: tuple[FinancialPeriod, ...]
    """One row per report period public on ``as_of``, ascending by period."""
    overdue_periods: tuple[date, ...] = ()
    adjusted_kind: str = "backward"
    industry_groups_version: str = field(default="")
    sw_code: str | None = None
    """Shenwan 2021 code that decided the group (``None`` -> CSRC fallback)."""
    industry_match: IndustryGroupMatch | None = None
    dividends: DividendHistory | None = None
    """``None``: dividend data not loaded for this view (e.g. observation path)."""

    @property
    def industry_fallback(self) -> bool:
        """The group came from the CSRC fallback table (flag ``industry_fallback``)."""

        return self.industry_match is not None and self.industry_match.fallback

    @property
    def provider(self) -> ProviderName:
        return self.policy.provider

    # -- constructors ----------------------------------------------------- #

    @classmethod
    def build(
        cls,
        *,
        ticker: str,
        as_of: date,
        policy: ProviderPolicy = TARGET_POOL,
        industry_code: str | None = None,
        industry_name: str | None = None,
        raw_bars: Iterable[Bar] = (),
        adjusted_bars: Iterable[Bar] = (),
        periods: Iterable[FinancialPeriod] = (),
        overdue_periods: Iterable[date] = (),
        adjusted_kind: str = "backward",
        industry_groups: IndustryGroupMap | None = None,
        sw_code: str | None = None,
        field_sources: FieldSourceRegistry | None = None,
        dividends: DividendHistory | None = None,
    ) -> SnapshotView:
        """Pure in-memory constructor; applies the point-in-time rules.

        Bars after ``as_of`` and periods not public by ``as_of`` are dropped;
        for a period given more than once the last one wins (later capture).
        ``sw_code`` is the Shenwan 2021 code valid on ``as_of`` (already vetted
        with :func:`usable_sw_code`); ``industry_code`` / ``industry_name``
        are the CSRC fallback. Under a resolving policy, a period given
        without ``sources`` is resolved as the anchor provider's only row
        (registry sanity rules apply; see :func:`resolve_period_fields`).
        Dividend events with an ex-date after ``as_of`` or not public by
        ``as_of`` are dropped.
        """

        groups = industry_groups or default_industry_groups()
        match = groups.resolve_classification(
            sw_code=sw_code, csrc_code=industry_code, csrc_name=industry_name
        )
        by_period: dict[date, FinancialPeriod] = {}
        for period in periods:
            if period.availability == "unknown" or period.available_from is None:
                continue
            if period.available_from > as_of:
                continue
            if policy.resolves and not period.sources:
                fields, sources = resolve_period_fields(
                    [ProviderFields(policy.provider, period.fields, period.ref)],
                    anchor=policy.provider,
                    registry=field_sources,
                )
                period = replace(period, fields=fields, sources=sources)
            by_period[period.report_period] = period
        return cls(
            ticker=ticker,
            as_of=as_of,
            policy=policy,
            industry_code=industry_code,
            industry_name=industry_name,
            industry_group=match.group,
            raw_bars=_bars_until(raw_bars, as_of),
            adjusted_bars=_bars_until(adjusted_bars, as_of),
            periods=tuple(by_period[key] for key in sorted(by_period)),
            overdue_periods=tuple(sorted(set(overdue_periods))),
            adjusted_kind=adjusted_kind,
            industry_groups_version=groups.version,
            sw_code=sw_code if match.system == SW2021_SYSTEM else None,
            industry_match=match,
            dividends=_dividends_until(dividends, as_of),
        )

    # -- accessors -------------------------------------------------------- #

    def latest_raw_bar(self) -> Bar | None:
        return self.raw_bars[-1] if self.raw_bars else None

    def latest_report(self) -> FinancialPeriod | None:
        return self.periods[-1] if self.periods else None

    def annual_periods(self) -> tuple[FinancialPeriod, ...]:
        return tuple(period for period in self.periods if period.period_kind == "annual")

    def latest_annual(self) -> FinancialPeriod | None:
        annual = self.annual_periods()
        return annual[-1] if annual else None

    def annual(self, year: int) -> FinancialPeriod | None:
        for period in self.annual_periods():
            if period.report_period.year == year:
                return period
        return None

    def total_shares(self) -> tuple[float, dict[str, Any]] | None:
        """Total shares usable on ``as_of`` and where they come from.

        baostock: ``total_shares`` of the latest report public on ``as_of``
        (effective from its pubDate). iFinD: the latest bar's total shares,
        falling back to the latest report.
        """

        if self.provider == "ifind":
            bar = self.latest_raw_bar()
            if bar is not None and bar.total_shares is not None and bar.total_shares > 0:
                return bar.total_shares, {
                    "source": "market_snapshot.total_shares",
                    "trade_date": bar.trade_date.isoformat(),
                    "ref": bar.ref,
                }
        report = self.latest_report()
        if report is None:
            return None
        shares = report.value("total_shares")
        if shares is None or shares <= 0:
            return None
        return shares, {
            "source": "financial_snapshot.total_shares",
            **report.describe(("total_shares",)),
        }

    def with_overdue(self, periods: Iterable[date]) -> SnapshotView:
        return replace(self, overdue_periods=tuple(sorted(set(periods))))


def _dividends_until(history: DividendHistory | None, as_of: date) -> DividendHistory | None:
    if history is None:
        return None
    by_ex = {
        event.ex_date: event
        for event in history.events
        if event.ex_date <= as_of
        and (event.available_from is None or event.available_from <= as_of)
    }
    return replace(history, events=tuple(by_ex[key] for key in sorted(by_ex)))


def _bars_until(bars: Iterable[Bar], as_of: date) -> tuple[Bar, ...]:
    by_date = {bar.trade_date: bar for bar in bars if bar.trade_date <= as_of}
    return tuple(by_date[key] for key in sorted(by_date))


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


def _decimal(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _bar_from_row(row: MarketSnapshot) -> Bar:
    return Bar(
        trade_date=row.trade_date,
        close=float(row.close),
        pe_ttm=_decimal(row.pe_ttm),
        pb_mrq=_decimal(row.pb_mrq),
        ps_ttm=_decimal(row.ps_ttm),
        turnover=_decimal(row.turnover),
        trade_status=row.trade_status,
        is_st=row.is_st,
        total_shares=_decimal(row.total_shares),
        ref=f"market_snapshot:{row.id}",
    )


async def _market_series(
    session: AsyncSession,
    listing_id: int,
    start: date,
    end: date,
    knowledge_cutoff: datetime,
    *,
    adjust: str,
    provider: str,
) -> list[MarketSnapshot]:
    repository = MarketSnapshotRepository(session)
    # Backfilled bars are visible by trade_date even though captured later:
    # prices are never restated (see MarketSnapshotRepository.series_as_of).
    return await repository.series_as_of(
        listing_id,
        start,
        end,
        knowledge_cutoff,
        adjust=adjust,
        provider=provider,
        include_backfill=True,
    )


async def _available_periods(
    session: AsyncSession,
    listing_id: int,
    as_of: date,
    *,
    providers: Sequence[str],
    include_backfill: bool = True,
) -> list[FinancialSnapshot]:
    """Every report period public and known on ``as_of``; latest version each.

    Same eligibility as :meth:`FinancialSnapshotRepository.latest_available`
    (``available_from <= as_of``, ``unknown`` excluded, captured by the end of
    ``as_of`` unless backfilled), but for all periods instead of the latest.
    The latest version is kept per ``(report period, provider, part)``:
    AkShare stores its period summaries and cash-flow statements as separate
    rows (``fields.part``).
    """

    known = FinancialSnapshot.captured_at < end_of_business_day_utc(as_of)
    knowledge = or_(known, FinancialSnapshot.is_backfill.is_(True)) if include_backfill else known
    stmt = (
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.listing_id == listing_id,
            FinancialSnapshot.provider.in_(list(providers)),
            FinancialSnapshot.availability != "unknown",
            FinancialSnapshot.available_from.is_not(None),
            FinancialSnapshot.available_from <= as_of,
            knowledge,
        )
        .order_by(FinancialSnapshot.report_period, FinancialSnapshot.version)
    )
    latest: dict[tuple[date, str, str], FinancialSnapshot] = {}
    for row in (await session.execute(stmt)).scalars():
        part = row.fields.get("part") if isinstance(row.fields, dict) else None
        latest[(row.report_period, row.provider, str(part or ""))] = row
    return [latest[key] for key in sorted(latest)]


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


def _financial_periods(
    rows: Sequence[FinancialSnapshot],
    policy: ProviderPolicy,
    registry: FieldSourceRegistry | None,
) -> list[FinancialPeriod]:
    if not policy.resolves:
        return [
            FinancialPeriod(
                report_period=row.report_period,
                period_kind=row.period_kind,
                available_from=row.available_from,
                availability=row.availability,
                fields=factor_fields(row.provider, row.fields),
                ref=f"financial_snapshot:{row.id}",
            )
            for row in rows
            if row.provider == policy.provider
        ]
    grouped: dict[date, dict[str, list[FinancialSnapshot]]] = {}
    for row in rows:
        grouped.setdefault(row.report_period, {}).setdefault(row.provider, []).append(row)
    periods: list[FinancialPeriod] = []
    for report_period in sorted(grouped):
        by_provider = grouped[report_period]
        anchor_rows = by_provider.get(policy.provider)
        if not anchor_rows:
            continue  # the anchor (baostock pubDate) decides when a period is public
        anchor = anchor_rows[-1]
        fields, sources = resolve_period_fields(
            [_provider_fields(provider, items) for provider, items in by_provider.items()],
            anchor=policy.provider,
            registry=registry,
        )
        periods.append(
            FinancialPeriod(
                report_period=report_period,
                period_kind=anchor.period_kind,
                available_from=anchor.available_from,
                availability=anchor.availability,
                fields=fields,
                ref=f"financial_snapshot:{anchor.id}",
                sources=sources,
            )
        )
    return periods


def _shanghai_date(value: datetime) -> date:
    """Stored UTC timestamp (SQLite returns it naive) -> Asia/Shanghai date."""

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(SHANGHAI_TZ).date()


def _dividend_from_row(row: DividendEventSnapshot) -> Dividend:
    extra = (row.stock_per_share or Decimal(0)) + (row.reserve_per_share or Decimal(0))
    return Dividend(
        ex_date=row.ex_date,
        cash_per_share=_decimal(row.cash_per_share_pre_tax),
        share_multiplier=1.0 + float(extra),
        available_from=row.available_from,
        ref=f"dividend_event:{row.id}",
    )


async def load_dividend_history(
    session: AsyncSession, listing_id: int, as_of: date, *, provider: str = "baostock"
) -> DividendHistory:
    """Events of the trailing :data:`DIVIDEND_HISTORY_DAYS` and the covered years
    (both as known on ``as_of``)."""

    start = as_of - timedelta(days=DIVIDEND_HISTORY_DAYS)
    repository = DividendRepository(session)
    rows = await repository.events_as_of(listing_id, start, as_of, provider=provider)
    coverage = await repository.coverage_as_of(
        listing_id, list(range(start.year, as_of.year + 1)), as_of, provider=provider
    )
    return DividendHistory(
        events=tuple(_dividend_from_row(row) for row in rows),
        covered_years={year: _shanghai_date(row.last_checked_at) for year, row in coverage.items()},
        provider=provider,
    )


async def load_snapshot_view(
    session: AsyncSession,
    ticker: str,
    as_of: date,
    *,
    policy: ProviderPolicy = TARGET_POOL,
    industry_groups: IndustryGroupMap | None = None,
    shenwan_directory: ShenwanDirectory | None = None,
    field_sources: FieldSourceRegistry | None = None,
) -> SnapshotView:
    """Read-only point-in-time view of ``ticker`` on ``as_of`` from the database.

    Knowledge time is the end of ``as_of`` (Asia/Shanghai): rows captured
    later are invisible (backfilled financial rows excepted, as in
    ``latest_available``). The industry group comes from the ``sw2021``
    classification valid on ``as_of``, else from ``csrc`` (P1-B3). Under a
    resolving policy (``TARGET_POOL``) financial fields are resolved across
    the policy's sources with the field source registry (P1-A12).
    """

    listing = await ListingRepository(session).get_a_share_listing(ticker)
    if listing is None:
        raise LookupError(f"no listing for {ticker}")
    knowledge_cutoff = end_of_business_day_utc(as_of) - timedelta(microseconds=1)
    industries = IndustryRepository(session)
    industry = await industries.industry_as_of(listing.id, as_of)
    shenwan = await industries.industry_as_of(listing.id, as_of, system=SW2021_SYSTEM)
    sw_code = (
        usable_sw_code(
            shenwan.code,
            shenwan.valid_from,
            shenwan.valid_to,
            shenwan_directory or default_shenwan_directory(),
            shenwan.name,
        )
        if shenwan is not None
        else None
    )
    raw_rows = await _market_series(
        session,
        listing.id,
        as_of - timedelta(days=PRICE_HISTORY_DAYS),
        as_of,
        knowledge_cutoff,
        adjust="none",
        provider=policy.provider,
    )
    adjusted_rows = await _market_series(
        session,
        listing.id,
        as_of - timedelta(days=ADJUSTED_HISTORY_DAYS),
        as_of,
        knowledge_cutoff,
        adjust="backward",
        provider=policy.provider,
    )
    financial_rows = await _available_periods(
        session, listing.id, as_of, providers=policy.sources or (policy.provider,)
    )
    overdue = await ReportOverdueRepository(session).open_as_of(listing.id, as_of)
    dividends = (
        await load_dividend_history(session, listing.id, as_of, provider=policy.provider)
        if policy.provider == "baostock"
        else None
    )
    return SnapshotView.build(
        ticker=ticker,
        as_of=as_of,
        policy=policy,
        industry_code=industry.code if industry else None,
        industry_name=industry.name if industry else None,
        raw_bars=[_bar_from_row(row) for row in raw_rows],
        adjusted_bars=[_bar_from_row(row) for row in adjusted_rows],
        periods=_financial_periods(financial_rows, policy, field_sources),
        overdue_periods=[record.report_period for record in overdue],
        industry_groups=industry_groups,
        sw_code=sw_code,
        field_sources=field_sources,
        dividends=dividends,
    )


# --------------------------------------------------------------------------- #
# iFinD research rows (observation path, golden corpus)
# --------------------------------------------------------------------------- #

# (request family, source field) -> (factor-layer field, percent?)
_IFIND_PROFITABILITY_FIELDS: dict[str, tuple[str, bool]] = {
    "ths_roe_stock": ("roe", True),
    "ths_gross_selling_rate_stock": ("gross_margin", True),
    "ths_net_sales_rate_stock": ("net_margin", True),
    "ths_eps_basic_stock": ("eps_basic_fy", False),
    "ths_nav_ps_stock": ("bvps_fy", False),
    "ths_revenue_ps_stock": ("revenue_ps_fy", False),
}
_IFIND_INCOME_FIELDS: dict[str, str] = {
    "ths_operating_total_revenue_stock": "revenue_total",
    "ths_np_atoopc_stock": "net_profit_parent",
}
_IFIND_CASH_FIELDS: dict[str, str] = {
    "ths_ncf_from_oa_stock": "operating_cash_flow",
    "ths_cash_paid_for_assets_stock": "capital_expenditure",
}
_IFIND_CAPITAL_FIELDS: dict[str, tuple[str, bool]] = {
    "ths_asset_liab_ratio_stock": ("debt_ratio", True),
}


def _first(rows: Sequence[Mapping[str, str]] | None) -> Mapping[str, str] | None:
    return rows[0] if rows else None


def ifind_research_view(
    *,
    ticker: str,
    as_of: date,
    report_year: int,
    rows: Mapping[str, Sequence[Mapping[str, str]]],
    industry_code: str | None = None,
    industry_name: str | None = None,
    industry_groups: IndustryGroupMap | None = None,
) -> SnapshotView:
    """Observation view from decoded iFinD research rows (``plan_ifind_requests`` keys).

    ``rows`` maps request names (``info``, ``history``, ``income_latest``,
    ``income_base``, ``cash_latest``, ``capital_latest``,
    ``profitability_<year>``) to decoded rows. The fiscal-year periods are
    treated as public on ``as_of`` (``availability="approx"``): the research
    request names the report year explicitly. History rows are the provider's
    (forward-adjusted) series; the last close equals the raw close, so it
    serves both as the raw and the return series.
    """

    info = _first(rows.get("info")) or {}
    history = sorted(rows.get("history") or [], key=lambda row: str(row.get("time", "")))
    total_shares = parse_number(info.get("ths_total_shares_stock"))
    bars: list[Bar] = []
    for row in history:
        time_text = str(row.get("time", ""))
        try:
            trade_date = datetime.strptime(time_text, "%Y%m%d").date()
        except ValueError:
            continue
        close = parse_number(row.get("close"))
        if close is None:
            continue
        bars.append(
            Bar(
                trade_date=trade_date,
                close=close,
                ref=f"ifind:/stock/history:{time_text}",
            )
        )
    if bars and total_shares is not None:
        bars[-1] = replace(bars[-1], total_shares=total_shares)

    def convert(row: Mapping[str, str], spec: Mapping[str, tuple[str, bool]]) -> dict[str, Any]:
        return {
            target: (percent_to_fraction if percent else parse_number)(row.get(source))
            for source, (target, percent) in spec.items()
        }

    periods: list[FinancialPeriod] = []
    for year in range(report_year - 3, report_year + 1):
        fields: dict[str, float | None] = {}
        refs: list[str] = []
        profitability = _first(rows.get(f"profitability_{year}"))
        if profitability is not None:
            fields.update(convert(profitability, _IFIND_PROFITABILITY_FIELDS))
            refs.append(f"profitability_{year}")
        income_key = (
            "income_latest"
            if year == report_year
            else "income_base"
            if year == report_year - 3
            else None
        )
        income = _first(rows.get(income_key)) if income_key else None
        if income is not None:
            fields.update(
                {
                    target: parse_number(income.get(source))
                    for source, target in _IFIND_INCOME_FIELDS.items()
                }
            )
            refs.append(str(income_key))
        if year == report_year:
            cash = _first(rows.get("cash_latest"))
            if cash is not None:
                fields.update(
                    {
                        target: parse_number(cash.get(source))
                        for source, target in _IFIND_CASH_FIELDS.items()
                    }
                )
                refs.append("cash_latest")
            capital = _first(rows.get("capital_latest"))
            if capital is not None:
                fields.update(convert(capital, _IFIND_CAPITAL_FIELDS))
                refs.append("capital_latest")
        if not refs:
            continue
        periods.append(
            FinancialPeriod(
                report_period=date(year, 12, 31),
                period_kind="annual",
                available_from=as_of,
                availability="approx",
                fields=fields,
                ref="ifind:" + "+".join(refs),
            )
        )
    return SnapshotView.build(
        ticker=ticker,
        as_of=as_of,
        policy=OBSERVATION,
        industry_code=industry_code,
        industry_name=industry_name,
        raw_bars=bars,
        adjusted_bars=bars,
        periods=periods,
        adjusted_kind="forward",
        industry_groups=industry_groups,
    )

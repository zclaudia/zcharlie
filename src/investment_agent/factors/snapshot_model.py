"""Point-in-time snapshot view model: the only data entry for factors (P1-B1).

A :class:`SnapshotView` is an immutable, read-only picture of one listing on
``as_of`` under one provider policy (decision D6):

* ``TARGET_POOL`` reads baostock bars and resolves every financial field
  through the field source registry (P1-A12, ``config/field_sources.yaml``):
  iFinD-verified value > primary > fallback(s), over baostock, AkShare
  (Eastmoney) and verified iFinD rows of the same report period;
* ``OBSERVATION`` reads iFinD rows only (watchlist / candidate depth, golden
  corpus parity, decision D3); no resolution.

This module holds the pure view model: the policy, the value objects and
:meth:`SnapshotView.build`, the in-memory constructor. The resolution
algorithm lives in :mod:`investment_agent.factors.field_resolution`, the
database loaders in :mod:`investment_agent.factors.view_db` and the iFinD
observation builder in :mod:`investment_agent.factors.ifind_view`; none of
them is imported here, so this module stays free of database dependencies.

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
meaning are renamed in :mod:`investment_agent.factors.field_resolution` and
only there; the stored snapshot JSON keeps its names. Factors only see the
factor-layer names:

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

Ways to build a view: :meth:`SnapshotView.build` (pure, in memory - tests,
golden corpus), :func:`investment_agent.factors.view_db.load_snapshot_view`
(database, async) and
:func:`investment_agent.factors.ifind_view.ifind_research_view` (decoded
iFinD research rows, golden corpus parity, decision D3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal

from investment_agent.factors.field_resolution import (
    AMBIGUOUS_STORED_FIELDS,
    FieldResolution,
    ProviderFields,
    resolve_period_fields,
)
from investment_agent.factors.field_sources import FieldSourceRegistry
from investment_agent.factors.industry_groups import (
    IndustryGroupMap,
    IndustryGroupMatch,
    default_industry_groups,
)
from investment_agent.providers.shenwan_directory import SW2021_SYSTEM

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


@dataclass(frozen=True)
class Bar:
    """One daily bar. Valuation / status columns are baostock-only (``pe_ttm``
    and ``turnover`` also come with a Tencent close quote)."""

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
    provider: str | None = None
    """Source of this bar when read through the market precedence (D2)."""
    conflicts: tuple[Mapping[str, Any], ...] = ()
    """Other sources' same-day closes beyond the ``close`` tolerance (D2)."""


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

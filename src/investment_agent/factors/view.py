"""Point-in-time snapshot view: the only data entry for factors (P1-B1).

A :class:`SnapshotView` is an immutable, read-only picture of one listing on
``as_of`` under one provider policy (decision D6):

* ``TARGET_POOL`` reads baostock rows (target-pool breadth);
* ``OBSERVATION`` reads iFinD rows (watchlist / candidate depth).

Point-in-time rules (enforced by every constructor):

* market bars: ``trade_date <= as_of``;
* financial periods: public by ``as_of`` (``available_from <= as_of``,
  ``unknown`` availability excluded) - the pubDate gating of
  :meth:`FinancialSnapshotRepository.latest_available`;
* report-overdue records open on ``as_of``.

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
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pit import end_of_business_day_utc
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository

ProviderName = Literal["baostock", "ifind"]


@dataclass(frozen=True)
class ProviderPolicy:
    """Which provider's snapshots a view reads (decision D6)."""

    name: str
    provider: ProviderName


TARGET_POOL = ProviderPolicy("target_pool", "baostock")
OBSERVATION = ProviderPolicy("observation", "ifind")

# Price history loaded for the 5-year valuation percentiles (plus a margin so
# the window start is covered) and for the trailing one-year statistics.
PRICE_HISTORY_DAYS = 5 * 366 + 7
ADJUSTED_HISTORY_DAYS = 400

# Stored field name -> factor-layer name, per provider. Names absent here keep
# their stored name; names mapped to ``None`` are dropped.
PROVIDER_FIELD_RENAMES: dict[str, dict[str, str]] = {
    "baostock": {"net_profit": "net_profit_total", "revenue": "revenue_main"},
    "ifind": {"net_profit": "net_profit_parent", "revenue": "revenue_total"},
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
    {"missing_fields", "multi_period_fields", "source_values", "pub_date", "stat_date", "tables"}
)


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
class FinancialPeriod:
    """One report period's factor-layer fields as usable on ``as_of``."""

    report_period: date
    period_kind: str
    available_from: date | None
    availability: str
    fields: Mapping[str, float | None]
    ref: str | None = None

    def value(self, name: str) -> float | None:
        if name in AMBIGUOUS_STORED_FIELDS:
            raise KeyError(f"{name!r} is ambiguous across providers; use a factor-layer name")
        return self.fields.get(name)

    def describe(self) -> dict[str, str | None]:
        return {
            "report_period": self.report_period.isoformat(),
            "period_kind": self.period_kind,
            "available_from": self.available_from.isoformat() if self.available_from else None,
            "availability": self.availability,
            "ref": self.ref,
        }


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
    ) -> SnapshotView:
        """Pure in-memory constructor; applies the point-in-time rules.

        Bars after ``as_of`` and periods not public by ``as_of`` are dropped;
        for a period given more than once the last one wins (later capture).
        ``sw_code`` is the Shenwan 2021 code valid on ``as_of`` (already vetted
        with :func:`usable_sw_code`); ``industry_code`` / ``industry_name``
        are the CSRC fallback.
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
        return shares, {"source": "financial_snapshot.total_shares", **report.describe()}

    def with_overdue(self, periods: Iterable[date]) -> SnapshotView:
        return replace(self, overdue_periods=tuple(sorted(set(periods))))


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
    provider: str,
    include_backfill: bool = True,
) -> list[FinancialSnapshot]:
    """Every report period public and known on ``as_of``; latest version each.

    Same eligibility as :meth:`FinancialSnapshotRepository.latest_available`
    (``available_from <= as_of``, ``unknown`` excluded, captured by the end of
    ``as_of`` unless backfilled), but for all periods instead of the latest.
    """

    known = FinancialSnapshot.captured_at < end_of_business_day_utc(as_of)
    knowledge = or_(known, FinancialSnapshot.is_backfill.is_(True)) if include_backfill else known
    stmt = (
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.listing_id == listing_id,
            FinancialSnapshot.provider == provider,
            FinancialSnapshot.availability != "unknown",
            FinancialSnapshot.available_from.is_not(None),
            FinancialSnapshot.available_from <= as_of,
            knowledge,
        )
        .order_by(FinancialSnapshot.report_period, FinancialSnapshot.version)
    )
    latest: dict[date, FinancialSnapshot] = {}
    for row in (await session.execute(stmt)).scalars():
        latest[row.report_period] = row
    return [latest[key] for key in sorted(latest)]


async def load_snapshot_view(
    session: AsyncSession,
    ticker: str,
    as_of: date,
    *,
    policy: ProviderPolicy = TARGET_POOL,
    industry_groups: IndustryGroupMap | None = None,
    shenwan_directory: ShenwanDirectory | None = None,
) -> SnapshotView:
    """Read-only point-in-time view of ``ticker`` on ``as_of`` from the database.

    Knowledge time is the end of ``as_of`` (Asia/Shanghai): rows captured
    later are invisible (backfilled financial rows excepted, as in
    ``latest_available``). The industry group comes from the ``sw2021``
    classification valid on ``as_of``, else from ``csrc`` (P1-B3).
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
    financial_rows = await _available_periods(session, listing.id, as_of, provider=policy.provider)
    overdue = await ReportOverdueRepository(session).open_as_of(listing.id, as_of)
    return SnapshotView.build(
        ticker=ticker,
        as_of=as_of,
        policy=policy,
        industry_code=industry.code if industry else None,
        industry_name=industry.name if industry else None,
        raw_bars=[_bar_from_row(row) for row in raw_rows],
        adjusted_bars=[_bar_from_row(row) for row in adjusted_rows],
        periods=[
            FinancialPeriod(
                report_period=row.report_period,
                period_kind=row.period_kind,
                available_from=row.available_from,
                availability=row.availability,
                fields=factor_fields(row.provider, row.fields),
                ref=f"financial_snapshot:{row.id}",
            )
            for row in financial_rows
        ],
        overdue_periods=[record.report_period for record in overdue],
        industry_groups=industry_groups,
        sw_code=sw_code,
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

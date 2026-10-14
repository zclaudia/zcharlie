"""Database loading for the point-in-time snapshot view (P1-B1).

:func:`load_snapshot_view` is the async, read-only database entry: it reads
the listing, the industry classification valid on ``as_of`` (Shenwan 2021,
else CSRC, P1-B3), the market bars, the financial periods and - for the
baostock policy - the dividend events, then hands everything to the pure
:meth:`SnapshotView.build`. Knowledge time is the end of ``as_of``
(Asia/Shanghai): rows captured later are invisible (backfilled financial
rows excepted, as in ``latest_available``).

This is the only module in the factor layer that touches snapshot
repositories, so a repository signature change is a one-file change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.factors.field_resolution import (
    factor_fields,
    provider_fields_from_rows,
    resolve_period_fields,
)
from investment_agent.factors.field_sources import (
    MARKET_ANCHOR_FIELD,
    FieldSourceRegistry,
    default_field_sources,
    market_precedence,
    relative_difference,
)
from investment_agent.factors.industry_groups import IndustryGroupMap, usable_sw_code
from investment_agent.factors.snapshot_model import (
    ADJUSTED_HISTORY_DAYS,
    DIVIDEND_HISTORY_DAYS,
    PRICE_HISTORY_DAYS,
    TARGET_POOL,
    Bar,
    Dividend,
    DividendHistory,
    FinancialPeriod,
    ProviderPolicy,
    SnapshotView,
)
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
from investment_agent.storage.repositories.market_snapshot import (
    MarketSnapshotRepository,
    preferred_row,
)
from investment_agent.storage.repositories.pit import end_of_business_day_utc
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository


def _decimal(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _bar_from_row(
    row: MarketSnapshot, *, conflicts: Sequence[Mapping[str, Any]] = ()
) -> Bar:
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
        provider=row.provider,
        conflicts=tuple(dict(item) for item in conflicts),
    )


def resolve_market_bars(
    by_date: Mapping[date, Mapping[str, MarketSnapshot]],
    precedence: Sequence[str],
    *,
    tolerance: float,
) -> list[Bar]:
    """Bars across providers (D2): the first source in ``precedence`` present on a
    trade date wins; another source's close beyond ``tolerance`` is a conflict
    on the bar (the registry's ``close`` tolerance, 0.1 % for Tencent)."""

    bars: list[Bar] = []
    for trade_date in sorted(by_date):
        rows = by_date[trade_date]
        chosen = preferred_row(rows, precedence)
        close = float(chosen.close)
        conflicts: list[dict[str, Any]] = []
        for provider, row in sorted(rows.items()):
            if provider == chosen.provider:
                continue
            other = float(row.close)
            difference = relative_difference(close, other)
            if difference > tolerance:
                conflicts.append(
                    {
                        "field": "close",
                        "provider": provider,
                        "value": other,
                        "ref": f"market_snapshot:{row.id}",
                        "relative_difference": round(difference, 6),
                        "tolerance": tolerance,
                    }
                )
        bars.append(_bar_from_row(chosen, conflicts=conflicts))
    return bars


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
            [provider_fields_from_rows(provider, items) for provider, items in by_provider.items()],
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
    checks = await repository.last_checks_as_of(
        listing_id, list(range(start.year, as_of.year + 1)), as_of, provider=provider
    )
    return DividendHistory(
        events=tuple(_dividend_from_row(row) for row in rows),
        covered_years={year: _shanghai_date(at) for year, at in checks.items()},
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
    registry = field_sources or default_field_sources()
    if policy.resolves:
        # D2: baostock first, the Tencent same-day close until it arrives;
        # a same-day close beyond the registry tolerance is a conflict.
        precedence = market_precedence(registry)
        raw_bars = resolve_market_bars(
            await MarketSnapshotRepository(session).series_by_provider(
                listing.id,
                as_of - timedelta(days=PRICE_HISTORY_DAYS),
                as_of,
                knowledge_cutoff,
                adjust="none",
                providers=precedence,
                include_backfill=True,
            ),
            precedence,
            tolerance=registry.tolerance(MARKET_ANCHOR_FIELD),
        )
    else:
        raw_bars = [
            _bar_from_row(row)
            for row in await _market_series(
                session,
                listing.id,
                as_of - timedelta(days=PRICE_HISTORY_DAYS),
                as_of,
                knowledge_cutoff,
                adjust="none",
                provider=policy.provider,
            )
        ]
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
        raw_bars=raw_bars,
        adjusted_bars=[_bar_from_row(row) for row in adjusted_rows],
        periods=_financial_periods(financial_rows, policy, field_sources),
        overdue_periods=[record.report_period for record in overdue],
        industry_groups=industry_groups,
        sw_code=sw_code,
        field_sources=field_sources,
        dividends=dividends,
    )

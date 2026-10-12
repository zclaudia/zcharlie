"""/stocks endpoints: directory search for the watchlist / nomination combobox (D4).

Reads the local ``stock_directory`` only (synced weekly by ``universe_sync``);
nothing is fetched upstream per keystroke. Match order: code prefix, then
name substring, then initials and full-pinyin prefixes; see
:meth:`~investment_agent.storage.repositories.stock_directory.StockDirectoryRepository.search`.
"""

import asyncio
import json
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.routes.data_health import _snapshot_universes
from investment_agent.api.schemas.stocks import (
    StockIndustryItem,
    StockReportYearItem,
    StockSearchItem,
)
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.factors.industry_groups import usable_sw_code
from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.providers.baostock_reference import BaostockReferenceClient, IndustrySnapshot
from investment_agent.providers.errors import ProviderError
from investment_agent.providers.shenwan_directory import SW2021_SYSTEM, default_shenwan_directory
from investment_agent.snapshots.breadth import financial_fields
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.earnings_events import DisclosureScheduleRecord
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, IndustryClassification
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pit import end_of_business_day_utc
from investment_agent.storage.repositories.stock_directory import StockDirectoryRepository
from investment_agent.storage.repositories.universe import UniverseRepository

router = APIRouter(
    prefix="/stocks",
    tags=["stocks"],
    dependencies=[Depends(verify_api_key_or_session)],
)

SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 50
SEARCH_QUERY_MAX_LENGTH = 32


@router.get("/search", response_model=list[StockSearchItem])
async def search_stocks(
    settings: Annotated[Settings, Depends(get_settings)],
    q: Annotated[str, Query(max_length=SEARCH_QUERY_MAX_LENGTH)] = "",
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT_MAX)] = SEARCH_LIMIT_DEFAULT,
) -> list[StockSearchItem]:
    """Directory hits for ``q`` (blank -> ``[]``), at most ``limit``."""

    async with session_scope() as session:
        if q.strip():
            _, total = await StockDirectoryRepository(session).freshness()
            if not total:
                raise HTTPException(
                    status_code=503,
                    detail="股票搜索字典尚未就绪，请稍后重试；可在数据健康页查看同步状态。",
                )
        return await search_directory(session, settings, q, limit=limit)


@router.get("/{ticker}/industry", response_model=StockIndustryItem)
async def stock_industry(ticker: str) -> StockIndustryItem:
    """Resolve local classification; fetch a missing one only on stock selection."""
    ticker = ticker.strip().upper()
    if parse_a_share_ticker(ticker) is None:
        raise HTTPException(status_code=422, detail="请输入完整的 A 股代码。")
    today = shanghai_today()
    async with session_scope() as session:
        if await StockDirectoryRepository(session).get(ticker) is None:
            raise HTTPException(status_code=404, detail="股票不在搜索字典中。")
        industry = (await _stock_industries(session, {ticker}, today)).get(ticker)
        if industry:
            return StockIndustryItem(ticker=ticker, industry=industry)
    try:
        snapshot = await BaostockReferenceClient(timeout_seconds=10, max_attempts=1).fetch_industry(
            today, symbol=ticker
        )
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail="行业暂未获取，请稍后重新选择股票重试。") from exc
    # Only persist the selected stock, even if a provider returns extra rows.
    snapshot = IndustrySnapshot(
        as_of=snapshot.as_of,
        effective_date=snapshot.effective_date,
        entries=tuple(entry for entry in snapshot.entries if entry.ticker == ticker),
        captured_at=snapshot.captured_at,
        raw_rows=(),
        system=snapshot.system,
        source=snapshot.source,
    )
    async with session_scope() as session:
        await IndustryRepository(session).apply_industry_snapshot(snapshot)
        industry = (await _stock_industries(session, {ticker}, today)).get(ticker)
    return StockIndustryItem(ticker=ticker, industry=industry)


@router.get("/{ticker}/report-year", response_model=StockReportYearItem)
async def stock_report_year(
    ticker: str, settings: Annotated[Settings, Depends(get_settings)]
) -> StockReportYearItem:
    """Latest annual report actually disclosed, never inferred from a deadline."""
    ticker = ticker.strip().upper()
    if parse_a_share_ticker(ticker) is None:
        raise HTTPException(status_code=422, detail="请输入完整的 A 股代码。")
    today = shanghai_today()
    async with session_scope() as session:
        entry = await StockDirectoryRepository(session).get(ticker)
        if entry is None:
            raise HTTPException(status_code=404, detail="股票不在搜索字典中。")
        name = entry.name
        known = (await _annual_disclosures(session, {ticker}, today)).get(ticker)
    if known and known[0] == today.year - 1:
        return StockReportYearItem(ticker=ticker, report_year=known[0], disclosed_on=known[1])
    client = BaostockMarketClient(timeout_seconds=10, max_attempts=1)
    try:
        async with asyncio.timeout(12):
            for year in range(today.year - 1, today.year - 11, -1):
                if known and year == known[0]:
                    return StockReportYearItem(ticker=ticker, report_year=known[0], disclosed_on=known[1])
                record = await client.fetch_quarterly_financials(ticker, year, 4)
                if record is None or record.pub_date > today:
                    continue
                if record.ticker != ticker or record.report_period != date(year, 12, 31):
                    raise HTTPException(status_code=503, detail="年报披露记录不匹配，请稍后重试。")
                # Keep the full financial answer and raw source, as in the collector.
                # Deterministic per ticker+period and overwritten: a repeated
                # miss must not accumulate a new uuid4 artifact per call.
                relative = f"snapshot-runs/stock-autofill/{ticker}/{record.report_period.isoformat()}/annual.json"
                target = settings.data_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({
                    "provider": record.source, "ticker": ticker,
                    "report_period": record.report_period.isoformat(),
                    "pub_date": record.pub_date.isoformat(),
                    "captured_at": record.captured_at.isoformat(),
                    "tables": record.raw_rows,
                }, ensure_ascii=False, default=str), encoding="utf-8")
                async with session_scope() as session:
                    listing = await ListingRepository(session).ensure_a_share_listing(ticker, company_name=name)
                    current = (await _annual_disclosures(session, {ticker}, today)).get(ticker)
                    if not current or current[0] < year:
                        await FinancialSnapshotRepository(session).insert(
                            listing_id=listing.id, report_period=record.report_period,
                            period_kind="annual", fields=financial_fields(record),
                            provider=record.source, captured_at=record.captured_at,
                            availability="actual", available_from=record.pub_date, raw_ref=relative,
                        )
                return StockReportYearItem(ticker=ticker, report_year=year, disclosed_on=record.pub_date)
    except (ProviderError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="年报披露年度暂未获取，请稍后重新选择股票重试。") from exc
    return StockReportYearItem(ticker=ticker)


async def search_directory(
    session: AsyncSession, settings: Settings, q: str, *, limit: int
) -> list[StockSearchItem]:
    """Directory hits for ``q`` with the target-pool / watchlist flags
    (shared by the API and the MCP ``search_stocks`` tool)."""

    query = q.strip()
    if not query:
        return []
    rows = await StockDirectoryRepository(session).search(query, limit=limit)
    if not rows:
        return []
    tickers = {row.ticker for row in rows}
    today = shanghai_today()
    industries = await _stock_industries(session, tickers, today)
    annuals = await _annual_disclosures(session, tickers, today)
    universes = await _snapshot_universes(session, settings)
    members = await UniverseRepository(session).members_as_of(universes[0], today)
    in_universe = {member.ticker for member in members} & tickers
    on_watchlist = await _watchlist_tickers(session) & tickers
    return [
        StockSearchItem(
            ticker=row.ticker,
            name=row.name,
            industry=industries.get(row.ticker),
            report_year=annuals[row.ticker][0] if row.ticker in annuals and annuals[row.ticker][0] == today.year - 1 else None,
            disclosed_on=annuals[row.ticker][1] if row.ticker in annuals and annuals[row.ticker][0] == today.year - 1 else None,
            status=row.status,
            in_universe=row.ticker in in_universe,
            on_watchlist=row.ticker in on_watchlist,
        )
        for row in rows
    ]


async def _annual_disclosures(
    session: AsyncSession, tickers: set[str], today: date
) -> dict[str, tuple[int, date]]:
    """Actual annual disclosures from financial captures or published schedules."""
    cutoff = end_of_business_day_utc(today)
    snapshots = select(
        FinancialSnapshot.listing_id.label("listing_id"),
        FinancialSnapshot.report_period.label("period"),
        FinancialSnapshot.available_from.label("disclosed_on"),
    ).where(
        FinancialSnapshot.period_kind == "annual",
        FinancialSnapshot.availability == "actual",
        FinancialSnapshot.available_from <= today,
        FinancialSnapshot.captured_at < cutoff,
    )
    schedules = select(
        DisclosureScheduleRecord.listing_id,
        DisclosureScheduleRecord.report_period,
        DisclosureScheduleRecord.actual_date,
    ).where(
        DisclosureScheduleRecord.actual_date <= today,
        DisclosureScheduleRecord.captured_at < cutoff,
    )
    disclosures = union_all(snapshots, schedules).subquery()
    stmt = select(
        Listing.ticker, Listing.market_code, disclosures.c.period, disclosures.c.disclosed_on
    ).join(disclosures, disclosures.c.listing_id == Listing.id).where(
        Listing.ticker.in_({ticker.split(".")[0] for ticker in tickers}),
    )
    annuals: dict[str, tuple[int, date]] = {}
    for symbol, market, period, disclosed_on in (await session.execute(stmt)).all():
        ticker = format_listing_ticker(ticker=symbol, market_code=market)
        if ticker not in tickers or (period.month, period.day) != (12, 31) or period.year >= today.year:
            continue
        known = annuals.get(ticker)
        if not known or period.year > known[0] or (period.year == known[0] and disclosed_on < known[1]):
            annuals[ticker] = (period.year, disclosed_on)
    return annuals


async def _stock_industries(
    session: AsyncSession, tickers: set[str], today: date
) -> dict[str, str]:
    """Batch local industries using the same preference as candidate promotion."""
    stmt = (
        select(Listing.ticker, Listing.market_code, IndustryClassification)
        .join(IndustryClassification, IndustryClassification.listing_id == Listing.id)
        .where(
            Listing.ticker.in_({ticker.split(".")[0] for ticker in tickers}),
            IndustryClassification.system.in_((SW2021_SYSTEM, "csrc")),
            IndustryClassification.valid_from <= today,
            or_(IndustryClassification.valid_to.is_(None), IndustryClassification.valid_to > today),
        )
        .order_by(IndustryClassification.valid_from.desc())
    )
    industries: dict[str, str] = {}
    shenwan: dict[str, str] = {}
    directory = default_shenwan_directory()
    for symbol, market, classification in (await session.execute(stmt)).all():
        ticker = format_listing_ticker(ticker=symbol, market_code=market)
        if ticker not in tickers:
            continue
        if classification.system == SW2021_SYSTEM:
            code = usable_sw_code(
                classification.code, classification.valid_from, classification.valid_to,
                directory, classification.name,
            )
            chain = directory.chain(code) if code else ()
            if chain and chain[0].level == 1:
                shenwan.setdefault(ticker, chain[0].name)
        elif classification.name:
            industries.setdefault(ticker, classification.name)
    return {**industries, **shenwan}


async def _watchlist_tickers(session: AsyncSession) -> set[str]:
    stmt = select(Listing.ticker, Listing.market_code).join(
        WatchlistItem, WatchlistItem.listing_id == Listing.id
    )
    return {
        format_listing_ticker(ticker=ticker, market_code=market_code)
        for ticker, market_code in (await session.execute(stmt)).all()
    }

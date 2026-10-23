"""/stocks endpoints: directory search for the watchlist / nomination combobox (D4).

Reads the local ``stock_directory`` only (synced weekly by ``universe_sync``);
nothing is fetched upstream per keystroke. Match order: code prefix, then
name substring, then initials and full-pinyin prefixes; see
:meth:`~investment_agent.storage.repositories.stock_directory.StockDirectoryRepository.search`.

``search_directory`` and its classification / annual-disclosure helpers now
live in :mod:`investment_agent.directory_search` (shared with the MCP
``search_stocks`` tool and the agent's ``search_stock_directory`` tool); this
module imports them from there.
"""

import asyncio
import json
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.stocks import (
    StockIndustryItem,
    StockReportYearItem,
    StockSearchItem,
)
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.directory_search import (
    _annual_disclosures,
    _stock_industries,
    search_directory,
)
from investment_agent.domain.a_share import parse_a_share_ticker
from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.providers.baostock_reference import BaostockReferenceClient, IndustrySnapshot
from investment_agent.providers.errors import ProviderError
from investment_agent.snapshots.breadth import financial_fields
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.stock_directory import StockDirectoryRepository

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

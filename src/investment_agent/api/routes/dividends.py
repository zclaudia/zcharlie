"""Read stored A-share dividend context and queue a watchlist refresh."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.dividends.service import dividend_context
from investment_agent.domain.a_share import parse_a_share_ticker
from investment_agent.snapshots.jobs import DividendSnapshotPayload, enqueue_snapshot_job
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository

router = APIRouter(prefix="/stocks", tags=["stocks"], dependencies=[Depends(verify_api_key_or_session)])


def _ticker(value: str) -> str:
    value = value.strip().upper()
    if parse_a_share_ticker(value) is None:
        raise HTTPException(status_code=422, detail="请输入完整的 A 股代码。")
    return value


async def _enabled_watchlist(session: AsyncSession, ticker: str) -> bool:
    listing = await ListingRepository(session).get_a_share_listing(ticker)
    if listing is None:
        return False
    item = await WatchlistRepository(session).get_by_listing(listing.id)
    return item is not None and item.enabled


@router.get("/{ticker}/dividends")
async def get_dividends(ticker: str, settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    ticker = _ticker(ticker)
    async with session_scope() as session:
        try:
            result = await dividend_context(session, ticker, shanghai_today())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="股票尚未入库。") from exc
        result["refresh_enabled"] = bool(settings.wmshare_dividends_enabled and await _enabled_watchlist(session, ticker))
        result["refresh_disabled_reason"] = (
            None if result["refresh_enabled"] else
            "wmshare 分红采集未启用。" if not settings.wmshare_dividends_enabled else "仅启用的观察池股票可刷新分红。"
        )
        return result


@router.post("/{ticker}/dividends/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_dividends(ticker: str, settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, Any]:
    ticker = _ticker(ticker)
    if not settings.wmshare_dividends_enabled:
        raise HTTPException(status_code=409, detail="wmshare 分红采集未启用。")
    async with session_scope() as session:
        if not await _enabled_watchlist(session, ticker):
            raise HTTPException(status_code=403, detail="仅启用的观察池股票可刷新分红。")
        payload = DividendSnapshotPayload(provider="wmshare", scope="watchlist", as_of=shanghai_today(), tickers=(ticker,))
        run, created = await enqueue_snapshot_job(session, payload, triggered_by="api")
        return {"run_id": run.id, "status": run.status, "created": created}

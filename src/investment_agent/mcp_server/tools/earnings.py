"""MCP tool ``list_earnings_events``: newest forecasts and disclosure schedules."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from investment_agent.mcp_server.plumbing import (
    EARNINGS_LIMIT_DEFAULT,
    LIMIT_MAX,
    TICKER_PATTERN,
    ServerContext,
)


def register_earnings(server: MCPServer, ctx: ServerContext) -> None:
    """Register the earnings-events tool."""

    @server.tool()
    async def list_earnings_events(
        ticker: Annotated[
            str | None, Field(pattern=TICKER_PATTERN, description="Default: all")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = EARNINGS_LIMIT_DEFAULT,
    ) -> dict[str, Any]:
        """Newest earnings forecasts and disclosure-schedule captures
        (Eastmoney via akshare), of one stock or across all stored ones."""

        from investment_agent.clock import shanghai_today
        from investment_agent.domain.a_share import format_listing_ticker
        from investment_agent.storage.models.reference import Listing
        from investment_agent.storage.repositories.earnings_events import EarningsEventRepository

        async with ctx.session() as session:
            listing_id = await ctx.listing_id(session, ticker) if ticker is not None else None
            repository = EarningsEventRepository(session)
            forecasts = await repository.recent_forecasts(listing_id=listing_id, limit=limit)
            schedules = await repository.recent_schedules(listing_id=listing_id, limit=limit)
            tickers: dict[int, str] = {}
            listing_ids = {row.listing_id for row in forecasts} | {
                row.listing_id for row in schedules
            }
            for row_listing_id in listing_ids:
                listing = await session.get(Listing, row_listing_id)
                if listing is not None:
                    tickers[row_listing_id] = format_listing_ticker(
                        ticker=listing.ticker, market_code=listing.market_code
                    )
        return ctx.json(
            {
                "as_of": shanghai_today(),
                "ticker": ticker,
                "forecasts": [
                    {
                        "ticker": tickers.get(row.listing_id),
                        "report_period": row.report_period,
                        "notice_date": row.notice_date,
                        "forecast_type": row.forecast_type,
                        "indicator": row.indicator,
                        "profit_low": ctx.number(row.profit_low),
                        "profit_high": ctx.number(row.profit_high),
                        "yoy_low": ctx.number(row.yoy_low),
                        "yoy_high": ctx.number(row.yoy_high),
                        "content": row.content,
                        "provider": row.provider,
                        "captured_at": ctx.utc(row.captured_at),
                        "raw_ref": row.raw_ref,
                    }
                    for row in forecasts
                ],
                "disclosures": [
                    {
                        "ticker": tickers.get(row.listing_id),
                        "report_period": row.report_period,
                        "first_date": row.first_date,
                        "scheduled_date": row.scheduled_date,
                        "actual_date": row.actual_date,
                        "provider": row.provider,
                        "captured_at": ctx.utc(row.captured_at),
                        "raw_ref": row.raw_ref,
                    }
                    for row in schedules
                ],
            }
        )

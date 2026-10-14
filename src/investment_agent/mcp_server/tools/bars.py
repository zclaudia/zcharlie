"""MCP tool ``get_bars``: the last N uncorrected daily closes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from investment_agent.mcp_server.plumbing import (
    BARS_DAYS_DEFAULT,
    BARS_DAYS_MAX,
    BARS_DAYS_MIN,
    ServerContext,
    Ticker,
)


def register_bars(server: MCPServer, ctx: ServerContext) -> None:
    """Register the daily-bars tool."""

    @server.tool()
    async def get_bars(
        ticker: Ticker,
        days: Annotated[int, Field(ge=BARS_DAYS_MIN, le=BARS_DAYS_MAX)] = BARS_DAYS_DEFAULT,
    ) -> dict[str, Any]:
        """The last ``days`` uncorrected daily closes known now (oldest first),
        baostock first and the Tencent 15:10 close for days baostock lacks."""

        from investment_agent.api.views import BARS_ADJUST, BARS_PROVIDER, load_bars

        as_of = datetime.now(UTC)
        async with ctx.session() as session:
            listing_id = await ctx.listing_id(session, ticker)
            rows = await load_bars(session, listing_id, days, as_of=as_of)
        return ctx.json(
            {
                "ticker": ticker,
                "as_of": as_of,
                "provider": BARS_PROVIDER,
                "adjust": BARS_ADJUST,
                "count": len(rows),
                "bars": [
                    {
                        "trade_date": row.trade_date,
                        "open": ctx.number(row.open),
                        "high": ctx.number(row.high),
                        "low": ctx.number(row.low),
                        "close": ctx.number(row.close),
                        "volume": ctx.number(row.volume),
                        "amount": ctx.number(row.amount),
                        "pe_ttm": ctx.number(row.pe_ttm),
                        "turnover": ctx.number(row.turnover),
                        "provider": row.provider,
                        "captured_at": ctx.utc(row.captured_at),
                        "is_backfill": row.is_backfill,
                    }
                    for row in rows
                ],
            }
        )

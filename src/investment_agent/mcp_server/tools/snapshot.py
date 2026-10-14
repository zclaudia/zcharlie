"""MCP tool ``get_stock_snapshot``: the point-in-time view of one stock."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from investment_agent.mcp_server.plumbing import ServerContext, Ticker


def register_stock_snapshot(server: MCPServer, ctx: ServerContext) -> None:
    """Register the point-in-time snapshot tool."""

    @server.tool()
    async def get_stock_snapshot(
        ticker: Ticker,
        as_of: Annotated[
            date | None,
            Field(description="Knowledge date (Asia/Shanghai); default today"),
        ] = None,
    ) -> dict[str, Any]:
        """Point-in-time view of one stock on ``as_of``: factor-layer values,
        industry (CSRC / Shenwan / group), the latest daily bar and report
        period usable that day, and the latest main-business segments."""

        from investment_agent.clock import shanghai_today
        from investment_agent.factors.catalog import DEFAULT_REGISTRY
        from investment_agent.factors.library import latest_bar
        from investment_agent.factors.view_db import load_snapshot_view
        from investment_agent.rules.drafting import (
            factor_input,
            industry_context,
            load_segments,
            segments_input,
        )

        day = as_of or shanghai_today()
        async with ctx.session() as session:
            try:
                view = await load_snapshot_view(session, ticker, day)
            except LookupError as exc:
                raise ToolError(f"Listing not found: {ticker}") from exc
            segments = await load_segments(session, ticker, day)
        bar, bar_flags = latest_bar(view)
        period = view.periods[-1] if view.periods else None
        market: dict[str, Any] | None = None
        if bar is not None:
            market = {
                "trade_date": bar.trade_date,
                "close": bar.close,
                "pe_ttm": bar.pe_ttm,
                "pb_mrq": bar.pb_mrq,
                "ps_ttm": bar.ps_ttm,
                "turnover": bar.turnover,
                "trade_status": bar.trade_status,
                "is_st": bar.is_st,
                "provider": bar.provider or view.provider,
                "ref": bar.ref,
                "flags": list(bar_flags),
                "conflicts": list(bar.conflicts),
            }
        financial: dict[str, Any] | None = None
        if period is not None:
            financial = period.describe(period.fields.keys()) | {
                "provider": view.provider,
                "fields": dict(period.fields),
            }
        return ctx.json(
            {
                "ticker": view.ticker,
                "as_of": view.as_of,
                "generated_at": datetime.now(UTC),
                "provider_policy": view.policy.name,
                "industry": industry_context(view),
                "factors": factor_input(DEFAULT_REGISTRY.compute_all(view)),
                "market": market,
                "financial": financial,
                "overdue_periods": list(view.overdue_periods),
                "segments": segments_input(segments),
            }
        )

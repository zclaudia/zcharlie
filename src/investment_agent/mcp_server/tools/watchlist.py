"""MCP tool ``list_watchlist``: every watchlist row with its newest research."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from investment_agent.mcp_server.plumbing import ServerContext


def register_watchlist(server: MCPServer, ctx: ServerContext) -> None:
    """Register the watchlist tools."""

    @server.tool()
    async def list_watchlist() -> dict[str, Any]:
        """Every watchlist entry (enabled and disabled) with the newest research
        summary, as ``GET /watchlist`` returns it."""

        from investment_agent.api.views import watchlist_item_out
        from investment_agent.clock import shanghai_today
        from investment_agent.storage.repositories.watchlist import WatchlistRepository

        async with ctx.session() as session:
            items = await WatchlistRepository(session).list_all()
            out = [await watchlist_item_out(session, item) for item in items]
        return ctx.json({"as_of": shanghai_today(), "count": len(out), "items": out})

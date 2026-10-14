"""MCP tool ``get_data_health`` (also the payload behind ``zcharlie://health``)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from investment_agent.mcp_server.plumbing import ServerContext


async def data_health_payload(ctx: ServerContext) -> dict[str, Any]:
    """The data-health aggregate, shared by the tool and the resource."""

    from investment_agent.api.views import compute_data_health

    async with ctx.session() as session:
        health = await compute_data_health(session, ctx.settings())
    return ctx.json(health)


def register_health(server: MCPServer, ctx: ServerContext) -> None:
    """Register the data-health tool."""

    @server.tool()
    async def get_data_health() -> dict[str, Any]:
        """The data-health aggregate of ``GET /health/data``: completeness,
        freshness, backfill, verification budget, overdue reports, provider
        breakers and warnings."""

        return await data_health_payload(ctx)

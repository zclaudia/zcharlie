"""MCP tool ``search_stocks``: local stock-directory search."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from investment_agent.mcp_server.plumbing import (
    LIMIT_MAX,
    SEARCH_LIMIT_DEFAULT,
    SEARCH_QUERY_MAX_LENGTH,
    ServerContext,
)


def register_stocks(server: MCPServer, ctx: ServerContext) -> None:
    """Register the directory-search tool."""

    @server.tool()
    async def search_stocks(
        q: Annotated[str, Field(min_length=1, max_length=SEARCH_QUERY_MAX_LENGTH)],
        limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = SEARCH_LIMIT_DEFAULT,
    ) -> dict[str, Any]:
        """Stock directory search by code prefix, name substring or pinyin
        initials (local directory, nothing fetched upstream)."""

        from investment_agent.clock import shanghai_today
        from investment_agent.directory_search import search_directory

        async with ctx.session() as session:
            items = await search_directory(session, ctx.settings(), q, limit=limit)
        return ctx.json({"as_of": shanghai_today(), "query": q.strip(), "items": items})

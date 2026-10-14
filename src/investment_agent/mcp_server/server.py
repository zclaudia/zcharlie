"""The ``zcharlie`` MCP server: read-only tools and resources over the local DB.

Boundaries (decision D5): stdio only, no HTTP listener, no LLM client, no
job queue access, no writing tool. Every tool opens one session from the
read-only engine (:mod:`investment_agent.mcp_server.readonly`) and returns
JSON with ``captured_at`` / ``provider`` / ``as_of`` wherever the stored rows
carry them. Tool errors are returned as MCP errors without paths or secrets
(the SDK replaces the text of unexpected exceptions with a generic message).

The tools call the same service functions as the HTTP API, so the payloads
match the API schemas field for field.

This module only assembles the server: it registers the per-domain tools from
:mod:`investment_agent.mcp_server.tools` (each ``register_*`` takes the shared
:class:`~investment_agent.mcp_server.plumbing.ServerContext`), registers the
health resource and declares :data:`TOOL_NAMES`. The JSON schema of every tool
still comes from its type annotations (``@server.tool()``), never from a
hand-written table.
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from investment_agent.config import Settings
from investment_agent.mcp_server.plumbing import ServerContext
from investment_agent.mcp_server.readonly import SessionFactory
from investment_agent.mcp_server.tools.bars import register_bars
from investment_agent.mcp_server.tools.candidates import register_candidates
from investment_agent.mcp_server.tools.earnings import register_earnings
from investment_agent.mcp_server.tools.health import data_health_payload, register_health
from investment_agent.mcp_server.tools.snapshot import register_stock_snapshot
from investment_agent.mcp_server.tools.stocks import register_stocks
from investment_agent.mcp_server.tools.watchlist import register_watchlist

SERVER_NAME = "zcharlie"
SERVER_INSTRUCTIONS = (
    "Read-only access to one personal A-share research desk: watchlist, "
    "point-in-time snapshots, candidates, rules, earnings events and data "
    "health. Nothing here writes, queues work or calls an LLM. Every value "
    "is as captured from the named provider at captured_at; as_of is the "
    "knowledge date of a point-in-time read."
)

TOOL_NAMES: tuple[str, ...] = (
    "list_watchlist",
    "get_stock_snapshot",
    "get_bars",
    "list_candidates",
    "get_candidate",
    "get_rule",
    "list_earnings_events",
    "get_data_health",
    "search_stocks",
)
"""The catalogue in registration order (locked by ``tests/unit/test_mcp_tools.py``)."""


def build_server(
    settings: Settings | None = None, sessions: SessionFactory | None = None
) -> MCPServer:
    """The server with its tools registered.

    ``settings`` and ``sessions`` are only touched when a tool runs, so
    ``ia mcp tools`` can list the catalogue without a database.
    """

    server = MCPServer(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    ctx = ServerContext(settings, sessions)

    # The call order is the tool catalogue order (``TOOL_NAMES``).
    register_watchlist(server, ctx)
    register_stock_snapshot(server, ctx)
    register_bars(server, ctx)
    register_candidates(server, ctx)
    register_earnings(server, ctx)
    register_health(server, ctx)
    register_stocks(server, ctx)

    @server.resource(
        "zcharlie://health",
        name="data_health",
        description="Data-layer health aggregate (same payload as get_data_health).",
        mime_type="application/json",
    )
    async def health_resource() -> str:
        return json.dumps(await data_health_payload(ctx), ensure_ascii=False)

    return server


__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "TOOL_NAMES", "build_server"]

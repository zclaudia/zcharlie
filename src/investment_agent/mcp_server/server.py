"""The ``zcharlie`` MCP server: read-only tools and resources over the local DB.

Boundaries (decision D5): stdio only, no HTTP listener, no LLM client, no
job queue access, no writing tool. Every tool opens one session from the
read-only engine (:mod:`investment_agent.mcp_server.readonly`) and returns
JSON with ``captured_at`` / ``provider`` / ``as_of`` wherever the stored rows
carry them. Tool errors are returned as MCP errors without paths or secrets.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from investment_agent.config import Settings
from investment_agent.mcp_server.readonly import SessionFactory

SERVER_NAME = "zcharlie"
SERVER_INSTRUCTIONS = (
    "Read-only access to one personal A-share research desk: watchlist, "
    "point-in-time snapshots, candidates, rules, earnings events and data "
    "health. Nothing here writes, queues work or calls an LLM. Every value "
    "is as captured from the named provider at captured_at; as_of is the "
    "knowledge date of a point-in-time read."
)


def build_server(
    settings: Settings | None = None, sessions: SessionFactory | None = None
) -> MCPServer:
    """The server with its tools registered.

    ``settings`` and ``sessions`` are only touched when a tool runs, so
    ``ia mcp tools`` can list the catalogue without a database.
    """

    server = MCPServer(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    return server


__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "build_server"]

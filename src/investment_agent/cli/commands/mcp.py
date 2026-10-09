"""``ia mcp``: the read-only MCP server (stdio) and its tool catalogue."""

from __future__ import annotations

import logging
import sys

import typer
from rich import print as rprint
from rich.table import Table

from investment_agent.config import Settings
from investment_agent.mcp_server.server import build_server

app = typer.Typer(help="Read-only MCP server for Claude Desktop and other MCP clients")


async def _serve() -> None:
    from investment_agent.mcp_server.readonly import readonly_engine, readonly_sessions
    from investment_agent.settings.loader import load_settings

    bootstrap = Settings()  # type: ignore[call-arg]
    engine = readonly_engine(bootstrap)
    try:
        sessions = readonly_sessions(engine)
        # Runtime thresholds (health) come from the settings table; the LLM
        # fields of the result are never read by any tool.
        async with sessions() as session:
            settings = await load_settings(session, base=bootstrap)
        await build_server(settings, sessions).run_stdio_async()
    finally:
        await engine.dispose()


def _logs_to_stderr() -> None:
    """stdout is the MCP transport: move every stdout log handler to stderr."""

    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.setStream(sys.stderr)


@app.command("serve")
def serve() -> None:
    """Serve the read-only tools over stdio (the MCP client spawns this)."""

    import asyncio

    _logs_to_stderr()
    asyncio.run(_serve())


@app.command("tools")
def tools() -> None:
    """Print the tool catalogue (name and description), no database needed."""

    import asyncio

    server = build_server()
    listed = asyncio.run(server.list_tools())
    table = Table("tool", "description")
    for tool in listed:
        table.add_row(
            tool.name, (tool.description or "").strip().splitlines()[0] if tool.description else ""
        )
    rprint(table)
    if not listed:
        rprint("[yellow]no tools registered[/yellow]")

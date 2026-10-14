"""Shared plumbing of the MCP tools: the server context and JSON vocabulary.

``build_server`` used to declare all ten registrations inline and close over
the settings, the session factory, ``listing_id`` and the JSON helpers;
D4 moved the registrations into :mod:`investment_agent.mcp_server.tools` (one
module per domain) and made those closed-over names explicit here:

* :class:`ServerContext` - what the transport was started with (settings,
  read-only session factory) plus the ``listing_id`` lookup and the
  serialization helpers every tool uses. ``settings`` / ``session`` are only
  touched when a tool runs, so ``ia mcp tools`` can list the catalogue
  without a database or settings;
* the argument vocabulary (ticker pattern, page sizes) the tool signatures
  annotate; the JSON schema still comes from those type annotations, no
  schema is hand-written.

Importing this module must stay cheap: it imports no domain, storage or
route module at module level.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.domain.time import as_utc
from investment_agent.mcp_server.readonly import SessionFactory

TICKER_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"
Ticker = Annotated[
    str, Field(pattern=TICKER_PATTERN, description="Canonical A-share ticker, e.g. 600519.SH")
]
BARS_DAYS_DEFAULT = 120
BARS_DAYS_MIN, BARS_DAYS_MAX = 20, 400
LIMIT_MAX = 200
CANDIDATES_LIMIT_DEFAULT = 20
EARNINGS_LIMIT_DEFAULT = 50
SEARCH_LIMIT_DEFAULT = 10
SEARCH_QUERY_MAX_LENGTH = 32
CANDIDATE_STATUSES = ("open", "promoted", "dismissed", "expired")
"""The candidate status vocabulary (the ``list_candidates`` pattern spells it out)."""


class ServerContext:
    """The state and helpers the per-domain registrations share.

    ``settings`` / ``sessions`` are the values ``build_server`` was called
    with; both may be ``None`` so the catalogue is listable without a
    database, and a tool that needs them raises :class:`ToolError` instead.
    """

    def __init__(self, settings: Settings | None, sessions: SessionFactory | None) -> None:
        self._settings = settings
        self._sessions = sessions

    def settings(self) -> Settings:
        if self._settings is None:
            raise ToolError("server started without settings")
        return self._settings

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._sessions is None:
            raise ToolError("server started without a database")
        async with self._sessions() as session:
            yield session

    async def listing_id(self, session: AsyncSession, ticker: str) -> int:
        """The listing id of ``ticker`` or an MCP ``ToolError`` when absent."""

        from investment_agent.storage.repositories.listing import ListingRepository

        listing = await ListingRepository(session).get_a_share_listing(ticker)
        if listing is None:
            raise ToolError(f"Listing not found: {ticker}")
        return listing.id

    @staticmethod
    def json(value: Any) -> dict[str, Any]:
        """Plain JSON data (dates ISO, Decimals as numbers, models dumped)."""

        data = to_jsonable_python(value, serialize_unknown=True)
        assert isinstance(data, dict)
        return data

    @staticmethod
    def number(value: Decimal | None) -> float | None:
        """Stored ``Numeric`` columns as JSON numbers (pydantic would emit strings)."""

        return None if value is None else float(value)

    @staticmethod
    def utc(value: datetime) -> datetime:
        """Stored timestamps as aware UTC (``captured_at`` fields)."""

        return as_utc(value)


__all__ = [
    "BARS_DAYS_DEFAULT",
    "BARS_DAYS_MAX",
    "BARS_DAYS_MIN",
    "CANDIDATES_LIMIT_DEFAULT",
    "CANDIDATE_STATUSES",
    "EARNINGS_LIMIT_DEFAULT",
    "LIMIT_MAX",
    "SEARCH_LIMIT_DEFAULT",
    "SEARCH_QUERY_MAX_LENGTH",
    "TICKER_PATTERN",
    "ServerContext",
    "Ticker",
]

"""The ``zcharlie`` MCP server: read-only tools and resources over the local DB.

Boundaries (decision D5): stdio only, no HTTP listener, no LLM client, no
job queue access, no writing tool. Every tool opens one session from the
read-only engine (:mod:`investment_agent.mcp_server.readonly`) and returns
JSON with ``captured_at`` / ``provider`` / ``as_of`` wherever the stored rows
carry them. Tool errors are returned as MCP errors without paths or secrets
(the SDK replaces the text of unexpected exceptions with a generic message).

The tools call the same service functions as the HTTP API, so the payloads
match the API schemas field for field.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession

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

TICKER_PATTERN = r"^\d{6}\.(SH|SZ|BJ)$"
BARS_DAYS_DEFAULT = 120
BARS_DAYS_MIN, BARS_DAYS_MAX = 20, 400
LIMIT_MAX = 200
CANDIDATES_LIMIT_DEFAULT = 20
EARNINGS_LIMIT_DEFAULT = 50
SEARCH_LIMIT_DEFAULT = 10
SEARCH_QUERY_MAX_LENGTH = 32
CANDIDATE_STATUSES = ("open", "promoted", "dismissed", "expired")

Ticker = Annotated[
    str, Field(pattern=TICKER_PATTERN, description="Canonical A-share ticker, e.g. 600519.SH")
]
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


def _json(value: Any) -> dict[str, Any]:
    """Plain JSON data (dates ISO, Decimals as numbers, models dumped)."""

    data = to_jsonable_python(value, serialize_unknown=True)
    assert isinstance(data, dict)
    return data


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _number(value: Decimal | None) -> float | None:
    """Stored ``Numeric`` columns as JSON numbers (pydantic would emit strings)."""

    return None if value is None else float(value)


def build_server(
    settings: Settings | None = None, sessions: SessionFactory | None = None
) -> MCPServer:
    """The server with its tools registered.

    ``settings`` and ``sessions`` are only touched when a tool runs, so
    ``ia mcp tools`` can list the catalogue without a database.
    """

    server = MCPServer(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)

    def _settings() -> Settings:
        if settings is None:
            raise ToolError("server started without settings")
        return settings

    @asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        if sessions is None:
            raise ToolError("server started without a database")
        async with sessions() as session:
            yield session

    async def _listing_id(session: AsyncSession, ticker: str) -> int:
        from investment_agent.storage.repositories.listing import ListingRepository

        listing = await ListingRepository(session).get_a_share_listing(ticker)
        if listing is None:
            raise ToolError(f"Listing not found: {ticker}")
        return listing.id

    # -- tools ------------------------------------------------------------- #

    @server.tool()
    async def list_watchlist() -> dict[str, Any]:
        """Every watchlist entry (enabled and disabled) with the newest research
        summary, as ``GET /watchlist`` returns it."""

        from investment_agent.api.routes.watchlist import watchlist_item_out
        from investment_agent.clock import shanghai_today
        from investment_agent.storage.repositories.watchlist import WatchlistRepository

        async with _session() as session:
            items = await WatchlistRepository(session).list_all()
            out = [await watchlist_item_out(session, item) for item in items]
        return _json({"as_of": shanghai_today(), "count": len(out), "items": out})

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
        from investment_agent.factors.view import load_snapshot_view
        from investment_agent.rules.drafting import (
            factor_input,
            industry_context,
            load_segments,
            segments_input,
        )

        day = as_of or shanghai_today()
        async with _session() as session:
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
        return _json(
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

    @server.tool()
    async def get_bars(
        ticker: Ticker,
        days: Annotated[int, Field(ge=BARS_DAYS_MIN, le=BARS_DAYS_MAX)] = BARS_DAYS_DEFAULT,
    ) -> dict[str, Any]:
        """The last ``days`` uncorrected daily closes known now (oldest first),
        baostock first and the Tencent 15:10 close for days baostock lacks."""

        from investment_agent.api.routes.market import BARS_ADJUST, BARS_PROVIDER, load_bars

        as_of = datetime.now(UTC)
        async with _session() as session:
            listing_id = await _listing_id(session, ticker)
            rows = await load_bars(session, listing_id, days, as_of=as_of)
        return _json(
            {
                "ticker": ticker,
                "as_of": as_of,
                "provider": BARS_PROVIDER,
                "adjust": BARS_ADJUST,
                "count": len(rows),
                "bars": [
                    {
                        "trade_date": row.trade_date,
                        "open": _number(row.open),
                        "high": _number(row.high),
                        "low": _number(row.low),
                        "close": _number(row.close),
                        "volume": _number(row.volume),
                        "amount": _number(row.amount),
                        "pe_ttm": _number(row.pe_ttm),
                        "turnover": _number(row.turnover),
                        "provider": row.provider,
                        "captured_at": _utc(row.captured_at),
                        "is_backfill": row.is_backfill,
                    }
                    for row in rows
                ],
            }
        )

    @server.tool()
    async def list_candidates(
        status: Annotated[
            str | None,
            Field(pattern=r"^(open|promoted|dismissed|expired)$", description="Default: all"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = CANDIDATES_LIMIT_DEFAULT,
    ) -> dict[str, Any]:
        """Candidates newest first (sources, latest screen reasons, draft state),
        as ``GET /candidates`` returns them."""

        from investment_agent.api.routes.candidates import TemplateCache, candidate_summary_out
        from investment_agent.clock import shanghai_today
        from investment_agent.storage.repositories.candidate import CandidateRepository

        async with _session() as session:
            rows, total = await CandidateRepository(session).page(
                status=status, limit=limit, offset=0
            )
            templates = TemplateCache(session)
            items = [await candidate_summary_out(session, row, templates) for row in rows]
        return _json({"as_of": shanghai_today(), "status": status, "total": total, "items": items})

    @server.tool()
    async def get_candidate(candidate_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """One candidate with its screen reasons, draft rule (rationale and
        parameters), decision log and main-business segments."""

        from investment_agent.api.routes.candidates import candidate_detail_out
        from investment_agent.storage.repositories.candidate import CandidateRepository

        async with _session() as session:
            candidate = await CandidateRepository(session).get(candidate_id)
            if candidate is None:
                raise ToolError(f"Candidate {candidate_id} not found")
            detail = await candidate_detail_out(session, candidate)
        return _json(detail)

    @server.tool()
    async def get_rule(rule_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """One rule instance: template reference, parameters with their ranges,
        zones, status and draft rationale."""

        from investment_agent.api.routes.candidates import TemplateCache, rule_detail_out
        from investment_agent.storage.models.rules import RuleInstanceRecord

        async with _session() as session:
            rule = await session.get(RuleInstanceRecord, rule_id)
            if rule is None:
                raise ToolError(f"Rule {rule_id} not found")
            detail = await rule_detail_out(rule, TemplateCache(session))
        return _json(detail)

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

        async with _session() as session:
            listing_id = await _listing_id(session, ticker) if ticker is not None else None
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
        return _json(
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
                        "profit_low": _number(row.profit_low),
                        "profit_high": _number(row.profit_high),
                        "yoy_low": _number(row.yoy_low),
                        "yoy_high": _number(row.yoy_high),
                        "content": row.content,
                        "provider": row.provider,
                        "captured_at": _utc(row.captured_at),
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
                        "captured_at": _utc(row.captured_at),
                        "raw_ref": row.raw_ref,
                    }
                    for row in schedules
                ],
            }
        )

    async def _health() -> dict[str, Any]:
        from investment_agent.api.routes.data_health import compute_data_health

        async with _session() as session:
            health = await compute_data_health(session, _settings())
        return _json(health)

    @server.tool()
    async def get_data_health() -> dict[str, Any]:
        """The data-health aggregate of ``GET /health/data``: completeness,
        freshness, backfill, verification budget, overdue reports, provider
        breakers and warnings."""

        return await _health()

    @server.tool()
    async def search_stocks(
        q: Annotated[str, Field(min_length=1, max_length=SEARCH_QUERY_MAX_LENGTH)],
        limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = SEARCH_LIMIT_DEFAULT,
    ) -> dict[str, Any]:
        """Stock directory search by code prefix, name substring or pinyin
        initials (local directory, nothing fetched upstream)."""

        from investment_agent.api.routes.stocks import search_directory
        from investment_agent.clock import shanghai_today

        async with _session() as session:
            items = await search_directory(session, _settings(), q, limit=limit)
        return _json({"as_of": shanghai_today(), "query": q.strip(), "items": items})

    # -- resources --------------------------------------------------------- #

    @server.resource(
        "zcharlie://health",
        name="data_health",
        description="Data-layer health aggregate (same payload as get_data_health).",
        mime_type="application/json",
    )
    async def health_resource() -> str:
        return json.dumps(await _health(), ensure_ascii=False)

    return server


__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "TOOL_NAMES", "build_server"]

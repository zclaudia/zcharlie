"""Stock directory refresh (mrd-intake D4): baostock ``query_all_stock`` -> ``stock_directory``.

Runs at the end of every successful ``universe_sync`` (weekly). The answer
is a full list of the stocks listed on one trading day; baostock returns an
empty list for a non-trading day and for today before the open, so the sync
steps back day by day (at most :data:`MAX_LOOKBACK_DAYS`) until it gets a
non-empty answer. Rows are upserted, never deleted: a ticker that vanished is
marked ``delisted`` (see :mod:`investment_agent.storage.models.stock_directory`).

Pinyin initials come from ``pypinyin`` (``lazy_pinyin`` with
``Style.FIRST_LETTER``): ``贵州茅台`` -> ``gzmt``. Non-Chinese runs keep their
letters lower-cased (``TCL科技`` -> ``tclkj``, ``万科A`` -> ``wka``); the ``*``
of ``*ST`` and any other punctuation is dropped, ``ST`` stays (``*ST华仪`` ->
``sthy``). Polyphones take pypinyin's first reading - good enough for a
first-letter search.
"""

from __future__ import annotations

import re
from contextlib import AbstractAsyncContextManager
from datetime import date, timedelta
from typing import Protocol

import structlog
from pypinyin import Style, lazy_pinyin
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import shanghai_today
from investment_agent.providers.baostock_reference import BaostockReferenceClient, DirectorySnapshot
from investment_agent.providers.errors import ProviderDataError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.stock_directory import STATUS_LISTED, STATUS_SUSPENDED
from investment_agent.storage.repositories.stock_directory import (
    DirectoryUpsert,
    StockDirectoryRepository,
)

log = structlog.get_logger(__name__)

MAX_LOOKBACK_DAYS = 10
"""Calendar days to step back for a trading-day answer (covers the National Day week)."""

_NON_WORD = re.compile(r"\W+")


class DirectoryClient(Protocol):
    def session(self) -> AbstractAsyncContextManager[None]: ...

    async def fetch_all_stocks(self, day: date) -> DirectorySnapshot: ...


def pinyin_initials(name: str) -> str:
    """Lower-case first letters of ``name`` (``*`` and punctuation dropped)."""

    cleaned = _NON_WORD.sub("", name)
    if not cleaned:
        return ""
    return "".join(
        part.strip().lower() for part in lazy_pinyin(cleaned, style=Style.FIRST_LETTER)
    )


async def initialize_directory(client: DirectoryClient | None = None) -> None:
    """Bootstrap an empty directory independently of the weekly universe job.

    The API runs this in the background so startup remains responsive. Existing
    directories continue to use the weekly refresh, with no upstream search calls.
    """
    try:
        async with session_scope() as session:
            _, total = await StockDirectoryRepository(session).freshness()
        if total:
            return
        client = client or BaostockReferenceClient(timeout_seconds=30, max_attempts=1)
        async with session_scope() as session:
            await sync_directory(session, client, today=shanghai_today())
    except Exception:
        log.exception("directory_initialization_failed", hint="retry `ia snapshot sync-universe --now`")


async def _latest_snapshot(client: DirectoryClient, *, today: date) -> DirectorySnapshot:
    async with client.session():
        for offset in range(MAX_LOOKBACK_DAYS + 1):
            day = today - timedelta(days=offset)
            snapshot = await client.fetch_all_stocks(day)
            if not snapshot.is_empty:
                return snapshot
            log.info("directory_sync_empty_day", day=day.isoformat())
    raise ProviderDataError(
        f"baostock query_all_stock answered nothing for the {MAX_LOOKBACK_DAYS + 1} days "
        f"ending {today.isoformat()}",
        provider="baostock",
    )


async def sync_directory(
    session: AsyncSession, client: DirectoryClient, *, today: date
) -> dict[str, object]:
    """Refresh ``stock_directory`` from the latest trading day on or before ``today``.

    Returns run metadata: ``as_of`` (the trading day answered), ``total`` /
    ``added`` / ``changed`` / ``delisted`` counts and ``provider_rows``
    (stocks in the answer).
    """

    snapshot = await _latest_snapshot(client, today=today)
    entries = [
        DirectoryUpsert(
            ticker=entry.ticker,
            name=entry.name,
            pinyin_initials=pinyin_initials(entry.name),
            status=STATUS_LISTED if entry.trading else STATUS_SUSPENDED,
        )
        for entry in snapshot.entries
    ]
    result = await StockDirectoryRepository(session).upsert_all(
        entries,
        synced_on=snapshot.as_of,
        captured_at=snapshot.captured_at,
        provider=snapshot.source,
    )
    log.info(
        "directory_synced",
        as_of=snapshot.as_of.isoformat(),
        total=result.total,
        added=result.added,
        changed=result.changed,
        delisted=result.delisted,
    )
    return {
        "as_of": snapshot.as_of.isoformat(),
        "provider": snapshot.source,
        "provider_rows": len(entries),
        "total": result.total,
        "added": result.added,
        "changed": result.changed,
        "delisted": result.delisted,
    }


__all__ = [
    "MAX_LOOKBACK_DAYS",
    "DirectoryClient",
    "initialize_directory",
    "pinyin_initials",
    "sync_directory",
]

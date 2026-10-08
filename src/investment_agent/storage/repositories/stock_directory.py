"""``stock_directory`` repository: full upsert, three-way search, freshness (D4)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, case, func, select

from investment_agent.storage.models.stock_directory import (
    DIRECTORY_STATUSES,
    STATUS_DELISTED,
    STATUS_LISTED,
    STATUS_SUSPENDED,
    StockDirectoryEntry,
)
from investment_agent.storage.repositories.base import Repository

# A code query: up to six digits, optionally followed by "." and an exchange
# suffix being typed (``6005``, ``600519.``, ``600519.S``).
_CODE_QUERY = re.compile(r"^\d{1,6}(?:\.[A-Za-z]{0,2})?$")
_PINYIN_QUERY = re.compile(r"^[A-Za-z]+$")

_STATUS_RANK = case(
    (StockDirectoryEntry.status == STATUS_LISTED, 0),
    (StockDirectoryEntry.status == STATUS_SUSPENDED, 1),
    else_=2,
)


@dataclass(frozen=True)
class DirectoryUpsert:
    """One row of a directory answer, ready to store."""

    ticker: str
    name: str
    pinyin_initials: str
    status: str


@dataclass(frozen=True)
class DirectorySyncResult:
    total: int
    """Rows in the table after the sync (including ``delisted`` ones)."""
    added: int
    changed: int
    """Existing rows whose name / initials / status changed (``delisted`` not counted)."""
    delisted: int
    """Rows missing from the answer that were newly marked ``delisted``."""


class StockDirectoryRepository(Repository[StockDirectoryEntry]):
    model = StockDirectoryEntry

    async def upsert_all(
        self,
        entries: Iterable[DirectoryUpsert],
        *,
        synced_on: date,
        captured_at: datetime,
        provider: str,
    ) -> DirectorySyncResult:
        """Replace the directory with ``entries`` without deleting anything.

        Every row of the answer gets the new ``synced_on``; rows absent from
        the answer are marked ``delisted`` (once) and keep their last name.
        """

        existing = {
            row.ticker: row
            for row in (await self.session.execute(select(StockDirectoryEntry))).scalars()
        }
        seen: set[str] = set()
        added = changed = delisted = 0
        for entry in entries:
            if entry.status not in DIRECTORY_STATUSES:
                raise ValueError(f"unknown directory status {entry.status!r}")
            if entry.ticker in seen:
                continue
            seen.add(entry.ticker)
            row = existing.get(entry.ticker)
            if row is None:
                self.session.add(
                    StockDirectoryEntry(
                        ticker=entry.ticker,
                        name=entry.name,
                        pinyin_initials=entry.pinyin_initials,
                        status=entry.status,
                        synced_on=synced_on,
                        provider=provider,
                        captured_at=captured_at,
                    )
                )
                added += 1
                continue
            if (row.name, row.pinyin_initials, row.status) != (
                entry.name,
                entry.pinyin_initials,
                entry.status,
            ):
                row.name = entry.name
                row.pinyin_initials = entry.pinyin_initials
                row.status = entry.status
                changed += 1
            row.synced_on = synced_on
            row.provider = provider
            row.captured_at = captured_at
        for ticker, row in existing.items():
            if ticker not in seen and row.status != STATUS_DELISTED:
                row.status = STATUS_DELISTED
                row.synced_on = synced_on
                row.provider = provider
                row.captured_at = captured_at
                delisted += 1
        await self.session.flush()
        return DirectorySyncResult(
            total=len(existing) + added, added=added, changed=changed, delisted=delisted
        )

    async def search(self, query: str, *, limit: int) -> list[StockDirectoryEntry]:
        """Code prefix > name substring > pinyin-initials prefix, deduplicated.

        Inside each group listed rows come before suspended, then delisted,
        then by ticker. An empty / blank query returns nothing.
        """

        text = query.strip()
        if not text or limit <= 0:
            return []
        groups: list[list[StockDirectoryEntry]] = []
        if _CODE_QUERY.fullmatch(text):
            groups.append(
                await self._rows(StockDirectoryEntry.ticker.startswith(text.upper(), autoescape=True), limit)
            )
        groups.append(
            await self._rows(StockDirectoryEntry.name.contains(text, autoescape=True), limit)
        )
        if _PINYIN_QUERY.fullmatch(text):
            groups.append(
                await self._rows(
                    StockDirectoryEntry.pinyin_initials.startswith(text.lower(), autoescape=True),
                    limit,
                )
            )
        merged: list[StockDirectoryEntry] = []
        seen: set[str] = set()
        for group in groups:
            for row in group:
                if row.ticker in seen:
                    continue
                seen.add(row.ticker)
                merged.append(row)
                if len(merged) >= limit:
                    return merged
        return merged

    async def _rows(
        self, predicate: ColumnElement[bool], limit: int
    ) -> list[StockDirectoryEntry]:
        stmt = (
            select(StockDirectoryEntry)
            .where(predicate)
            .order_by(_STATUS_RANK, StockDirectoryEntry.ticker)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def freshness(self) -> tuple[date | None, int]:
        """``(latest synced_on, row count)``; ``(None, 0)`` before the first sync."""

        stmt = select(func.max(StockDirectoryEntry.synced_on), func.count())
        synced_on, total = (await self.session.execute(stmt)).one()
        return synced_on, int(total or 0)

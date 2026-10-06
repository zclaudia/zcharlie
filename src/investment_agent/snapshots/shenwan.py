"""Shenwan 2021 history -> ``industry_classification`` (``system = sw2021``, P1-A9).

Shared by the ``industry_sync`` job and the backfill ``industry`` part: one
whole-market ``stock_industry_clf_hist_sw`` answer carries every stock's full
change list, so both just rebuild the history of the listings in scope with
:meth:`IndustryRepository.rebuild_timelines` (same interval merge as the CSRC
backfill). Rows coexist with ``csrc`` rows.

Per stock the points are its ``(start_date, code)`` changes up to ``as_of``
(``valid_to`` of one interval = the next ``start_date``) plus a closing point
on ``as_of``, so the provider is authoritative over ``[first change, as_of]``
and changes stored after ``as_of`` survive. Names come from the committed
directory (``config/shenwan_2021.yaml``); intervals of the pre-2021 era
(:func:`is_sw2021_interval` false) and codes missing from the directory keep
the code as their name and are reported. Listings are never created here.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import date
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import A_SHARE_MARKETS, format_listing_ticker
from investment_agent.providers.akshare_shenwan import ShenwanChange, ShenwanHistory
from investment_agent.providers.shenwan_directory import (
    SW2021_CUTOVER,
    SW2021_SYSTEM,
    ShenwanDirectory,
    default_shenwan_directory,
    is_sw2021_interval,
)
from investment_agent.snapshots.collect import ArtifactWriter, fence_writes
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.industry import IndustryRepository, IndustryValue
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import RunLease

log = structlog.get_logger(__name__)

_MAX_LISTED = 50


class ShenwanClient(Protocol):
    async def fetch_industry_history(self) -> ShenwanHistory: ...


def sw2021_points(
    changes: Sequence[ShenwanChange],
    *,
    directory: ShenwanDirectory,
    as_of: date,
) -> tuple[list[tuple[date, IndustryValue | None]], set[str], int]:
    """Points for one stock: ``(points, unresolved sw2021-era codes, legacy intervals)``.

    The era of an interval (and so whether the 2021 directory may name it) is
    judged on the full history: a pre-2021 code stays unnamed even when
    ``as_of`` truncates the history before its 2021-07-30 replacement. Only
    the label depends on it; the stored code is the provider's fact.
    """

    points: list[tuple[date, IndustryValue | None]] = []
    unresolved: set[str] = set()
    legacy = 0
    for index, change in enumerate(changes):
        if change.start_date > as_of:
            break
        valid_to = changes[index + 1].start_date if index + 1 < len(changes) else None
        name: str | None = None
        if is_sw2021_interval(change.start_date, valid_to):
            name = directory.name_of(change.code)
        if name is None:
            if change.start_date >= SW2021_CUTOVER:
                unresolved.add(change.code)  # a 2021 code the directory lacks: refresh it
            else:
                # pre-2021 code: ended before the cutover, or never re-mapped
                # (stocks delisted before 2021 keep their old code open-ended)
                legacy += 1
        points.append((change.start_date, (change.code, name or change.code)))
    if points and points[-1][0] < as_of:
        points.append((as_of, points[-1][1]))
    return points, unresolved, legacy


async def existing_listing_ids(
    session: AsyncSession, tickers: Collection[str] | None = None
) -> dict[str, int]:
    """Canonical ticker -> listing id of existing A-share listings (never creates)."""

    if tickers is not None:
        listings = ListingRepository(session)
        found: dict[str, int] = {}
        for ticker in sorted(set(tickers)):
            listing = await listings.get_a_share_listing(ticker)
            if listing is not None:
                found[ticker] = listing.id
        return found
    market_codes = [market.market_code for market in A_SHARE_MARKETS.values()]
    rows = (
        (await session.execute(select(Listing).where(Listing.market_code.in_(market_codes))))
        .scalars()
        .all()
    )
    return {
        format_listing_ticker(ticker=row.ticker, market_code=row.market_code): row.id
        for row in rows
    }


async def apply_sw2021_history(
    history: ShenwanHistory,
    *,
    scope: Mapping[str, int],
    as_of: date,
    lease: RunLease | None,
    directory: ShenwanDirectory | None = None,
) -> dict[str, object]:
    """Rebuild the ``sw2021`` history of the listings in ``scope`` (ticker -> id)."""

    names = directory or default_shenwan_directory()
    timelines: dict[int, list[tuple[date, IndustryValue | None]]] = {}
    labels: dict[int, str] = {}
    unresolved: dict[str, set[str]] = {}
    legacy_intervals = 0
    missing: list[str] = []
    for ticker, listing_id in sorted(scope.items()):
        changes = history.changes.get(ticker)
        points, codes, legacy = (
            sw2021_points(changes, directory=names, as_of=as_of) if changes else ([], set(), 0)
        )
        if not points:
            missing.append(ticker)
            continue
        timelines[listing_id] = points
        labels[listing_id] = ticker
        legacy_intervals += legacy
        for code in codes:
            unresolved.setdefault(code, set()).add(ticker)
    async with session_scope() as session:
        await fence_writes(session, lease)
        result = await IndustryRepository(session).rebuild_timelines(
            timelines,
            system=SW2021_SYSTEM,
            source=history.source,
            captured_at=history.captured_at,
            labels=labels,
            requested=len(scope),
        )
    if unresolved:
        log.warning(
            "shenwan_codes_unresolved",
            codes=sorted(unresolved)[:20],
            count=len(unresolved),
            hint="run tools/refresh_shenwan_directory.py",
        )
    return {
        "system": SW2021_SYSTEM,
        "provider": history.source,
        "as_of": as_of.isoformat(),
        "captured_at": history.captured_at.isoformat(),
        "directory_version": names.version,
        "provider_tickers": len(history.changes),
        "scope": len(scope),
        "applied": len(timelines),
        "not_in_history": missing[:_MAX_LISTED],
        "not_in_history_count": len(missing),
        "reclassified": list(result.reclassified[:_MAX_LISTED]),
        "reclassified_count": len(result.reclassified),
        "unresolved_codes": {code: sorted(tickers)[:5] for code, tickers in sorted(unresolved.items())},
        "legacy_intervals": legacy_intervals,
        "intervals": {
            "kept": result.kept,
            "reused": result.reused,
            "deleted": result.deleted,
            "inserted": result.inserted,
        },
    }


def write_history_artifact(
    artifacts: ArtifactWriter, history: ShenwanHistory, *, name: str, tickers: Collection[str]
) -> str:
    """Raw rows of the tickers in scope (the whole-market file is ~13k rows)."""

    symbols = {ticker.split(".")[0] for ticker in tickers}
    return artifacts.write(
        name,
        {
            "provider": history.source,
            "operation": "stock_industry_clf_hist_sw",
            "captured_at": history.captured_at.isoformat(),
            "provider_rows": len(history.raw_rows),
            "note": "rows of the listings in scope only",
            "rows": [
                dict(row) for row in history.raw_rows if str(row.get("symbol")) in symbols
            ],
        },
    )

"""``close_quote`` collector (mrd-intake B2, decisions D1 / D2).

At 15:10 on a trading day the job asks the Tencent batch quote feed
(:class:`~investment_agent.providers.tencent_quote.TencentQuoteClient`) for
the day's close of every listing the user may look at - the payload's scope
(target pool) plus the enabled watchlist, open candidates and holdings - and
stores one ``market_snapshot`` row per listing (``provider="tencent"``,
``adjust="none"``, ``captured_at`` from the client clock, ``raw_ref`` to the
run artifact). The daily range hints (P2) can then use the close the same
afternoon instead of the next morning; readers that do not pin a provider get
the baostock bar once the evening ``market_snapshot`` job stored it
(:data:`~investment_agent.storage.repositories.market_snapshot.MARKET_PROVIDER_PRECEDENCE`).

Skipped without a call: listings whose baostock raw bar for the day is
already stored (counted as collected) and listings this job already stored a
Tencent row for (the 16:00 ``retry`` slot re-collects only what was still
``pending`` at 15:10). ``pending`` quotes (session not closed, day not quoted
yet) go to ``Completeness.mark_missing(kind="pending")`` and leave the ratio
alone; a code the feed does not know is ``no_data``.

Failures are isolated per request (60 codes): after
:data:`MAX_CONSECUTIVE_TRANSIENT` consecutive transient failures the job's
breaker stops the run and trips the cross-run circuit for ``tencent`` (A1),
so the next job for the provider fails fast with ``partial``; an already
open circuit (:class:`ProviderUnavailableError`) stops the run before the
first call.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.errors import ProviderUnavailableError
from investment_agent.providers.tencent_quote import PROVIDER, QuoteBatch
from investment_agent.research.retry_policy import is_transient_error
from investment_agent.snapshots.collect import (
    ArtifactWriter,
    CollectionReport,
    Completeness,
    chunked,
    error_reason,
    fence_writes,
)
from investment_agent.snapshots.jobs import CloseQuotePayload
from investment_agent.snapshots.scope import scope_members
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import HoldingItem, WatchlistItem
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease

log = structlog.get_logger(__name__)

CLOSE_QUOTE_PROVIDER = PROVIDER
BAOSTOCK_PROVIDER = "baostock"
ADJUST = "none"
MAX_CONSECUTIVE_TRANSIENT = 3
"""Consecutive failed requests (60 codes each) after which the feed is down."""
_MAX_LISTED_FLAGS = 50


class CloseQuoteClient(Protocol):
    chunk_size: int

    async def fetch_close(self, tickers: Sequence[str], trade_date: date) -> QuoteBatch: ...


@dataclass
class QuoteBreaker:
    """Stops the job after consecutive transient request failures.

    When it trips, the cross-run :class:`CircuitBreaker` (if wired) opens for
    the whole ``tencent`` provider, so the next close-quote job fails fast
    with ``partial`` instead of walking into the same outage.
    """

    limit: int = MAX_CONSECUTIVE_TRANSIENT
    consecutive: int = 0
    reason: str | None = None
    circuit: CircuitBreaker | None = None

    def success(self) -> None:
        self.consecutive = 0

    def failure(self, exc: BaseException) -> None:
        if not is_transient_error(exc):
            self.consecutive = 0
            return
        self.consecutive += 1
        if self.consecutive >= self.limit and self.reason is None:
            self.reason = (
                f"skipped: {CLOSE_QUOTE_PROVIDER} unavailable after {self.consecutive} "
                f"consecutive transient failures ({error_reason(exc)})"
            )
            if self.circuit is not None:
                self.circuit.open_now(CLOSE_QUOTE_PROVIDER, exc)


@dataclass(frozen=True)
class CloseQuoteScope:
    """Ticker -> listing id of every pool, plus where each listing came from."""

    listing_by_ticker: dict[str, int]
    universe: int
    watchlist: int
    candidates: int
    holdings: int

    def to_metadata(self) -> dict[str, object]:
        return {
            "members": len(self.listing_by_ticker),
            "universe": self.universe,
            "watchlist": self.watchlist,
            "candidates": self.candidates,
            "holdings": self.holdings,
        }


async def _a_share_tickers(session: AsyncSession, listing_ids: Sequence[int]) -> dict[str, int]:
    if not listing_ids:
        return {}
    listings = (
        await session.execute(select(Listing).where(Listing.id.in_(list(set(listing_ids)))))
    ).scalars()
    return {
        ticker: listing.id
        for listing in listings
        if (
            ticker := format_listing_ticker(
                ticker=listing.ticker, market_code=listing.market_code
            )
        ).endswith((".SH", ".SZ", ".BJ"))
    }


async def close_quote_scope(
    session: AsyncSession, payload: CloseQuotePayload, trade_date: date
) -> CloseQuoteScope:
    """Target pool (``scope="universe"``) ∪ enabled watchlist ∪ open candidates ∪ holdings.

    The user's pools may be empty; an empty target pool is the usual
    :class:`SnapshotPreconditionError` (run ``universe_sync`` first).
    """

    universe: dict[str, int] = {}
    if payload.scope == "universe":
        universe = await scope_members(session, payload, trade_date)
    watchlist_ids = [
        item.listing_id
        for item in (
            await session.execute(select(WatchlistItem).where(WatchlistItem.enabled.is_(True)))
        ).scalars()
    ]
    candidate_ids = list(
        (
            await session.execute(
                select(CandidateRecord.listing_id).where(CandidateRecord.status == "open")
            )
        ).scalars()
    )
    holding_ids = list((await session.execute(select(HoldingItem.listing_id))).scalars())
    watchlist = await _a_share_tickers(session, watchlist_ids)
    candidates = await _a_share_tickers(session, candidate_ids)
    holdings = await _a_share_tickers(session, holding_ids)
    merged = {**universe, **watchlist, **candidates, **holdings}
    return CloseQuoteScope(
        listing_by_ticker=dict(sorted(merged.items())),
        universe=len(universe),
        watchlist=len(watchlist),
        candidates=len(candidates),
        holdings=len(holdings),
    )


def write_quote_artifact(
    artifacts: ArtifactWriter, name: str, chunk: Sequence[str], batch: QuoteBatch
) -> str:
    """Raw answers of one request (all codes) -> ``raw_ref``."""

    return artifacts.write(
        name,
        {
            "provider": CLOSE_QUOTE_PROVIDER,
            "trade_date": batch.trade_date.isoformat(),
            "captured_at": batch.captured_at.isoformat(),
            "tickers": list(chunk),
            "responses": list(batch.raw),
        },
    )


async def store_quotes(
    market: MarketSnapshotRepository,
    *,
    listing_by_ticker: dict[str, int],
    batch: QuoteBatch,
    raw_ref: str,
) -> int:
    """Insert one raw bar per closed quote; returns the row count."""

    for ticker, quote in batch.quotes.items():
        await market.insert(
            listing_id=listing_by_ticker[ticker],
            trade_date=quote.trade_date,
            open=quote.open,
            high=quote.high,
            low=quote.low,
            close=quote.close,
            volume=quote.volume_shares,
            amount=quote.amount_cny,
            adjust=ADJUST,
            provider=CLOSE_QUOTE_PROVIDER,
            captured_at=batch.captured_at,
            raw_ref=raw_ref,
            pe_ttm=quote.pe,
            turnover=quote.turnover,
            trade_status=quote.trade_status,
            is_st=quote.is_st,
        )
    return len(batch.quotes)


async def collect_close_quotes(
    payload: CloseQuotePayload,
    *,
    client: CloseQuoteClient,
    now: Callable[[], datetime],
    lease: RunLease | None,
    artifacts: ArtifactWriter,
    circuit: CircuitBreaker | None = None,
) -> CollectionReport:
    if payload.provider != CLOSE_QUOTE_PROVIDER:
        raise ValueError(f"close quote collector got provider {payload.provider!r}")
    del now  # captured_at comes from the quote client (its clock)
    trade_date = payload.trade_date
    async with session_scope() as session:
        scope = await close_quote_scope(session, payload, trade_date)
        listing_by_ticker = scope.listing_by_ticker
        listing_ids = list(listing_by_ticker.values())
        repository = MarketSnapshotRepository(session)
        baostock_last = await repository.latest_trade_dates(
            listing_ids, adjust=ADJUST, provider=BAOSTOCK_PROVIDER
        )
        tencent_last = await repository.latest_trade_dates(
            listing_ids, adjust=ADJUST, provider=CLOSE_QUOTE_PROVIDER
        )

    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=CLOSE_QUOTE_PROVIDER)
    baostock_present: list[str] = []
    already_present: list[str] = []
    todo: list[str] = []
    for ticker, listing_id in listing_by_ticker.items():
        stored = baostock_last.get(listing_id)
        if stored is not None and stored >= trade_date:
            baostock_present.append(ticker)
            completeness.mark_collected(ticker)
            continue
        stored = tencent_last.get(listing_id)
        if stored is not None and stored >= trade_date:
            already_present.append(ticker)
            completeness.mark_collected(ticker)
            continue
        todo.append(ticker)

    breaker = QuoteBreaker(circuit=circuit)
    requests = 0
    rows_inserted = 0
    suspended: list[str] = []
    st: list[str] = []
    chunks = list(chunked(todo, max(int(client.chunk_size), 1)))
    for index, chunk in enumerate(chunks):
        if breaker.reason is not None:
            for ticker in chunk:
                completeness.mark_missing(ticker, "failed", breaker.reason)
            continue
        try:
            batch = await client.fetch_close(chunk, trade_date)
            requests += batch.requests
            ref = write_quote_artifact(
                artifacts, f"close-quote-{trade_date.isoformat()}-{index:03d}", chunk, batch
            )
            async with session_scope() as session:
                await fence_writes(session, lease)
                rows_inserted += await store_quotes(
                    MarketSnapshotRepository(session),
                    listing_by_ticker=listing_by_ticker,
                    batch=batch,
                    raw_ref=ref,
                )
        except LeaseLostError:
            raise
        except ProviderUnavailableError as exc:
            # Circuit open (negative cache): nothing was called; stop here.
            remaining = [ticker for item in chunks[index:] for ticker in item]
            report.stop_for_provider(exc, remaining, failures=exc.fail_count)
            break
        except Exception as exc:
            reason = error_reason(exc)
            report.record_failure(exc)
            breaker.failure(exc)
            log.warning(
                "snapshot.close_quote.failed", tickers=len(chunk), chunk=index, error=reason
            )
            for ticker in chunk:
                completeness.mark_missing(ticker, "failed", reason)
            continue
        breaker.success()
        for ticker, quote in batch.quotes.items():
            completeness.mark_collected(ticker)
            if quote.suspended:
                suspended.append(ticker)
            if quote.is_st:
                st.append(ticker)
        for ticker, reason in batch.pending.items():
            completeness.mark_missing(ticker, "pending", reason)
        for ticker, reason in batch.missing.items():
            completeness.mark_missing(ticker, "no_data", reason)

    report.metadata = {
        "provider": CLOSE_QUOTE_PROVIDER,
        "scope": payload.scope,
        "universe_code": payload.universe_code,
        "slot": payload.slot,
        "trade_date": trade_date.isoformat(),
        "adjust": ADJUST,
        "members": scope.to_metadata(),
        "baostock_present": len(baostock_present),
        "already_present": len(already_present),
        "requested": len(todo),
        "tencent_requests": requests,
        "rows_inserted": rows_inserted,
        "pending": completeness.pending[:_MAX_LISTED_FLAGS],
        "flags": {
            "suspended_count": len(suspended),
            "suspended": sorted(suspended)[:_MAX_LISTED_FLAGS],
            "st_count": len(st),
            "st": sorted(st)[:_MAX_LISTED_FLAGS],
        },
        "aborted": breaker.reason,
        "units": (
            "volume in shares (手 × 100), amount in CNY (万元 × 1e4), turnover fraction "
            "(percent / 100), pe_ttm = Tencent field 39; pb / ps not stored"
        ),
    }
    return report


__all__ = [
    "ADJUST",
    "CLOSE_QUOTE_PROVIDER",
    "MAX_CONSECUTIVE_TRANSIENT",
    "CloseQuoteClient",
    "CloseQuoteScope",
    "QuoteBreaker",
    "close_quote_scope",
    "collect_close_quotes",
    "store_quotes",
    "write_quote_artifact",
]

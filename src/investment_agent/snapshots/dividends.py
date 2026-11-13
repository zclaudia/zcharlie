"""baostock dividend events for the target pool (F2 / Q2, migration 017).

``dividend_event`` (provider ``baostock``)
    ``query_dividend_data(yearType="operate")`` per listing and ex-date year:
    every executed or scheduled distribution with an ex-date in that year.
    Rows are de-duplicated per ex-date (baostock repeats preliminary rows,
    see :func:`~investment_agent.providers.baostock_market.merge_dividend_rows`);
    a re-capture is stored as a new version only when its payload changed,
    so the weekly re-collection is idempotent. ``available_from`` is the plan
    announcement (else the implementation announcement / record date).

``dividend_coverage``
    Every successfully queried ``(listing, year)`` is recorded, so the factor
    layer can tell "paid nothing" (covered, no event) from "not collected".

The live job (``dividend_snapshot``) re-collects the payload's ``years``
(default: ``as_of`` year and the year before) every week with the financial
schedule; the history backfill (part ``dividends``,
:mod:`investment_agent.snapshots.backfill`) covers the price window plus one
year. Failures are isolated per listing with the shared transient breaker.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import structlog

from investment_agent.providers.baostock_market import DividendEvent, DividendYear
from investment_agent.providers.wmshare_dividends import WmshareDividendClient
from investment_agent.snapshots.breadth import BAOSTOCK_PROVIDER, TransientBreaker
from investment_agent.snapshots.collect import (
    ArtifactWriter,
    CollectionReport,
    Completeness,
    error_reason,
    fence_writes,
)
from investment_agent.snapshots.jobs import DividendSnapshotPayload
from investment_agent.snapshots.scope import collection_members, user_pools
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease

log = structlog.get_logger(__name__)

_MAX_LISTED = 50


class DividendClient(Protocol):
    def session(self) -> AbstractAsyncContextManager[None]: ...

    async def fetch_dividends(self, symbol: str, year: int) -> DividendYear: ...


@dataclass
class DividendStoreResult:
    inserted: int = 0
    unchanged: int = 0
    events: int = 0

    def add(self, other: DividendStoreResult) -> None:
        self.inserted += other.inserted
        self.unchanged += other.unchanged
        self.events += other.events


def event_payload(event: DividendEvent) -> dict[str, object]:
    return {
        "record_date": event.record_date,
        "pay_date": event.pay_date,
        "announce_date": event.announce_date,
        "implement_date": event.implement_date,
        "cash_per_share_pre_tax": event.cash_per_share,
        "stock_per_share": event.stock_per_share,
        "reserve_per_share": event.reserve_per_share,
    }


def write_dividend_artifact(
    artifacts: ArtifactWriter, name: str, ticker: str, answers: Sequence[DividendYear]
) -> str:
    """Raw dividend answers of one listing (all years) -> ``raw_ref``."""

    return artifacts.write(
        name,
        {
            "provider": BAOSTOCK_PROVIDER,
            "ticker": ticker,
            "year_type": "operate",
            "years": {
                str(answer.year): {
                    "captured_at": answer.captured_at.isoformat(),
                    "rows": [dict(row) for row in answer.raw_rows],
                }
                for answer in answers
            },
        },
    )


async def store_dividend_year(
    repository: DividendRepository,
    *,
    listing_id: int,
    answer: DividendYear,
    raw_ref: str,
    is_backfill: bool = False,
) -> DividendStoreResult:
    """Store one ``(listing, year)`` answer and mark the year covered."""

    result = DividendStoreResult(events=len(answer.events))
    latest = await repository.latest_events(listing_id, [event.ex_date for event in answer.events])
    for event in answer.events:
        row = await repository.upsert_event(
            listing_id=listing_id,
            ex_date=event.ex_date,
            available_from=event.available_from,
            operate_year=answer.year,
            provider=BAOSTOCK_PROVIDER,
            captured_at=answer.captured_at,
            payload=event_payload(event),
            details=dict(event.details),
            raw_ref=raw_ref,
            is_backfill=is_backfill,
            latest=latest.get(event.ex_date),
        )
        if row is None:
            result.unchanged += 1
        else:
            result.inserted += 1
    await repository.record_coverage(
        listing_id=listing_id,
        provider=BAOSTOCK_PROVIDER,
        year=answer.year,
        checked_at=answer.captured_at,
        event_count=len(answer.events),
        is_backfill=is_backfill,
    )
    return result


@dataclass
class DividendBatch:
    """Outcome of collecting dividend years for many listings."""

    stored: DividendStoreResult
    queries: int
    duplicates: dict[str, list[str]]
    combined: dict[str, list[str]]
    no_ex_date: int
    aborted: str | None

    def to_metadata(self) -> dict[str, object]:
        return {
            "baostock_queries": self.queries,
            "events": self.stored.events,
            "inserted": self.stored.inserted,
            "unchanged": self.stored.unchanged,
            "duplicate_rows_merged": sum(len(v) for v in self.duplicates.values()),
            "combined_ex_dates": dict(sorted(self.combined.items())[:_MAX_LISTED]),
            "rows_without_ex_date": self.no_ex_date,
            "aborted": self.aborted,
        }


async def collect_dividend_years(
    plan: dict[str, tuple[int, list[int]]],
    *,
    client: DividendClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    completeness: Completeness,
    report: CollectionReport,
    is_backfill: bool,
    artifact_prefix: str,
) -> DividendBatch:
    """``plan``: ticker -> (listing id, years to query). Marks completeness per ticker."""

    breaker = TransientBreaker()
    stored = DividendStoreResult()
    duplicates: dict[str, list[str]] = {}
    combined: dict[str, list[str]] = {}
    no_ex_date = 0
    queries = 0
    async with client.session():
        for ticker, (listing_id, years) in plan.items():
            if not years:
                completeness.mark_collected(ticker)
                continue
            if breaker.reason is not None:
                completeness.mark_missing(ticker, "failed", breaker.reason)
                continue
            try:
                answers: list[DividendYear] = []
                for year in years:
                    queries += 1
                    answers.append(await client.fetch_dividends(ticker, year))
                ref = write_dividend_artifact(
                    artifacts, f"{artifact_prefix}-{ticker}", ticker, answers
                )
                async with session_scope() as session:
                    await fence_writes(session, lease)
                    repository = DividendRepository(session)
                    for answer in answers:
                        stored.add(
                            await store_dividend_year(
                                repository,
                                listing_id=listing_id,
                                answer=answer,
                                raw_ref=ref,
                                is_backfill=is_backfill,
                            )
                        )
            except LeaseLostError:
                raise
            except Exception as exc:
                reason = error_reason(exc)
                report.record_failure(exc)
                breaker.failure(exc)
                log.warning("snapshot.dividends.failed", ticker=ticker, error=reason)
                completeness.mark_missing(ticker, "failed", reason)
                continue
            breaker.success()
            completeness.mark_collected(ticker)
            for answer in answers:
                if answer.duplicates:
                    duplicates.setdefault(ticker, []).extend(answer.duplicates)
                if answer.combined:
                    combined.setdefault(ticker, []).extend(answer.combined)
                no_ex_date += answer.no_ex_date
    return DividendBatch(stored, queries, duplicates, combined, no_ex_date, breaker.reason)


async def collect_baostock_dividends(
    payload: DividendSnapshotPayload,
    *,
    client: DividendClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    now: Callable[[], datetime],
) -> CollectionReport:
    """Live weekly job: re-collect ``payload.years`` for every scope member."""

    del now  # captured_at comes from the provider client (its clock)
    async with session_scope() as session:
        listing_by_ticker = await collection_members(session, payload, payload.as_of)
        if payload.exclude_watchlist:
            watchlist = (await user_pools(session)).watchlist
            listing_by_ticker = {t: v for t, v in listing_by_ticker.items() if t not in watchlist}
    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness, provider=BAOSTOCK_PROVIDER)
    plan = {
        ticker: (listing_id, list(payload.years))
        for ticker, listing_id in sorted(listing_by_ticker.items())
    }
    batch = await collect_dividend_years(
        plan,
        client=client,
        artifacts=artifacts,
        lease=lease,
        completeness=completeness,
        report=report,
        is_backfill=False,
        artifact_prefix=f"dividends-{payload.as_of.isoformat()}",
    )
    report.metadata = {
        "provider": BAOSTOCK_PROVIDER,
        "scope": payload.scope,
        "universe_code": payload.universe_code,
        "as_of": payload.as_of.isoformat(),
        "years": list(payload.years),
        "year_type": "operate (ex-date year)",
        **batch.to_metadata(),
    }
    return report


async def collect_wmshare_dividends(
    payload: DividendSnapshotPayload,
    *,
    client: WmshareDividendClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> CollectionReport:
    """Capture the enabled watchlist, saving each stock in its own transaction."""
    from dataclasses import asdict

    from investment_agent.dividends.service import persist_wmshare_capture
    from investment_agent.snapshots.collect import SnapshotPreconditionError

    async with session_scope() as session:
        members = await collection_members(session, payload, payload.as_of)
    if payload.tickers:
        missing = set(payload.tickers) - set(members)
        if missing:
            raise SnapshotPreconditionError(
                f"tickers are not enabled watchlist members: {', '.join(sorted(missing))}"
            )
        members = {ticker: members[ticker] for ticker in payload.tickers}
    completeness = Completeness(expected=sorted(members))
    report = CollectionReport(completeness, provider="wmshare")
    costs: dict[str, dict[str, int | None]] = {}
    parts: dict[str, dict[str, str | None]] = {}
    stored: dict[str, object] = {}
    stopped = False
    partial_parts = False
    for ticker, listing_id in sorted(members.items()):
        if stopped:
            completeness.mark_missing(ticker, "stopped", "wmshare authentication or quota stop")
            continue
        try:
            capture = await client.fetch(ticker, payload.as_of)
            costs[ticker] = capture.ec
            raw_ref = artifacts.write(f"wmshare-dividends-{ticker}", asdict(capture))
            async with session_scope() as session:
                await fence_writes(session, lease)
                stored[ticker] = await persist_wmshare_capture(
                    session, listing_id, capture, raw_ref=raw_ref
                )
            parts[ticker] = {"history": capture.history_error, "snapshot": capture.snapshot_error}
            errors = [e for e in (capture.history_error, capture.snapshot_error) if e]
            if capture.history_error is None and not set(payload.years).issubset(
                capture.covered_years
            ):
                errors.append("dividend history coverage is incomplete")
            if errors and (capture.history_error is None or capture.snapshot is not None):
                partial_parts = True
            if capture.terminal:
                stopped = True
                completeness.mark_missing(ticker, "stopped", "; ".join(errors))
            elif errors:
                completeness.mark_missing(ticker, "failed", "; ".join(errors))
                report.all_failures_transient = False
            else:
                completeness.mark_collected(ticker)
        except LeaseLostError:
            raise
        except Exception as exc:
            report.record_failure(exc)
            completeness.mark_missing(ticker, "failed", error_reason(exc))
    known = [cost for by_part in costs.values() for cost in by_part.values() if cost is not None]
    report.metadata = {
        "provider": "wmshare",
        "scope": payload.scope,
        "as_of": payload.as_of.isoformat(),
        "ec_by_ticker": costs,
        "ec_known_total": sum(known),
        "ec_unknown_calls": sum(
            cost is None for by_part in costs.values() for cost in by_part.values()
        ),
        "parts": parts,
        "stored": stored,
        "aborted": stopped,
        "partial_parts": partial_parts,
    }
    return report

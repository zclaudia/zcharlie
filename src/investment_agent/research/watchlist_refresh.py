"""Batch enqueueing for durable watchlist research refreshes."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository

_SUPPORTED_PROVIDERS = {"ifind", "ifind-proxy"}
_TERMINAL_STATUSES = {"success", "partial", "failed"}


@dataclass(frozen=True)
class WatchlistRefreshOutcome:
    listing_id: int
    ticker: str | None
    run_id: int | None
    status: str
    created: bool
    error: str | None = None


async def enqueue_watchlist_refresh(
    session: AsyncSession,
    *,
    as_of: date,
    triggered_by: str,
    idempotency_key: str | None = None,
) -> list[WatchlistRefreshOutcome]:
    """Queue each enabled entry independently so one bad item cannot abort the batch."""

    watchlist_repository = WatchlistRepository(session)
    pipeline_repository = PipelineRunRepository(session)
    items = await watchlist_repository.list_enabled()
    outcomes: list[WatchlistRefreshOutcome] = []
    for item in items:
        listing = await session.get(Listing, item.listing_id)
        ticker = (
            format_listing_ticker(
                ticker=listing.ticker,
                market_code=listing.market_code,
            )
            if listing is not None
            else None
        )
        validation_error = _validate_refresh_item(item=item, ticker=ticker)
        if validation_error is not None:
            await watchlist_repository.mark_refresh_enqueue_failed(
                item.listing_id,
                error=validation_error,
            )
            outcomes.append(
                WatchlistRefreshOutcome(
                    listing_id=item.listing_id,
                    ticker=ticker,
                    run_id=None,
                    status="failed",
                    created=False,
                    error=validation_error,
                )
            )
            continue

        assert ticker is not None
        assert item.industry is not None
        assert item.report_year is not None
        payload: dict[str, object] = {
            "ticker": ticker,
            "industry": item.industry,
            "report_year": item.report_year,
            "as_of": as_of.isoformat(),
        }
        try:
            async with session.begin_nested():
                run, created = await pipeline_repository.enqueue(
                    kind="ifind_research",
                    idempotency_key=_child_idempotency_key(
                        idempotency_key,
                        listing_id=item.listing_id,
                    ),
                    triggered_by=triggered_by,
                    listing_id=item.listing_id,
                    request_json=payload,
                )
                if run.status in _TERMINAL_STATUSES:
                    await watchlist_repository.mark_refresh_finished(
                        item.listing_id,
                        run_id=run.id,
                        status=run.status,
                        refreshed_at=run.finished_at or datetime.now(UTC),
                        error=run.error_summary,
                    )
                else:
                    await watchlist_repository.mark_refresh_queued(
                        item.listing_id,
                        run_id=run.id,
                        status=run.status,
                    )
            outcomes.append(
                WatchlistRefreshOutcome(
                    listing_id=item.listing_id,
                    ticker=ticker,
                    run_id=run.id,
                    status=run.status,
                    created=created,
                    error=run.error_summary,
                )
            )
        except Exception as exc:
            error = f"enqueue failed: {type(exc).__name__}: {exc}"
            await watchlist_repository.mark_refresh_enqueue_failed(
                item.listing_id,
                error=error,
            )
            outcomes.append(
                WatchlistRefreshOutcome(
                    listing_id=item.listing_id,
                    ticker=ticker,
                    run_id=None,
                    status="failed",
                    created=False,
                    error=error,
                )
            )
    return outcomes


def _validate_refresh_item(
    *,
    item: WatchlistItem,
    ticker: str | None,
) -> str | None:
    if ticker is None:
        return "listing is missing"
    if parse_a_share_ticker(ticker) is None:
        return "only canonical A-share tickers are supported"
    if not item.industry:
        return "industry is required"
    if item.report_year is None:
        return "report_year is required"
    if item.provider not in _SUPPORTED_PROVIDERS:
        return f"unsupported research provider: {item.provider}"
    return None


def _child_idempotency_key(
    batch_key: str | None,
    *,
    listing_id: int,
) -> str | None:
    if batch_key is None:
        return None
    digest = sha256(batch_key.encode("utf-8")).hexdigest()[:32]
    return f"watchlist:{digest}:{listing_id}"


async def enqueue_single_stock_research(
    session: AsyncSession,
    *,
    listing_id: int,
    ticker: str,
    industry: str,
    report_year: int,
    as_of: date,
    triggered_by: str,
    idempotency_key: str | None = None,
) -> tuple[PipelineRun, bool]:
    """Queue one ``ifind_research`` run for a watchlist entry (same payload as
    ``POST /research/single-stock``) and track it on the entry."""

    payload: dict[str, object] = {
        "ticker": ticker,
        "industry": industry,
        "report_year": report_year,
        "as_of": as_of.isoformat(),
    }
    run, created = await PipelineRunRepository(session).enqueue(
        kind="ifind_research",
        idempotency_key=idempotency_key,
        triggered_by=triggered_by,
        listing_id=listing_id,
        request_json=payload,
    )
    if run.status not in _TERMINAL_STATUSES:
        await WatchlistRepository(session).mark_refresh_queued(
            listing_id, run_id=run.id, status=run.status
        )
    return run, created

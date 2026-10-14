"""/watchlist endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.admin import (
    TemplateSuggestionOut,
    WatchlistAdd,
    WatchlistItemOut,
    WatchlistRefreshItemOut,
    WatchlistRefreshOut,
    WatchlistRefreshRequest,
    WatchlistRemove,
    WatchlistRuleApproveRequest,
    WatchlistRuleDraftRequest,
    WatchlistRuleEditRequest,
    WatchlistRuleOut,
    WatchlistRuleRejectRequest,
    WatchlistUpdate,
)
from investment_agent.api.views import watchlist_item_out
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.domain.a_share import parse_a_share_ticker
from investment_agent.research.watchlist_refresh import (
    enqueue_watchlist_refresh,
    research_source_mode,
)
from investment_agent.rules.recommend import suggest_template
from investment_agent.rules.validation import RuleValidationError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.research_snapshot import ResearchSnapshotRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from investment_agent.watchlist.drafts import (
    WatchDraftError,
    approve_draft,
    edit_draft_params,
    redraft,
    reject_draft,
    remove_entry,
    rule_snapshot,
    sync_add_state,
)

router = APIRouter(
    prefix="/watchlist",
    tags=["watchlist"],
    dependencies=[Depends(verify_api_key_or_session)],
)


@router.get("", response_model=list[WatchlistItemOut])
async def list_watchlist() -> list[WatchlistItemOut]:
    async with session_scope() as session:
        items = await WatchlistRepository(session).list_all()
        return [await watchlist_item_out(session, item) for item in items]


@router.post("", response_model=WatchlistItemOut, status_code=201)
async def add_watchlist(body: WatchlistAdd) -> WatchlistItemOut:
    async with session_scope() as session:
        watchlist_repository = WatchlistRepository(session)
        parsed = parse_a_share_ticker(body.ticker)
        industry = body.industry
        report_year = body.report_year
        if parsed is not None:
            symbol, market = parsed
            if body.market_code is not None and body.market_code != market.market_code:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"market_code {body.market_code} conflicts with ticker suffix "
                        f"{market.suffix}"
                    ),
                )
            listing = await ListingRepository(session).get_by_ticker(
                market.market_code,
                symbol,
            )
            history = await ResearchSnapshotRepository(session).history(
                symbol=symbol,
                market=market.suffix,
                limit=1,
            )
            latest_snapshot = history[0] if history else None
            name = body.name or (
                latest_snapshot.company_name if latest_snapshot is not None else None
            )
            industry = industry or (
                latest_snapshot.industry if latest_snapshot is not None else None
            )
            report_year = report_year or (
                latest_snapshot.report_year if latest_snapshot is not None else None
            )
            existing_item = (
                await watchlist_repository.get_by_listing(listing.id)
                if listing is not None
                else None
            )
            if industry is None and existing_item is not None:
                industry = existing_item.industry
            if report_year is None and existing_item is not None:
                report_year = existing_item.report_year
            requested_provider = body.provider or (
                existing_item.provider if existing_item is not None else "auto"
            )
            # Snapshot research (P1-B5) falls back to the data industry group;
            # only iFinD deep research needs the industry text.
            if industry is None and research_source_mode(requested_provider) == "ifind":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "industry is required when no prior research snapshot or "
                        "watchlist metadata exists"
                    ),
                )
            if listing is None:
                if name is None:
                    raise HTTPException(
                        status_code=422,
                        detail="name is required when no prior research snapshot exists",
                    )
                listing = await ListingRepository(session).ensure_a_share_listing(
                    f"{symbol}.{market.suffix}",
                    company_name=name,
                )
        else:
            market_code = body.market_code or "NASDAQ"
            listing = (
                await session.execute(
                    select(Listing).where(
                        Listing.market_code == market_code,
                        Listing.ticker == body.ticker,
                    ),
                )
            ).scalar_one_or_none()
        if listing is None:
            raise HTTPException(
                status_code=404,
                detail=f"Listing not found: {body.market_code or 'NASDAQ'}/{body.ticker}",
            )
        existing_item = await watchlist_repository.get_by_listing(listing.id)
        # New A-share entries refresh from the free-source snapshot store
        # (``auto`` -> snapshot, P1-B5); ``ifind`` is an explicit opt-in.
        provider = body.provider or (
            existing_item.provider if existing_item is not None else "auto"
        )
        item = await watchlist_repository.add(
            listing.id,
            body.note,
            industry=industry,
            provider=provider,
            report_year=report_year,
            enabled=body.enabled,
        )
        try:
            await sync_add_state(
                session,
                item,
                template_id=body.template_id,
                as_of=shanghai_today(),
                triggered_by="watchlist_api",
            )
        except WatchDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return await watchlist_item_out(session, item)


@router.post(
    "/refresh",
    response_model=WatchlistRefreshOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_watchlist(
    body: WatchlistRefreshRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r".*\S.*",
        ),
    ] = None,
) -> WatchlistRefreshOut:
    """Queue independent research jobs for every enabled watchlist item."""

    refresh_date = body.as_of or shanghai_today()
    async with session_scope() as session:
        outcomes = await enqueue_watchlist_refresh(
            session,
            as_of=refresh_date,
            triggered_by="watchlist_api",
            idempotency_key=idempotency_key,
            ifind_available=bool(settings.ifind_proxy_api_key),
        )
    items = [WatchlistRefreshItemOut(**outcome.__dict__) for outcome in outcomes]
    return WatchlistRefreshOut(
        as_of=refresh_date,
        total=len(items),
        created_runs=sum(item.created for item in items),
        reused_runs=sum(item.run_id is not None and not item.created for item in items),
        failed=sum(item.status == "failed" for item in items),
        items=items,
    )


@router.patch("/{listing_id}", response_model=WatchlistItemOut)
async def update_watchlist_item(listing_id: int, body: WatchlistUpdate) -> WatchlistItemOut:
    async with session_scope() as session:
        item = await WatchlistRepository(session).set_enabled(
            listing_id, enabled=body.enabled
        )
        if item is None:
            raise HTTPException(
                status_code=404,
                detail=f"No watchlist entry for listing_id={listing_id}",
            )
        return await watchlist_item_out(session, item)


async def _watch_item_or_404(session: AsyncSession, listing_id: int) -> WatchlistItem:
    item = await WatchlistRepository(session).get_by_listing(listing_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=f"No watchlist entry for listing_id={listing_id}",
        )
    return item


@router.get("/template-suggestion", response_model=TemplateSuggestionOut)
async def template_suggestion(
    ticker: Annotated[str, Query(min_length=1, max_length=32)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TemplateSuggestionOut:
    """Best-effort drafting-template suggestion for the add form (plan 1.5).

    Returns empty fields when nothing could be suggested; the entry can still
    be added and drafted later with an explicit template choice.
    """

    parsed = parse_a_share_ticker(ticker.strip().upper())
    if parsed is None:
        raise HTTPException(status_code=422, detail=f"not an A-share ticker: {ticker}")
    symbol, market = parsed
    async with session_scope() as session:
        suggestion = await suggest_template(
            session,
            ticker=f"{symbol}.{market.suffix}",
            as_of=shanghai_today(),
            settings=settings,
        )
    if suggestion is None:
        return TemplateSuggestionOut()
    return TemplateSuggestionOut(**suggestion.__dict__)


@router.post("/{listing_id}/rule/draft", response_model=WatchlistRuleOut)
async def draft_watch_rule(
    listing_id: int, body: WatchlistRuleDraftRequest
) -> WatchlistRuleOut:
    """(Re-)draft the entry's rule; the template falls back to the newest intent."""

    async with session_scope() as session:
        item = await _watch_item_or_404(session, listing_id)
        try:
            await redraft(
                session,
                item,
                as_of=shanghai_today(),
                template_id=body.template_id,
                triggered_by="watchlist_api",
            )
        except WatchDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return WatchlistRuleOut.model_validate(await rule_snapshot(session, item))


@router.post("/{listing_id}/rule/approve", response_model=WatchlistRuleOut)
async def approve_watch_rule(
    listing_id: int, body: WatchlistRuleApproveRequest
) -> WatchlistRuleOut:
    """Approve the entry's current draft (previous active version retires)."""

    async with session_scope() as session:
        item = await _watch_item_or_404(session, listing_id)
        try:
            await approve_draft(
                session,
                item,
                rule_instance_id=body.rule_instance_id,
                reason=body.reason,
                expected_active_rule_instance_id=body.expected_active_rule_instance_id,
            )
        except WatchDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return WatchlistRuleOut.model_validate(await rule_snapshot(session, item))


@router.post("/{listing_id}/rule/edit", response_model=WatchlistRuleOut)
async def edit_watch_rule(
    listing_id: int, body: WatchlistRuleEditRequest
) -> WatchlistRuleOut:
    """Save a new draft version with ``params`` merged over the current draft."""

    async with session_scope() as session:
        item = await _watch_item_or_404(session, listing_id)
        try:
            await edit_draft_params(
                session,
                item,
                rule_instance_id=body.rule_instance_id,
                params=body.params,
                reason=body.reason,
            )
        except WatchDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except RuleValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return WatchlistRuleOut.model_validate(await rule_snapshot(session, item))


@router.post("/{listing_id}/rule/reject", response_model=WatchlistRuleOut)
async def reject_watch_rule(
    listing_id: int, body: WatchlistRuleRejectRequest
) -> WatchlistRuleOut:
    """Reject the current draft with a required reason (no automatic re-draft)."""

    async with session_scope() as session:
        item = await _watch_item_or_404(session, listing_id)
        try:
            await reject_draft(
                session,
                item,
                rule_instance_id=body.rule_instance_id,
                reason=body.reason,
            )
        except WatchDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return WatchlistRuleOut.model_validate(await rule_snapshot(session, item))


async def _remove_entry_response(listing_id: int, body: WatchlistRemove) -> Response:
    """Shared by DELETE /{listing_id} and POST /{listing_id}/remove.

    ``DELETE`` with a JSON body is the legacy wire shape (some proxies drop
    request bodies, which would 422); the POST form is the same operation
    without that hazard. Both stay live until the web client migrates.
    """

    async with session_scope() as session:
        item = await _watch_item_or_404(session, listing_id)
        try:
            await remove_entry(session, item, reason=body.reason)
        except WatchDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(status_code=204)


@router.delete("/{listing_id}", status_code=204)
async def remove_watchlist(listing_id: int, body: WatchlistRemove) -> Response:
    """Remove an observation entry; the reason is journaled (plan 1.5 移除).

    Legacy shape (JSON body on DELETE); kept until the web client moves to the
    POST form below.
    """

    return await _remove_entry_response(listing_id, body)


@router.post("/{listing_id}/remove", status_code=204)
async def remove_watchlist_post(listing_id: int, body: WatchlistRemove) -> Response:
    """Remove an observation entry (proxy-safe POST form of the DELETE above)."""

    return await _remove_entry_response(listing_id, body)

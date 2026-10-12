"""Authenticated user fact endpoints; intentionally absent from model tools."""
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.config import Settings
from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.calculation_contracts import PortfolioCalculationRequest
from investment_agent.portfolio.calculations import PortfolioCalculationService
from investment_agent.portfolio.contracts import (
    Account,
    AppendEvent,
    AppendResult,
    ApplyCorrection,
    CorrectionResult,
    CreateAccount,
    EventPage,
    EventRecord,
    ImportBatch,
    ImportPreview,
    ImportResult,
    Snapshot,
)
from investment_agent.portfolio.plan_contracts import PlanView
from investment_agent.portfolio.purposes import (
    PurposeRequest,
    PurposeService,
    PurposeVersion,
    PurposeView,
)
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.valuation import Valuation, ValuationService
from investment_agent.scheduler.trading_calendar import (
    UnknownTradingCalendarYearError,
    load_trading_calendar,
)
from investment_agent.settings.loader import (
    DEFAULT_APP_YAML,
    configured_calendar_path,
    resolved_calendar_path,
)
from investment_agent.storage.database import session_scope

router = APIRouter(prefix="/portfolio", tags=["portfolio"], dependencies=[Depends(verify_api_key_or_session)])

logger = structlog.get_logger(__name__)


async def service() -> AsyncIterator[PortfolioService]:
    try:
        async with session_scope() as session:
            yield PortfolioService(session, "local_owner")
    except InvestorError as exc:
        code = 403 if exc.detail.code == "writes_disabled" else 404 if exc.detail.code == "not_found" else 422 if exc.detail.code == "invalid_reference" else 409
        raise HTTPException(code, detail=exc.detail.model_dump(mode="json")) from exc


Service = Annotated[PortfolioService, Depends(service, scope="function")]


async def valuation_service() -> AsyncIterator[ValuationService]:
    try:
        async with session_scope() as session:
            yield ValuationService(session, "local_owner")
    except InvestorError as exc:
        code = 403 if exc.detail.code == "writes_disabled" else 404 if exc.detail.code == "not_found" else 422 if exc.detail.code == "invalid_reference" else 409
        raise HTTPException(code, detail=exc.detail.model_dump(mode="json")) from exc


ValuationSvc = Annotated[ValuationService, Depends(valuation_service, scope="function")]


def trading_calendar_predicate(settings: Settings) -> Callable[[date], bool | None]:
    """Reviewed years use the calendar; an unreviewed year is ``None``.

    ``None`` is not "weekday": staleness cannot be settled then, and the
    valuation reports a limitation instead (plan 2.10 / WP10).

    Same source as the scheduler and the dashboard: the DATA_DIR copy applied
    from the health page wins over the repository baseline, so an applied
    calendar takes effect on the next staleness decision without a restart.
    """

    try:
        baseline = configured_calendar_path(DEFAULT_APP_YAML)
        calendar = load_trading_calendar(
            resolved_calendar_path(baseline, data_dir=Path(settings.data_dir))
        )
    except Exception as exc:
        raise InvestorError("invalid_reference", "交易日历不可用，无法确认价格时效") from exc

    def predicate(value: date) -> bool | None:
        try:
            return calendar.is_trading_day(value)
        except UnknownTradingCalendarYearError:
            return None

    return predicate


async def _enqueue_daily_valuation(
    svc: PortfolioService | ValuationService, account_id: UUID
) -> None:
    """Facts changed (or the stored valuation is older than required): queue the
    deterministic daily valuation. Reads never compute it (plan 2.10 / WP6)."""

    from investment_agent.clock import shanghai_today
    from investment_agent.portfolio.valuation_jobs import enqueue_valuation

    try:
        await enqueue_valuation(
            svc.session,
            account_id=account_id,
            valuation_date=shanghai_today(),
            # No explicit cutoff: enqueue_valuation derives the deterministic
            # day-end cutoff, so the cron and the API share one idempotency
            # identity instead of colliding on a wall-clock payload.
            triggered_by="portfolio_api",
        )
    except Exception:
        logger.exception("portfolio.valuation.enqueue_failed", account_id=str(account_id))


async def enabled(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not settings.portfolio_ledger_enabled:
        raise HTTPException(403, "Portfolio ledger writes are disabled")


@router.post("/accounts", dependencies=[Depends(enabled)])
async def create_account(body: CreateAccount, svc: Service) -> Account:
    return await svc.create_account(body)


@router.get("/accounts")
async def accounts(svc: Service) -> tuple[Account, ...]:
    return await svc.accounts()


@router.post("/events", dependencies=[Depends(enabled)])
async def append_event(body: AppendEvent, svc: Service) -> AppendResult:
    result = await svc.append(body)
    await _enqueue_daily_valuation(svc, result.snapshot.account_id)
    return result


@router.post("/imports/preview", dependencies=[Depends(enabled)])
async def preview_import(body: ImportBatch, svc: Service) -> ImportPreview:
    """Server-side preview of the whole batch on the caller's base revision.

    Returns each row's inclusion, quantity/cost/cash effect and every conflict;
    a base revision older than the current facts is refused (preview again).
    """

    return await svc.preview_import(body)


@router.post("/imports", dependencies=[Depends(enabled)])
async def apply_import(body: ImportBatch, svc: Service) -> ImportResult:
    """Commit the whole batch under one account CAS (plan 2.8); retries replay."""

    result = await svc.apply_import(body)
    await _enqueue_daily_valuation(svc, result.snapshot.account_id)
    return result


@router.post("/corrections", dependencies=[Depends(enabled)])
async def correct_events(body: ApplyCorrection, svc: Service) -> CorrectionResult:
    """Atomic, authenticated correction with exact-body receipt replay."""
    result = await svc.apply_correction(body)
    await _enqueue_daily_valuation(svc, result.snapshot.account_id)
    return result


@router.get("/events")
async def events(svc: Service, account_id: UUID, after_revision: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=200)] = 100) -> EventPage:
    return await svc.events(account_id, after_revision=after_revision, limit=limit)


@router.get("/events/{event_id}")
async def event(event_id: UUID, account_id: UUID, svc: Service) -> EventRecord:
    return await svc.event(account_id, event_id)


@router.get("/snapshot")
async def snapshot(svc: Service, account_id: UUID, snapshot_id: UUID | None = None, revision: Annotated[int | None, Query(ge=1)] = None) -> Snapshot:
    return await svc.snapshot(account_id, snapshot_id=snapshot_id, revision=revision)


@router.post("/calculations", dependencies=[Depends(enabled)])
async def calculate_portfolio(body: PortfolioCalculationRequest, svc: Service) -> CalculationRecord:
    return await PortfolioCalculationService(svc.session, svc.owner).save(body)


@router.get("/calculations/{calculation_id}")
async def portfolio_calculation(calculation_id: UUID, account_id: UUID, svc: Service) -> CalculationRecord:
    return await PortfolioCalculationService(svc.session, svc.owner).get(account_id, calculation_id)


@router.get("/plans/current")
async def current_plan(account_id: UUID, svc: Service) -> PlanView | None:
    from investment_agent.portfolio.plans import PortfolioPlanService
    return await PortfolioPlanService(svc.session, svc.owner).current(account_id)


@router.get("/plans/{plan_id}")
async def portfolio_plan(plan_id: UUID, account_id: UUID, svc: Service) -> PlanView:
    from investment_agent.investor.common import ObjectRef
    from investment_agent.portfolio.plans import PortfolioPlanService
    view = await PortfolioPlanService(svc.session, svc.owner).get(ObjectRef(kind="portfolio_plan", id=plan_id, revision=1))
    if view.plan.account_id != account_id:
        raise InvestorError("not_found", "Plan unavailable for account")
    return view


@router.get("/artifacts/{kind}/{artifact_id}")
async def portfolio_artifact(kind: Literal["draft", "publication_check", "review"], artifact_id: UUID, account_id: UUID, svc: Service) -> dict[str, Any]:
    """Public plan candidate/check/review only; account and owner are exact."""
    from investment_agent.investor.common import ObjectRef
    from investment_agent.portfolio.plans import PortfolioPlanService
    row = await PortfolioPlanService(svc.session, svc.owner).artifact(ObjectRef(kind=kind, id=artifact_id, revision=1))
    if row.account_id != str(account_id):
        raise InvestorError("not_found", "Portfolio artifact unavailable for account")
    return row.payload


@router.get("/purposes/current")
async def current_purposes(account_id: UUID, svc: Service) -> PurposeView | None:
    return await PurposeService(svc.session, svc.owner).current(account_id)


@router.post("/purposes")
async def confirm_purposes(body: PurposeRequest, svc: Service, settings: Annotated[Settings, Depends(get_settings)]) -> PurposeVersion:
    return await PurposeService(svc.session, svc.owner).confirm(body, enabled=settings.portfolio_ledger_enabled)


@router.get("/valuations/current")
async def current_valuation(
    account_id: UUID,
    svc: ValuationSvc,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Valuation:
    """The stored current valuation - reads never compute (plan 2.10 / WP6).

    Missing, or older than the last required trading day: the deterministic
    daily job is enqueued (idempotent) and the read reports "更新中" (409) when
    nothing can be shown yet; a stored older result is returned as is.
    """

    from investment_agent.clock import shanghai_today
    from investment_agent.portfolio.valuation_jobs import required_valuation_date

    stored = await svc.current(account_id)
    required = required_valuation_date(shanghai_today(), trading_calendar_predicate(settings))
    if stored is None:
        await _enqueue_daily_valuation(svc, account_id)
        raise HTTPException(409, detail="估值更新中：当日估值已入队，请稍后刷新")
    if required is not None and stored.valuation_date < required:
        await _enqueue_daily_valuation(svc, account_id)
    return stored


@router.get("/valuations/history")
async def valuation_history(
    account_id: UUID,
    svc: ValuationSvc,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> tuple[Valuation, ...]:
    return await svc.history(account_id, limit=limit)


@router.get("/valuations/{valuation_id}")
async def valuation(account_id: UUID, valuation_id: UUID, svc: ValuationSvc) -> Valuation:
    return await svc.get(account_id, valuation_id)


@router.get("/listings")
async def portfolio_listings(
    svc: Service,
    ids: Annotated[str, Query(min_length=1, max_length=512)],
) -> list[dict[str, Any]]:
    """Display identities for the entry/valuation tables (ticker and name only)."""

    from investment_agent.domain.a_share import format_listing_ticker
    from investment_agent.storage.models.reference import Company, Listing

    parsed = [int(part) for part in ids.split(",") if part.strip().isdigit()]
    if not parsed or len(parsed) > 100:
        raise HTTPException(422, detail="ids must list 1 to 100 listing ids")
    rows = list((await svc.session.scalars(select(Listing).where(Listing.id.in_(parsed)))).all())
    out: list[dict[str, Any]] = []
    for listing in rows:
        company = await svc.session.get(Company, listing.company_id)
        out.append(
            {
                "listing_id": listing.id,
                "ticker": format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code),
                "name": company.canonical_name if company is not None else None,
            }
        )
    return out


@router.get("/listing")
async def resolve_listing(
    svc: Service,
    ticker: Annotated[str, Query(min_length=1, max_length=32)],
    name: Annotated[str | None, Query(max_length=256)] = None,
) -> dict[str, Any]:
    """Resolve an A-share ticker to its canonical listing reference (entry forms).

    Idempotently ensures the listing row exists; it never invents a price or a
    fact.
    """

    from investment_agent.storage.repositories.listing import ListingRepository

    try:
        listing = await ListingRepository(svc.session).ensure_a_share_listing(ticker, company_name=name)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return {
        "listing_ref": ObjectRef(kind="listing", id=listing.id, revision=1).model_dump(mode="json"),
        "ticker": ticker.upper(),
        "name": name,
    }

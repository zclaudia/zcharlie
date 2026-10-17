"""/research endpoints for HK/US material: read context and refresh registration (plan 09).

Two endpoints, both authenticated by :func:`verify_api_key_or_session` (API key
or same-origin session cookie; a state-changing request additionally passes the
CSRF origin check of the auth dependency):

* ``GET /research/listings/{listing_id}/financial-context`` — strictly read
  only: it answers from the local rows and the checked identity list, opens one
  read session, and never touches an upstream source (the read model it serves
  cannot even import a fetcher). 404 for an unknown listing; an existing listing
  outside the checked list is answered with an explicit not-covered view.
* ``POST /research/listings/{listing_id}/refresh`` — validates the requested
  scope and registers one deduplicated audit run; 404 for an unknown listing,
  422 for a refused request. Per-source policy decisions explain whether a
  worker may fetch; registration never grants fetch permission.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.research_materials import (
    FactOut,
    FinancialContextResponse,
    FiscalYearOut,
    FreshnessOut,
    MetricCoverageOut,
    MetricOut,
    RefreshBody,
    RefreshLifecycleOut,
    RefreshResponse,
    StatusOut,
)
from investment_agent.materials.context import (
    FinancialContextView,
    ListingNotFoundError,
    financial_context,
)
from investment_agent.materials.refresh import (
    DEFAULT_REASON,
    ERROR_INVALID_PERIOD,
    ERROR_INVALID_REASON,
    ERROR_LISTING_NOT_COVERED,
    ERROR_NOT_FOUND,
    ERROR_UNKNOWN_SOURCE,
    ERROR_UNSUPPORTED_PERIOD,
    RefreshError,
    RefreshRequest,
    request_refresh,
)
from investment_agent.storage.database import session_scope

router = APIRouter(
    prefix="/research",
    tags=["research-materials"],
    dependencies=[Depends(verify_api_key_or_session)],
)

NOT_FOUND_DETAIL = "证券不存在。"
"""404 body of a listing id that matches no row."""

UNKNOWN_ERROR_STATUS = 422
"""Status of a refusal code the router does not know (never a silent 2xx)."""

_REFRESH_ERROR_STATUS: dict[str, int] = {
    ERROR_NOT_FOUND: 404,
    ERROR_LISTING_NOT_COVERED: 422,
    ERROR_UNKNOWN_SOURCE: 422,
    ERROR_INVALID_PERIOD: 422,
    ERROR_INVALID_REASON: 422,
    ERROR_UNSUPPORTED_PERIOD: 422,
}
"""Refusal code -> HTTP status; every other code falls back to 422."""


@router.get(
    "/listings/{listing_id}/financial-context",
    response_model=FinancialContextResponse,
)
async def financial_context_endpoint(
    listing_id: Annotated[int, Path(ge=1)],
) -> FinancialContextResponse:
    """The listing's saved financial material, statuses and capability bits."""

    async with session_scope() as session:
        try:
            view = await financial_context(session, listing_id)
        except ListingNotFoundError as exc:
            raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL) from exc
    return _context_response(view)


@router.post(
    "/listings/{listing_id}/refresh",
    status_code=202,
    response_model=RefreshResponse,
)
async def refresh_endpoint(
    listing_id: Annotated[int, Path(ge=1)],
    body: RefreshBody,
) -> RefreshResponse:
    """Register one deduplicated audit run for the listing.

    Every structural problem is refused before a run is written: unknown listing
    (404), listing outside the checked identity list, unknown source, malformed
    period end or reason (422). An identical repeat returns the existing run
    with ``created=False``.
    """

    request = RefreshRequest(
        listing_id=listing_id,
        sources=tuple(body.sources or ()),
        periods=tuple(body.periods or ()),
        reason=DEFAULT_REASON if body.reason is None else body.reason,
    )
    async with session_scope() as session:
        try:
            outcome = await request_refresh(session, request)
        except RefreshError as exc:
            status = _REFRESH_ERROR_STATUS.get(exc.code, UNKNOWN_ERROR_STATUS)
            raise HTTPException(
                status_code=status, detail={"code": exc.code, "message": str(exc)}
            ) from exc
    return RefreshResponse(
        run_id=outcome.run_id,
        created=outcome.created,
        idempotency_key=outcome.idempotency_key,
        adapter_versions=outcome.adapter_versions,
        online_refresh_enabled=outcome.online_refresh_enabled,
        source_decisions=outcome.source_decisions,
    )


def _context_response(view: FinancialContextView) -> FinancialContextResponse:
    """Explicit wire mapping of the read model (no dataclass is serialised directly)."""

    return FinancialContextResponse(
        listing_id=view.listing_id,
        market=view.market,
        display_code=view.display_code,
        company_name=view.company_name,
        security_type=view.security_type,
        quote_currency=view.quote_currency,
        report_currency=view.report_currency,
        covered=view.covered,
        fiscal_years=tuple(
            FiscalYearOut(end=year.end, label=year.label, days=year.days, basis=year.basis)
            for year in view.fiscal_years
        ),
        facts=tuple(
            FactOut(
                metric=fact.metric,
                label=fact.label,
                period_end=fact.period_end,
                basis=fact.basis,
                amount=fact.amount,
                unit=fact.unit,
                currency=fact.currency,
                missing_reason=fact.missing_reason,
                definition=fact.definition,
                verification=fact.verification,
                conflict=fact.conflict,
                source=fact.source,
                evidence_kind=fact.evidence_kind,
                source_date=fact.source_date,
                locator=fact.locator,
            )
            for fact in view.facts
        ),
        metrics=tuple(
            MetricOut(
                key=metric.key,
                label=metric.label,
                status=metric.status,
                value=metric.value,
                unit=metric.unit,
                currency=metric.currency,
                basis=metric.basis,
                periods=metric.periods,
                notes=metric.notes,
            )
            for metric in view.metrics
        ),
        capabilities=dict(view.capabilities),
        coverage=dict(view.coverage),
        statuses=tuple(
            StatusOut(code=status.code, affected=status.affected, next_step=status.next_step)
            for status in view.statuses
        ),
        freshness=FreshnessOut(
            last_requested_at=view.freshness.last_requested_at,
            last_attempt_at=view.freshness.last_attempt_at,
            last_success_at=view.freshness.last_success_at,
            last_imported_at=view.freshness.last_imported_at,
            last_verified_on=view.freshness.last_verified_on,
            latest_period_end=view.freshness.latest_period_end,
            latestness=view.freshness.latestness,
            latestness_checked_on=view.freshness.latestness_checked_on,
            sources=view.freshness.sources or {},
        ),
        refresh=RefreshLifecycleOut(
            run_id=view.refresh.run_id,
            status=view.refresh.status,
            reason=view.refresh.reason,
            started_at=view.refresh.started_at,
            completed_at=view.refresh.completed_at,
        ),
        metric_coverage={
            key: MetricCoverageOut(
                applicable=item.applicable,
                evidence_status=item.evidence_status,
                usable_facts=item.usable_facts,
                verified_facts=item.verified_facts,
            )
            for key, item in (view.metric_coverage or {}).items()
        },
    )

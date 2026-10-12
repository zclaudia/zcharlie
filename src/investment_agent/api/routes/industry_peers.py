"""/stocks/{ticker}/industry-peer-context: read-only peer reference API (WP9).

Placement follows the existing route organization: every ``/stocks/{ticker}/...``
resource route lives behind the ``/stocks`` prefix (directory search, industry,
report year), so the two peer-context GETs form their own module with the same
prefix instead of bending the ``/research`` router's namespace - the URLs are
fixed by the technical design (§页面与接口接入), and the plan's module table
allows this file-organization decision to be made on the existing conventions.

Discipline (fixed by the design; the service enforces the data part):

* **Read-only.** Neither endpoint ever enqueues a collection run (GET 永不入队);
  only the trusted admission path (WP8) creates collect runs.
* **Identity.** Same ``local_owner`` same-origin authentication as the other
  ``/stocks`` endpoints; the service verifies the batch kind, roster membership
  and mapping target for that owner, and this layer converts its errors:
  non-canonical ticker or ``batch_run_id`` -> 422, unknown target or a batch
  that holds no result for the target -> 404. Everything else is a normal
  200 business body (8-state enum, nulls with reasons) - the design's status
  table maps onto the body, never onto an HTTP error.
* **Feature gate.** ``industry_peer_context_enabled`` (default off) returns the
  not-yet-covered business state for every stock without touching the
  database - the minimal gate until WP14 unifies enforcement across API, page
  and agent.
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.industry_peers import (
    IndustryPeerContextOut,
    PeerCohortOut,
    PeerCompaniesOut,
    PeerMetricRefOut,
)
from investment_agent.config import Settings
from investment_agent.domain.a_share import parse_a_share_ticker
from investment_agent.industry_peers.contracts import PeerContextStatus
from investment_agent.industry_peers.service import (
    SERVICE_SCHEMA_VERSION,
    PeerContextResult,
    PeerTargetLookupError,
    get_industry_peer_context,
    list_industry_peer_companies,
)
from investment_agent.storage.database import session_scope

router = APIRouter(
    prefix="/stocks",
    tags=["stocks"],
    dependencies=[Depends(verify_api_key_or_session)],
)

_NOT_ENABLED_GAP = (
    "industry peer context is disabled; the feature does not cover this stock yet"
)


@router.get("/{ticker}/industry-peer-context", response_model=IndustryPeerContextOut)
async def stock_industry_peer_context(
    ticker: str,
    settings: Annotated[Settings, Depends(get_settings)],
    batch_run_id: Annotated[str | None, Query(max_length=128)] = None,
) -> IndustryPeerContextOut:
    """One target's peer context: the current submitted batch, or one batch.

    Without ``batch_run_id`` the current result is read (the valid submitted
    mapping with the maximum generation in the scope); with it, exactly that
    batch's mapping for this target - a batch of another target is a 404.
    """

    normalized = ticker.strip().upper()
    if parse_a_share_ticker(normalized) is None:
        raise HTTPException(status_code=422, detail=f"Invalid A-share ticker: {ticker}")
    if not settings.industry_peer_context_enabled:
        return _not_supported_context()
    try:
        async with session_scope() as session:
            result = await get_industry_peer_context(
                session,
                ticker=normalized,
                batch_run_id=batch_run_id,
                owner_id="local_owner",
                data_dir=settings.data_dir,
            )
    except PeerTargetLookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:  # non-canonical batch_run_id (path traversal & co.)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _context_out(result)


@router.get(
    "/{ticker}/industry-peer-context/companies",
    response_model=PeerCompaniesOut,
)
async def stock_industry_peer_companies(
    ticker: str,
    settings: Annotated[Settings, Depends(get_settings)],
    batch_run_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> PeerCompaniesOut:
    """The batch's fixed-version peer detail: values, exclusions, evidence.

    ``batch_run_id`` is required - the detail always shows the batch a result
    came from, never a recomputation or a mix of "latest" records.
    """

    normalized = ticker.strip().upper()
    if parse_a_share_ticker(normalized) is None:
        raise HTTPException(status_code=422, detail=f"Invalid A-share ticker: {ticker}")
    if not settings.industry_peer_context_enabled:
        return _not_supported_companies()
    try:
        async with session_scope() as session:
            result = await list_industry_peer_companies(
                session,
                ticker=normalized,
                batch_run_id=batch_run_id,
                owner_id="local_owner",
                data_dir=settings.data_dir,
            )
    except PeerTargetLookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PeerCompaniesOut(**result.model_dump())


def _not_supported_context() -> IndustryPeerContextOut:
    """The feature-disabled body: the not-yet-covered business state (200)."""

    return IndustryPeerContextOut(
        status=PeerContextStatus.NOT_SUPPORTED,
        data_as_of=datetime.now(UTC),
        gaps=(_NOT_ENABLED_GAP,),
    )


def _not_supported_companies() -> PeerCompaniesOut:
    return PeerCompaniesOut(
        status=PeerContextStatus.NOT_SUPPORTED,
        data_as_of=datetime.now(UTC),
        gaps=(_NOT_ENABLED_GAP,),
    )


def _context_out(result: PeerContextResult) -> IndustryPeerContextOut:
    """The service read, converted 1:1 into the HTTP body.

    ``metric_calculation_refs`` re-states the batch mapping's frozen entries
    (one per metric, ``saved``/``reused`` with the reference, ``gap`` with its
    reason); the per-metric blocks carry the values, counts, check coverage,
    exclusions and the citable calculation/result references.
    """

    return IndustryPeerContextOut(
        schema_version=result.schema_version,
        status=result.status,
        target=result.target,
        report_period=result.report_period,
        current_report_period=result.current_report_period,
        old_reference=result.old_reference,
        cohort=PeerCohortOut(ref=result.cohort_ref, content_hash=result.cohort_content_hash),
        batch_run_id=result.batch_run_id,
        generation=result.generation,
        provider=result.provider,
        adapter_version=result.adapter_version,
        batch_status=result.batch_status,
        metric_calculation_refs=[
            PeerMetricRefOut(
                metric_key=metric.metric_key,
                state=metric.state,
                ref=metric.calculation_ref,
                gap_reason=metric.gap_reason,
            )
            for metric in result.metrics
        ],
        data_as_of=result.data_as_of,
        inputs_acquired_earliest=result.inputs_acquired_earliest,
        inputs_acquired_latest=result.inputs_acquired_latest,
        last_checked_at=result.last_checked_at,
        submitted_at=result.submitted_at,
        metrics=result.metrics,
        warnings=result.warnings,
        gaps=result.gaps,
        stale_reasons=result.stale_reasons,
        job_hint=result.job_hint,
    )


__all__ = ["router", "SERVICE_SCHEMA_VERSION"]

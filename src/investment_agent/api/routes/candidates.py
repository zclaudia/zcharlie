"""Candidate, rule and screen-run endpoints (P1-D3).

Auth: ``X-API-Key`` or the same-origin web session (state-changing cookie
requests need the CSRF proof of :func:`verify_api_key_or_session`); wrong
keys are throttled like every protected route.

Every state change goes through :mod:`investment_agent.candidates` and
writes its decision in the same transaction.

The converters (``TemplateCache`` and the ``*_out`` helpers) now live in
:mod:`investment_agent.api.views`, shared with the MCP candidate tools; this
module imports them from there and re-exports the public ones.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.candidates import (
    CandidateDetailOut,
    CandidateNominateOut,
    CandidateNominateRequest,
    CandidatePageOut,
    PromoteOut,
    PromoteRequest,
    ReasonRequest,
    RedraftOut,
    RedraftRequest,
    RuleDecisionOut,
    RuleDetailOut,
    RulePatchRequest,
    ScreenHitOut,
    ScreenRunOut,
    ScreenTemplateOut,
    WatchlistEntryOut,
)
from investment_agent.api.views import (
    TemplateCache,
    _decision_out,
    _reasons,
    _rule_summary,
    candidate_detail_out,
    candidate_summary_out,
    rule_detail_out,
)
from investment_agent.candidates.lifecycle import (
    CandidateError,
    CandidatePolicy,
    dismiss_candidate,
    nominate_candidate,
    open_candidate,
    redraft_candidate,
)
from investment_agent.candidates.promote import (
    edit_rule_params,
    reject_rule,
    run_promotion,
)
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.domain.time import (
    as_utc as _as_utc,
)
from investment_agent.domain.time import (
    optional_utc as _optional_utc,
)
from investment_agent.rules.screen_job import SCREEN_RUN, screen_results
from investment_agent.rules.validation import RuleValidationError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.rules import (
    RuleInstanceRecord,
    ScreenResultRecord,
)
from investment_agent.storage.repositories.candidate import (
    CandidateRepository,
    ReasonRequiredError,
)

_auth = [Depends(verify_api_key_or_session)]
candidates_router = APIRouter(prefix="/candidates", tags=["candidates"], dependencies=_auth)
rules_router = APIRouter(prefix="/rules", tags=["rules"], dependencies=_auth)
screen_runs_router = APIRouter(prefix="/screen-runs", tags=["screen-runs"], dependencies=_auth)


def _raise_http(exc: Exception) -> NoReturn:
    """Candidate-domain errors -> HTTP.  Must be called from an ``except`` block:
    the final bare ``raise`` re-raises the active exception untouched, while
    ``raise exc`` would splice this helper's frame into its traceback."""

    if isinstance(exc, RuleValidationError):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "rule validation failed",
                "issues": [
                    {"code": issue.code, "path": issue.path, "message": issue.message}
                    for issue in exc.issues
                ],
            },
        ) from exc
    if isinstance(exc, ReasonRequiredError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, CandidateError):
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    raise


async def _required_candidate(session: AsyncSession, candidate_id: int) -> CandidateRecord:
    candidate = await CandidateRepository(session).get(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"Candidate {candidate_id} not found")
    return candidate


# --------------------------------------------------------------------------- #
# /candidates
# --------------------------------------------------------------------------- #


@candidates_router.get("", response_model=CandidatePageOut)
async def list_candidates(
    candidate_status: Annotated[
        str | None,
        Query(alias="status", pattern=r"^(open|promoted|dismissed|expired)$"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> CandidatePageOut:
    async with session_scope() as session:
        rows, total = await CandidateRepository(session).page(
            status=candidate_status, limit=limit, offset=offset
        )
        templates = TemplateCache(session)
        items = [await candidate_summary_out(session, row, templates) for row in rows]
    return CandidatePageOut(items=items, total=total, limit=limit, offset=offset)


@candidates_router.get("/{candidate_id}", response_model=CandidateDetailOut)
async def get_candidate(candidate_id: int) -> CandidateDetailOut:
    async with session_scope() as session:
        candidate = await _required_candidate(session, candidate_id)
        return await candidate_detail_out(session, candidate)


@candidates_router.post("", response_model=CandidateNominateOut, status_code=201)
async def nominate(
    body: CandidateNominateRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> CandidateNominateOut:
    try:
        async with session_scope() as session:
            outcome = await nominate_candidate(
                session,
                ticker=body.ticker,
                template_id=body.template_id,
                note=body.note,
                policy=CandidatePolicy.from_settings(settings),
                as_of=shanghai_today(),
            )
            detail = await candidate_detail_out(session, outcome.candidate)
    except (CandidateError, ReasonRequiredError) as exc:
        _raise_http(exc)
    return CandidateNominateOut(
        candidate=detail, created=outcome.created, draft_run_id=outcome.draft_run_id
    )


@candidates_router.post("/{candidate_id}/dismiss", response_model=CandidateDetailOut)
async def dismiss(candidate_id: int, body: ReasonRequest) -> CandidateDetailOut:
    try:
        async with session_scope() as session:
            candidate = await dismiss_candidate(session, candidate_id, reason=body.reason)
            return await candidate_detail_out(session, candidate)
    except (CandidateError, ReasonRequiredError) as exc:
        _raise_http(exc)


@candidates_router.post("/{candidate_id}/redraft", response_model=RedraftOut, status_code=202)
async def redraft(candidate_id: int, body: RedraftRequest) -> RedraftOut:
    """Queue a fresh LLM rule draft for an open candidate."""

    try:
        async with session_scope() as session:
            run = await redraft_candidate(session, candidate_id, template_id=body.template_id)
            candidate = await candidate_detail_out(session, await open_candidate(session, candidate_id))
            return RedraftOut(candidate=candidate, draft_run_id=run.id)
    except CandidateError as exc:
        _raise_http(exc)


@candidates_router.post("/{candidate_id}/promote", response_model=PromoteOut)
async def promote(
    candidate_id: int,
    body: PromoteRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> PromoteOut:
    try:
        outcome = await run_promotion(
            candidate_id,
            rule_instance_id=body.rule_instance_id,
            params_override=body.params_override,
            reason=body.reason,
            settings=settings,
        )
    except (CandidateError, RuleValidationError, ReasonRequiredError) as exc:
        _raise_http(exc)
    async with session_scope() as session:
        templates = TemplateCache(session)
        candidate = await _required_candidate(session, outcome.candidate.id)
        rule = await session.get(RuleInstanceRecord, outcome.rule.id)
        assert rule is not None
        item = outcome.watchlist_item
        return PromoteOut(
            candidate=await candidate_summary_out(session, candidate, templates),
            rule=await _rule_summary(rule, templates),
            retired_rule_instance_id=outcome.previous_active_id,
            watchlist=WatchlistEntryOut(
                listing_id=item.listing_id,
                ticker=candidate.ticker,
                industry=item.industry,
                industry_source=outcome.metadata.industry_source,
                report_year=item.report_year,
                report_year_source=outcome.metadata.report_year_source,
                enabled=item.enabled,
            ),
            decisions=[_decision_out(item) for item in outcome.decisions],
            verification_requested=outcome.verification_requested,
            research_run_id=outcome.research_run_id,
            warnings=outcome.warnings,
        )


# --------------------------------------------------------------------------- #
# /rules
# --------------------------------------------------------------------------- #


@rules_router.get("/{rule_id}", response_model=RuleDetailOut)
async def get_rule(rule_id: int) -> RuleDetailOut:
    async with session_scope() as session:
        rule = await session.get(RuleInstanceRecord, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"Rule {rule_id} not found")
        return await rule_detail_out(rule, TemplateCache(session))


@rules_router.patch("/{rule_id}", response_model=RuleDecisionOut)
async def patch_rule(rule_id: int, body: RulePatchRequest) -> RuleDecisionOut:
    try:
        async with session_scope() as session:
            rule, decision = await edit_rule_params(
                session, rule_id, dict(body.params), reason=body.reason
            )
            return RuleDecisionOut(
                rule=await rule_detail_out(rule, TemplateCache(session)),
                decision=_decision_out(decision),
            )
    except (CandidateError, RuleValidationError) as exc:
        _raise_http(exc)


@rules_router.post("/{rule_id}/reject", response_model=RuleDecisionOut)
async def reject(rule_id: int, body: ReasonRequest) -> RuleDecisionOut:
    try:
        async with session_scope() as session:
            rule, decision = await reject_rule(session, rule_id, reason=body.reason)
            return RuleDecisionOut(
                rule=await rule_detail_out(rule, TemplateCache(session)),
                decision=_decision_out(decision),
            )
    except (CandidateError, ReasonRequiredError) as exc:
        _raise_http(exc)


# --------------------------------------------------------------------------- #
# /screen-runs
# --------------------------------------------------------------------------- #


@screen_runs_router.get("/{run_id}", response_model=ScreenRunOut)
async def get_screen_run(run_id: int) -> ScreenRunOut:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        if run is None or run.kind != SCREEN_RUN:
            raise HTTPException(status_code=404, detail=f"Screen run {run_id} not found")
        rows = await screen_results(session, run_id)
        templates = TemplateCache(session)
        listing_ids = sorted({row.listing_id for row in rows if row.outcome == "hit"})
        open_candidates: dict[int, int] = {}
        if listing_ids:
            stmt = select(CandidateRecord.listing_id, CandidateRecord.id).where(
                CandidateRecord.listing_id.in_(listing_ids), CandidateRecord.status == "open"
            )
            open_candidates = {
                listing_id: candidate_id
                for listing_id, candidate_id in (await session.execute(stmt)).all()
            }
        grouped: dict[tuple[str, int], list[ScreenResultRecord]] = {}
        for row in rows:
            grouped.setdefault((row.template_id, row.template_version), []).append(row)
        template_out: list[ScreenTemplateOut] = []
        for (template_id, version), members in sorted(grouped.items()):
            counts: dict[str, int] = {}
            for member in members:
                counts[member.outcome] = counts.get(member.outcome, 0) + 1
            hits = sorted(
                (member for member in members if member.outcome == "hit"),
                key=lambda item: (item.rank if item.rank is not None else 1 << 30, item.ticker),
            )
            template_out.append(
                ScreenTemplateOut(
                    template_ref=f"{template_id}@{version}",
                    template_id=template_id,
                    template_version=version,
                    calibration_status=await templates.calibration(template_id, version),
                    counts=counts,
                    hits=[
                        ScreenHitOut(
                            screen_result_id=hit.id,
                            listing_id=hit.listing_id,
                            ticker=hit.ticker,
                            rank=hit.rank,
                            reasons=_reasons(hit.reasons),
                            candidate_id=open_candidates.get(hit.listing_id),
                        )
                        for hit in hits
                    ],
                )
            )
        metadata = run.metadata_json or {}
        request = run.request_json or {}
        raw_as_of = request.get("as_of")
        member_count = metadata.get("member_count")
        candidates_meta = metadata.get("candidates")
        return ScreenRunOut(
            run_id=run.id,
            status=run.status,
            stage=run.stage,
            universe_code=run.universe_code,
            as_of=date.fromisoformat(raw_as_of) if isinstance(raw_as_of, str) else None,
            started_at=_as_utc(run.started_at),
            finished_at=_optional_utc(run.finished_at),
            error_summary=run.error_summary,
            member_count=member_count if isinstance(member_count, int) else None,
            templates=template_out,
            candidates=candidates_meta if isinstance(candidates_meta, dict) else None,
        )


routers = (candidates_router, rules_router, screen_runs_router)

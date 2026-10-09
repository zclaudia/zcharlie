"""Candidate, rule and screen-run endpoints (P1-D3).

Auth: ``X-API-Key`` or the same-origin web session (state-changing cookie
requests need the CSRF proof of :func:`verify_api_key_or_session`); wrong
keys are throttled like every protected route.

Every state change goes through :mod:`investment_agent.candidates` and
writes its decision in the same transaction.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.candidates import (
    CandidateDetailOut,
    CandidateNominateOut,
    CandidateNominateRequest,
    CandidatePageOut,
    CandidateSourceOut,
    CandidateSummaryOut,
    DecisionOut,
    ParamRangeOut,
    PromoteOut,
    PromoteRequest,
    ReasonRequest,
    RedraftOut,
    RedraftRequest,
    RuleDecisionOut,
    RuleDetailOut,
    RulePatchRequest,
    RuleSummaryOut,
    ScreenHitOut,
    ScreenReasonOut,
    ScreenResultOut,
    ScreenRunOut,
    ScreenTemplateOut,
    SegmentOut,
    WatchlistEntryOut,
)
from investment_agent.candidates.lifecycle import (
    CandidateError,
    CandidatePolicy,
    context_with_industry_name,
    dismiss_candidate,
    effective_draft,
    latest_screen_results,
    nominate_candidate,
    open_candidate,
    redraft_candidate,
    rule_params,
)
from investment_agent.candidates.promote import (
    edit_rule_params,
    reject_rule,
    run_promotion,
)
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.rules.screen_job import SCREEN_RUN, screen_results
from investment_agent.rules.store import template_from_record
from investment_agent.rules.validation import RuleValidationError, effective_params
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.rules import (
    RuleInstanceRecord,
    RuleTemplateRecord,
    ScreenResultRecord,
)
from investment_agent.storage.repositories.candidate import (
    CandidateRepository,
    DecisionRepository,
    ReasonRequiredError,
)

_auth = [Depends(verify_api_key_or_session)]
candidates_router = APIRouter(prefix="/candidates", tags=["candidates"], dependencies=_auth)
rules_router = APIRouter(prefix="/rules", tags=["rules"], dependencies=_auth)
screen_runs_router = APIRouter(prefix="/screen-runs", tags=["screen-runs"], dependencies=_auth)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


def _raise_http(exc: Exception) -> NoReturn:
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
    raise exc


# --------------------------------------------------------------------------- #
# Converters
# --------------------------------------------------------------------------- #


class TemplateCache:
    """Per-request cache of template records (also used by the MCP tools)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._cache: dict[tuple[str, int], RuleTemplateRecord | None] = {}

    async def get(self, template_id: str, version: int) -> RuleTemplateRecord | None:
        key = (template_id, version)
        if key not in self._cache:
            self._cache[key] = await self.session.get(RuleTemplateRecord, key)
        return self._cache[key]

    async def calibration(self, template_id: str | None, version: int | None) -> str | None:
        if template_id is None or version is None:
            return None
        record = await self.get(template_id, version)
        return record.calibration_status if record is not None else None


def _factor_unit(name: object) -> str | None:
    if isinstance(name, str) and name in DEFAULT_REGISTRY:
        return DEFAULT_REGISTRY.get(name).unit
    return None


def _reasons(raw: list[dict[str, Any]] | None) -> list[ScreenReasonOut]:
    reasons: list[ScreenReasonOut] = []
    for item in raw or []:
        reason = ScreenReasonOut.model_validate(item)
        if reason.unit is None:
            reason.unit = _factor_unit(reason.factor)
        reasons.append(reason)
    return reasons


def _zone_factors(node: Any) -> list[str]:
    if isinstance(node, dict):
        if isinstance(node.get("factor"), str):
            return [node["factor"]]
        found: list[str] = []
        for key in ("all", "any"):
            for child in node.get(key) or []:
                found += _zone_factors(child)
        return found
    return []


async def _screen_result_out(row: ScreenResultRecord, templates: TemplateCache) -> ScreenResultOut:
    return ScreenResultOut(
        screen_result_id=row.id,
        screen_run_id=row.screen_run_id,
        as_of=row.as_of,
        template_id=row.template_id,
        template_version=row.template_version,
        template_ref=f"{row.template_id}@{row.template_version}",
        calibration_status=await templates.calibration(row.template_id, row.template_version),
        outcome=row.outcome,
        rank=row.rank,
        reasons=_reasons(row.reasons),
        context=context_with_industry_name(row.context),
    )


async def _rule_summary(rule: RuleInstanceRecord, templates: TemplateCache) -> RuleSummaryOut:
    return RuleSummaryOut(
        rule_instance_id=rule.id,
        listing_id=rule.listing_id,
        ticker=rule.symbol,
        scope=rule.scope,
        template_id=rule.template_id,
        template_version=rule.template_version,
        template_ref=f"{rule.template_id}@{rule.template_version}",
        calibration_status=await templates.calibration(rule.template_id, rule.template_version),
        version=rule.version,
        status=rule.status,
        params=rule_params(rule),
        drafted_by=rule.drafted_by,
    )


async def rule_detail_out(rule: RuleInstanceRecord, templates: TemplateCache) -> RuleDetailOut:
    summary = await _rule_summary(rule, templates)
    record = await templates.get(rule.template_id, rule.template_version)
    template_params: dict[str, float] = {}
    ranges: dict[str, ParamRangeOut] = {}
    effective: dict[str, float] = dict(summary.params)
    definition: dict[str, Any] = dict(rule.definition or {})
    raw_zones = definition.get("zones")
    zones: dict[str, Any] = dict(raw_zones) if isinstance(raw_zones, dict) else {}
    if record is not None:
        template = template_from_record(record)
        if not zones:
            zones = {
                name: group.model_dump(mode="json", by_alias=True, exclude_none=True)
                for name, group in template.zones_for(rule.scope).items()
            }
        template_params = dict(template.params)
        ranges = {
            name: ParamRangeOut(min=item.min, max=item.max)
            for name, item in template.param_ranges.items()
        }
        effective = effective_params(template) | summary.params
    return RuleDetailOut(
        **summary.model_dump(),
        definition=definition,
        draft_rationale=rule.draft_rationale,
        approved_by=rule.approved_by,
        approved_at=_optional_utc(rule.approved_at),
        retired_at=_optional_utc(rule.retired_at),
        created_at=_as_utc(rule.created_at),
        template_params=template_params,
        param_ranges=ranges,
        effective_params=effective,
        zones=zones,
        factor_units={
            name: unit
            for name in dict.fromkeys(_zone_factors({"all": list(zones.values())}))
            if (unit := _factor_unit(name)) is not None
        },
    )


def _decision_out(decision: DecisionRecord) -> DecisionOut:
    return DecisionOut(
        id=decision.id,
        kind=decision.kind,
        actor=decision.actor,
        ticker=decision.ticker,
        reason_text=decision.reason_text,
        rule_instance_id=decision.rule_instance_id,
        rule_ref=decision.rule_ref,
        candidate_id=decision.candidate_id,
        at=_as_utc(decision.at),
        context=dict(decision.context or {}),
    )


async def _company_name(session: AsyncSession, listing_id: int) -> str | None:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        return None
    company = await session.get(Company, listing.company_id)
    return company.canonical_name if company is not None else None


async def _summary_parts(
    session: AsyncSession, candidate: CandidateRecord, templates: TemplateCache
) -> tuple[dict[str, Any], list[ScreenResultRecord], RuleInstanceRecord | None]:
    results = await latest_screen_results(session, candidate)
    draft_state, draft = await effective_draft(session, candidate)
    sources = [CandidateSourceOut.model_validate(item) for item in candidate.sources or []]
    refs = list(
        dict.fromkeys(
            f"{item.template_id}@{item.template_version}"
            for item in sources
            if item.template_id is not None and item.template_version is not None
        )
    )
    newest = next(
        (item for item in reversed(sources) if item.template_id and item.template_version),
        None,
    )
    fields: dict[str, Any] = {
        "id": candidate.id,
        "listing_id": candidate.listing_id,
        "ticker": candidate.ticker,
        "name": await _company_name(session, candidate.listing_id),
        "status": candidate.status,
        "opened_at": _as_utc(candidate.opened_at),
        "expires_at": _as_utc(candidate.expires_at),
        "closed_at": _optional_utc(candidate.closed_at),
        "sources": sources,
        "template_refs": refs,
        "calibration_status": (
            await templates.calibration(newest.template_id, newest.template_version)
            if newest is not None
            else None
        ),
        "latest_screen": await _screen_result_out(results[0], templates) if results else None,
        "draft_state": draft_state,
        "draft_rule": await _rule_summary(draft, templates) if draft is not None else None,
        "note": candidate.note,
    }
    return fields, results, draft


async def candidate_summary_out(
    session: AsyncSession, candidate: CandidateRecord, templates: TemplateCache
) -> CandidateSummaryOut:
    fields, _results, _draft = await _summary_parts(session, candidate, templates)
    return CandidateSummaryOut(**fields)


async def _segments_out(session: AsyncSession, candidate: CandidateRecord) -> list[SegmentOut]:
    """Latest 主营构成 of the listing (facts; the same loader as the drafting prompt)."""

    from investment_agent.rules.drafting import load_segments

    return [
        SegmentOut(
            report_period=item.report_period,
            kind=item.kind,
            name=item.name,
            revenue=item.revenue,
            share=item.share,
            margin=item.margin,
            profit=item.profit,
        )
        for item in await load_segments(session, candidate.ticker, shanghai_today())
    ]


async def candidate_detail_out(session: AsyncSession, candidate: CandidateRecord) -> CandidateDetailOut:
    templates = TemplateCache(session)
    fields, results, draft = await _summary_parts(session, candidate, templates)
    decisions = await DecisionRepository(session).for_candidate(candidate.id)
    return CandidateDetailOut(
        **fields,
        screen_results=[await _screen_result_out(row, templates) for row in results],
        draft_rule_detail=await rule_detail_out(draft, templates) if draft is not None else None,
        decisions=[_decision_out(item) for item in decisions],
        segments=await _segments_out(session, candidate),
    )


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

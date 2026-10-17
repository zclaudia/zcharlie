"""Authenticated explicit local checks and side-effect-free artifact reads."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.deps import session_service
from investment_agent.api.schemas.investment_assessments import (
    AssessmentFeatureFlags,
    AssessmentListing,
    FitContextResponse,
    RankingEntryResponse,
)
from investment_agent.candidates.ranking_contracts import RankingRequest
from investment_agent.candidates.ranking_jobs import (
    CandidateRankingAdmission,
    RankingView,
    read_ranking_view,
)
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.suitability import AccountFitService, AccountFitView
from investment_agent.portfolio.suitability_contracts import AccountFitRequest
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.portfolio.suitability_jobs import (
    AccountFitAdmission,
    AdmissionReceipt,
    AssessmentRunView,
    run_view,
)
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.rules import RuleTemplateRecord

router = APIRouter(tags=["investment-assessments"], dependencies=[Depends(verify_api_key_or_session)])
service = session_service(lambda session: SuitabilityInputService(session))
Service = Annotated[SuitabilityInputService, Depends(service, scope="function")]
EffectiveSettings = Annotated[Settings, Depends(get_settings)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
ExactRevision = Annotated[int, Query(ge=1)]
HashKey = Annotated[str, Query(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")]


def _reference(kind: Literal["account_fit", "candidate_ranking"], identity: UUID, revision: int) -> ObjectRef:
    if revision != 1:
        raise InvestorError("not_found", "评估版本不存在")
    return ObjectRef(kind=kind, id=identity, revision=revision)


@router.get("/portfolio/fit-context")
async def fit_context(account_id: UUID, svc: Service, settings: EffectiveSettings, listing_id: Annotated[int | None, Query(ge=1)] = None) -> FitContextResponse:
    context = await svc.context(account_id)
    listing = None
    listing_source = None
    if listing_id is not None:
        row = (await svc.session.execute(select(Listing, Company.canonical_name).join(Company, Company.id == Listing.company_id).where(Listing.id == listing_id))).first()
        if row is None:
            raise InvestorError("not_found", "标的尚未建档")
        listing = AssessmentListing(listing_ref=ObjectRef(kind="listing", id=row[0].id, revision=1), ticker=row[0].ticker, name=row[1])
        listing_source = (await svc.listing_sources((listing.listing_ref,), valuation=context.valuation, as_of=datetime.now(UTC), rule_refs=set(), holding_refs={h.listing_ref for h in context.snapshot.holdings} if context.snapshot else set()))[0]
        source_gaps = tuple(reason for reason in (listing_source.price.unknown_reason, listing_source.sector.unknown_reason) if reason is not None)
        context = context.model_copy(update={"gaps": (*context.gaps, *source_gaps)})
    templates = tuple(await svc.session.scalars(select(RuleTemplateRecord.id).distinct().order_by(RuleTemplateRecord.id)))
    return FitContextResponse(**context.model_dump(), enabled=AssessmentFeatureFlags(account_fit=settings.account_fit_enabled, candidate_ranking=settings.candidate_ranking_enabled, portfolio_ledger=settings.portfolio_ledger_enabled), listing=listing, listing_source=listing_source, source_templates=templates)


@router.post("/portfolio/fit-assessments", status_code=202)
async def create_fit(body: AccountFitRequest, svc: Service, settings: EffectiveSettings) -> AdmissionReceipt:
    return await AccountFitAdmission(svc.session, settings=settings, rules=svc.rules).accept(body)


@router.get("/portfolio/fit-assessments/current")
async def current_fit(account_id: UUID, listing_id: Annotated[int, Query(ge=1)], action: Literal["open", "add", "hold"], purpose_binding_key: HashKey, calculation_basis_key: HashKey, svc: Service, settings: EffectiveSettings) -> AccountFitView:
    return await AccountFitService(svc.session, rules=svc.rules).current(account_id, ObjectRef(kind="listing", id=listing_id, revision=1), action=action, purpose_binding_key=purpose_binding_key, calculation_basis_key=calculation_basis_key)


@router.get("/portfolio/fit-assessments/{assessment_id}")
async def exact_fit(assessment_id: UUID, account_id: UUID, svc: Service, settings: EffectiveSettings, revision: ExactRevision = 1) -> AccountFitView:
    return await AccountFitService(svc.session, rules=svc.rules).get(account_id, _reference("account_fit", assessment_id, revision))


@router.post("/candidates/rankings", status_code=202)
async def create_ranking(body: RankingRequest, svc: Service, settings: EffectiveSettings) -> AdmissionReceipt:
    return await CandidateRankingAdmission(svc.session, settings=settings, rules=svc.rules).accept(body)


@router.get("/candidates/rankings/{ranking_id}")
async def exact_ranking(ranking_id: UUID, account_id: UUID, svc: Service, settings: EffectiveSettings, revision: ExactRevision = 1, cursor: str | None = None, limit: PageLimit = 100, bucket: Literal["incomplete", "blocked", "ready", "limited"] | None = None, financial_status: Literal["pass", "warning", "blocked", "unknown"] | None = None) -> RankingView:
    return await read_ranking_view(svc.session, account_id=account_id, ranking_ref=_reference("candidate_ranking", ranking_id, revision), limit=limit, bucket=bucket, financial_status=financial_status, cursor=cursor, settings=settings, rules=svc.rules)


@router.get("/candidates/rankings/{ranking_id}/entries/{listing_id}")
async def exact_entry(ranking_id: UUID, listing_id: int, account_id: UUID, svc: Service, settings: EffectiveSettings, revision: ExactRevision = 1) -> RankingEntryResponse:
    reference = _reference("candidate_ranking", ranking_id, revision)
    page = await read_ranking_view(svc.session, account_id=account_id, ranking_ref=reference, limit=100, bucket=None, financial_status=None, cursor=None, settings=settings, rules=svc.rules)
    # Exact entries are selected from the complete immutable set, never from
    # the current candidate page or a newly generated fit.
    from investment_agent.storage.repositories.investment_assessment import (
        InvestmentAssessmentRepository,
    )

    stored = await InvestmentAssessmentRepository(svc.session).read_ranking("local_owner", reference)
    assert stored is not None
    entry = next((entry for entry in stored.entries if entry.listing_ref.id == listing_id), None)
    if entry is None:
        raise InvestorError("not_found", "排序中没有该标的")
    member = next(member for member in stored.snapshot.source_manifest.members if member.listing_ref.id == listing_id)
    return RankingEntryResponse(entry=entry, sources=member.sources, current_limits=page.current_limits)


@router.get("/investment-assessment-runs/{run_id}")
async def assessment_run(run_id: Annotated[int, Path(ge=1)], svc: Service) -> AssessmentRunView:
    return await run_view(svc.session, run_id)

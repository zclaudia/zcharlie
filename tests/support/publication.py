"""Publication fixtures and constructors (moved from tests/test_investor_publication.py)."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionGrant, admit
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord, Period
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Mutation, ObjectRef, Quantity
from investment_agent.investor.contracts import (
    DIMENSIONS,
    Claim,
    CompanyRecommendation,
    DimensionAssessment,
    NumericNode,
    ReviewEvent,
    TextNode,
    ThesisRevision,
)
from investment_agent.investor.foundations import CreateCase, InvestorService
from investment_agent.investor.publication import (
    FIXED_METHOD,
    PublicationCandidate,
    PublicationService,
    SemanticAssessment,
    SemanticReview,
    candidate_hash,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from investment_agent.valuation.contracts import PercentileRequest
from investment_agent.valuation.service import ValuationService


@pytest_asyncio.fixture
async def publication_db(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


def metadata():
    return dict(id=uuid4(), revision=1, actor="untrusted", origin_ref=None, created_at=datetime.now(UTC))


def base():
    return Settings(_env_file=None, api_key="ci-inert-api-key", investor_agent_enabled=True, investor_agent_task_seconds=600)


async def fixture():
    async with session_scope() as session:
        session.add(Market(code="SH", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="Fictional company")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SH", ticker="600000.SH", is_primary=True)
        session.add(listing)
        await session.flush()
        subject = ObjectRef(kind="listing", id=listing.id, revision=1)
        svc = InvestorService(session)
        from investment_agent.investor.contracts import Mandate, ResearchAuthorization
        unknown = Quantity(value=None, unit="USD", unknown_reason="Not specified")
        mandate = await svc.create_mandate("local_owner", Mandate(**metadata(), goals=(), scope_refs=(subject,), hard_constraints=(), unknowns=(), authorization=ResearchAuthorization(daily_budget=unknown, per_task_budget=unknown, max_leads_per_batch=0, max_new_tasks_per_batch=0, source_refs=())), uuid4())
        case = await svc.create_case("local_owner", CreateCase(listing_ref=subject, question="Review company", idempotency_key=uuid4()))
        as_of = datetime.now(UTC) - timedelta(days=1)
        record = EvidenceRecord(**metadata(), source_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1), source_kind="document", locator="fictional.fixture", content_hash="fictional", subject_ref=subject,
            period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31), basis="annual"), published_at=as_of - timedelta(days=1), acquired_at=as_of - timedelta(days=1), available_from=as_of - timedelta(days=1), verification="verified", conflict="none", value=Quantity(value="12.345", unit="亿元", currency="CNY"))
        record = record.model_copy(update={"created_at": as_of - timedelta(days=1)})
        session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
        ref = ObjectRef(kind="evidence", id=record.id, revision=1)
        claim = Claim(id=uuid4(), kind="fact", text=(TextNode(type="text", text="Saved cash value "), NumericNode(type="numeric_claim", ref=ref.model_copy(update={"field_path": "value"}), role="historical_fact", display_format="decimal")), materiality="critical", support_refs=(ref,), opposition_refs=(), assumption_refs=(), verification="verified", impact_if_wrong="The cash assessment must be reconsidered")
        dimensions = tuple(DimensionAssessment(dimension=name, status="missing", applicable=True, reason="Report detail remains unavailable", refs=()) for name in DIMENSIONS)
        thesis = ThesisRevision(**metadata(), case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision), previous_ref=None, change_reason="Initial review", question_scope="Whole company with explicit gaps", assessed_dimensions=dimensions, claims=(claim,), assumptions=(), counter_case=(TextNode(type="text", text="Cash may reflect temporary changes"),), evidence_set_hash="fixture-material", method_ref=FIXED_METHOD, as_of=as_of)
        from investment_agent.investor.contracts import Dependency
        recommendation = CompanyRecommendation(**metadata(), scope="company_view", case_ids=(case.id,), thesis_revision_refs=(ObjectRef(kind="thesis", id=thesis.id, revision=1),), as_of=as_of, question_scope=thesis.question_scope, assessed_dimensions=dimensions, sufficiency="bounded", claims=(claim,), counter_case=thesis.counter_case, assumptions=(), gaps=("Durability not established",), conditions=(), valid_until=as_of + timedelta(days=14), next_review_at=as_of + timedelta(days=7), monitor_refs=(), dependency_refs=(Dependency(ref=ref, materiality="critical", fields=("value",)),), mandate_revision_id=ObjectRef(kind="mandate", id=mandate.id, revision=1), method_revision_id=FIXED_METHOD, provenance=(ref,), business_view="undetermined", price_view="undetermined", conditional_actions=())
        candidate = PublicationCandidate(thesis=thesis, recommendation=recommendation)
    return candidate, ref, subject


def semantic(candidate):
    claims = {c.id: c for c in (*candidate.thesis.claims, *candidate.recommendation.claims)}
    return SemanticReview(**metadata(), candidate_hash=candidate_hash(candidate), material_set_hash=candidate.thesis.evidence_set_hash, method_ref=FIXED_METHOD,
        assessments=tuple(SemanticAssessment(claim_id=identifier, fact_relation="The saved historical cash supports a limited observation", counter_evidence_relevance="Temporary cash changes do not establish durable economics", materiality_reason="Cash quality could change the limited observation") for identifier in claims), checked_sources=tuple({r for c in claims.values() for r in (*c.support_refs, *c.opposition_refs, *c.assumption_refs)}), unresolved_questions=(), conclusion="pass", reviewer="trusted_research_step")


async def prepared(candidate, review=None, *, permission=True):
    review = review or semantic(candidate)
    async with session_scope() as session:
        grant = ExecutionGrant(mode="interactive", source="authenticated_message", prompt="Research the company", tool_names=("publish_recommendation", "extend_recommendation") if permission else (), scope_refs=(candidate.thesis.case_ref,), mandate_id=candidate.recommendation.mandate_revision_id.id if candidate.recommendation.mandate_revision_id else None)
        task = await admit(session, base=base(), input_key=str(uuid4()), binding=ContextBinding(context_refs=(candidate.thesis.case_ref,), base_refs=(candidate.thesis.case_ref,), as_of=datetime.now(UTC)), grant=grant)
        run = await PipelineRunRepository(session).claim_next(worker_id=str(task.id), kinds=["agent_turn"])
        claim = await TaskService(session).claim(task.id, RunLease(run.id, str(task.id), run.attempt_count))
        action = await TaskService(session).prepare_action(claim, intent_key=f"publish:{candidate.thesis.case_ref.id}", parameters={"candidate": candidate.model_dump(mode="json"), "semantic": review.model_dump(mode="json")})
    return claim, UUID(action.operation_id), review


async def publish(candidate, prepared_action=None):
    claim, operation, review = prepared_action or await prepared(candidate)
    async with session_scope() as session:
        return await PublicationService(session).publish(claim, operation, candidate, review, base=base())


def unchanged_review(ref, checked):
    now = datetime.now(UTC)
    return ReviewEvent(**metadata(), trigger_refs=(), recommendation_ref=ref, evidence_diff_refs=checked, result="maintain", reasoning_revision=False, new_recommendation_ref=None, rationale="All relevant premises remain unchanged", due_at=now, started_at=now, completed_at=now)


async def market_candidate():
    """Real AG05/AG10 source adapters, with a finite candidate company view.

    Anchored on the run time: the market premise must stay inside the 14-day
    snapshot freshness and validity windows on any date. A fixed cutoff drifts
    out of them, and then publish(candidate) returns published=False and every
    scenario built here loses its published-first precondition.
    """
    from investment_agent.investor.contracts import Dependency
    from tests.support.valuation import seed_bars
    now = datetime.now(UTC)
    # The newest premise close settles one session behind the cutoff, which is
    # recent enough to publish and old enough for a same-value capture after it.
    anchor = now - timedelta(days=1)
    candidate, _, _ = await fixture()
    async with session_scope() as session:
        listing, bars = await seed_bars(session, anchor=anchor)
        output = await ValuationService(session).percentile("local_owner", PercentileRequest(ticker="600519.SH", as_of=anchor, metric="pe"), uuid4())
        calculation = CalculationRecord.model_validate(output["calculation"])
        evidence_refs = [ref for ref in calculation.fact_refs if (await EvidenceService(session).get_evidence("local_owner", ref, anchor)).data.source_ref.field_path == "close"]
        evidence_ref = evidence_refs[0]
        evidence = (await EvidenceService(session).get_evidence("local_owner", evidence_ref, anchor)).data
        foundation = InvestorService(session)
        subject = ObjectRef(kind="listing", id=listing.id, revision=1)
        mandate = await foundation.get_mandate("local_owner")
        mandate = await foundation.revise_mandate("local_owner", mandate.model_copy(update={"revision": 2, "scope_refs": (*mandate.scope_refs, subject)}), Mutation(expected_revision=1, idempotency_key=uuid4()))
        case = await foundation.create_case("local_owner", CreateCase(listing_ref=subject, question="Review company", idempotency_key=uuid4()))
        claim = candidate.recommendation.claims[0].model_copy(update={"materiality": "context", "verification": "pending", "support_refs": (evidence_ref,), "text": (TextNode(type="text", text="Saved daily price "), NumericNode(type="numeric_claim", ref=evidence_ref.model_copy(update={"field_path": "value"}), role="historical_fact", display_format="decimal"))})
        thesis = candidate.thesis.model_copy(update={"case_ref": ObjectRef(kind="case", id=case.id, revision=1), "as_of": anchor, "claims": (claim,)})
        rec = candidate.recommendation.model_copy(update={"case_ids": (case.id,), "as_of": anchor, "valid_until": anchor + timedelta(days=14), "next_review_at": anchor + timedelta(days=7), "claims": (claim,), "dependency_refs": (Dependency(ref=evidence_ref, materiality="critical", fields=("value",)),), "provenance": (evidence_ref,), "mandate_revision_id": ObjectRef(kind="mandate", id=mandate.id, revision=2)})
        return candidate.model_copy(update={"thesis": thesis, "recommendation": rec}), evidence_ref, evidence, bars[-1]

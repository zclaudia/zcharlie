"""AF-P3-05 integration: real admission, real evaluation worker, real prepare."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from investment_agent.agent.execution import ExecutionDeniedError
from investment_agent.agent.research_publication import (
    PublicationCandidate,
    SemanticInput,
    reviewed_candidate,
)
from investment_agent.config import Settings
from investment_agent.evidence.contracts import EvidenceRecord, Period
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.jobs.base import ClaimedJob
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from investment_agent.supplemental_rules.admission import SUPPLEMENTAL_RULE_EVALUATE
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import RuleCoverageManifest
from investment_agent.supplemental_rules.jobs import SupplementalRuleHandler
from tests.support.publication import metadata
from tests.support.research_closure import (
    activate_rule,
    admit_research,
    business_input,
    claim_task,
    commit_progress,
    metric_rule,
    run_prepare,
    seed_company_view,
    seed_listing,
    set_runtime,
)


class _InlineContext:
    worker_id = "p3-coverage"

    async def run_with_heartbeat(self, lease, work):
        return await work()


@pytest.fixture(autouse=True)
def _api_env(monkeypatch):
    # P2/P3 services construct neutral Settings(_env_file=None) bases for the
    # runtime switch; the key requirement comes from the environment.
    monkeypatch.setenv("API_KEY", "p3-test-key")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")


@pytest_asyncio.fixture
async def closure_db(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'coverage.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


async def _drain_workers() -> int:
    """Run every queued supplemental evaluation through the real handler."""

    drained = 0
    while True:
        async with session_scope() as session:
            row = await PipelineRunRepository(session).claim_next(
                worker_id="p3-coverage", kind=SUPPLEMENTAL_RULE_EVALUATE)
            if row is None:
                return drained
            job = ClaimedJob(run_id=row.id, kind=row.kind,
                lease=RunLease(row.id, "p3-coverage", row.attempt_count),
                request_json=dict(row.request_json or {}))
        outcome = await SupplementalRuleHandler(settings=Settings(_env_file=None)).execute(
            job, _InlineContext())
        assert outcome == "completed", outcome
        drained += 1


async def _coverage_payloads() -> list[dict]:
    async with session_scope() as session:
        rows = list((await session.scalars(
            select(InvestmentAssessment).where(InvestmentAssessment.kind == "rule_coverage")
        )).all())
        return [row.payload for row in rows]


async def _prepared_chain(*, seed_rule: bool, ticker: str = "600301"):
    """Listing, optional rule, case, evidence, and an admitted claimed task."""

    await set_runtime(seed_rule)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker=ticker)
        if seed_rule:
            await activate_rule(session, metric_rule(listing_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
    return case, case_ref, evidence_ref, claim


@pytest.mark.usefixtures("closure_db")
async def test_evaluation_then_prepare_builds_precise_manifests():
    case, case_ref, evidence_ref, claim = await _prepared_chain(seed_rule=True)
    business = business_input(case, evidence_ref)
    first = await run_prepare(claim, business)
    # The evaluation is admitted but has not run yet: every entry stays unknown.
    assert first["coverage_complete"] is False
    assert first["coverage_limitations"]
    rows = await _coverage_payloads()
    assert len(rows) == 1
    prepared = RuleCoverageManifest.model_validate(rows[0])
    assert prepared.stage == "prepared" and prepared.task_id == prepared.task_ref.id
    assert all(entry.status == "unknown" and entry.unknown_reason == "coverage_evaluation_missing"
               for entry in prepared.rule_entries)
    assert "coverage_unknown" in first["coverage_limitations"][0]
    assert prepared.manifest_hash == canonical_hash(
        {key: value for key, value in prepared.model_dump(mode="python").items()
         if key != "manifest_hash"})

    assert await _drain_workers() == 1
    # Different parameters force a fresh prepare action after the evaluation.
    revised = business_input(case, evidence_ref, question_scope="Bounded research after evaluation")
    second = await run_prepare(claim, revised)
    assert second["coverage_complete"] is True
    rows = await _coverage_payloads()
    assert len(rows) == 2  # two prepares, two manifests, neither overwrites the other
    evaluated = next(RuleCoverageManifest.model_validate(payload) for payload in rows
                     if payload["manifest_hash"] != prepared.manifest_hash)
    assert {entry.status for entry in evaluated.rule_entries} == {"application"}
    assert all(entry.application_ref is not None for entry in evaluated.rule_entries)
    assert evaluated.open_issue_refs  # the unknown ROE stays an explicit open issue
    # An identical replay returns the saved receipt and builds nothing new.
    replay = await run_prepare(claim, business)
    assert replay == first
    assert len(await _coverage_payloads()) == 2


@pytest.mark.usefixtures("closure_db")
async def test_unbound_prepare_matches_legacy_output_shape():
    case, case_ref, evidence_ref, claim = await _prepared_chain(seed_rule=False)
    result = await run_prepare(claim, business_input(case, evidence_ref))
    assert set(result) == {"preparation_ref", "candidate", "candidate_hash", "material_set_hash",
                           "materials", "material_count", "material_review_blocked",
                           "material_limit", "review_required", "publication_constraints"}
    assert await _coverage_payloads() == []


@pytest.mark.usefixtures("closure_db")
async def test_reviewed_candidate_takes_coverage_from_receipt_only():
    case, case_ref, evidence_ref, claim = await _prepared_chain(seed_rule=True)
    result = await run_prepare(claim, business_input(case, evidence_ref))
    candidate = PublicationCandidate.model_validate(result["candidate"])
    from investment_agent.investor.publication import SemanticAssessment

    assessments = tuple(SemanticAssessment(claim_id=c.id,
        fact_relation="The saved cash supports a bounded observation",
        counter_evidence_relevance="Temporary changes remain possible",
        materiality_reason="Cash persistence affects the observation") for c in candidate.thesis.claims)
    forged_ref = ObjectRef(kind="rule_coverage", id=uuid4(), revision=1)
    review = SemanticInput(preparation_ref=ObjectRef.model_validate(result["preparation_ref"]),
        assessments=assessments, checked_sources=(evidence_ref,), unresolved_questions=(),
        conclusion="pass", coverage_manifest_ref=forged_ref, coverage_manifest_hash="f" * 64)
    async with session_scope() as session:
        _, semantic = await reviewed_candidate(session, claim.task_id, review)
        # The model-supplied coverage identity is discarded; the receipt wins.
        assert semantic.coverage_manifest_ref == ObjectRef.model_validate(result["coverage_manifest_ref"])
        assert semantic.coverage_manifest_hash == result["coverage_manifest_hash"]
        assert semantic.coverage_manifest_ref != forged_ref


@pytest.mark.usefixtures("closure_db")
async def test_review_window_overflow_blocks_and_clears_summaries():
    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600302")
        await activate_rule(session, metric_rule(listing_ref))
        case, _ = await seed_company_view(session, listing_ref)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        # Thirty-three verified facts: one past the shared review budget.
        refs = []
        as_of = datetime.now(UTC) - timedelta(days=1)
        for index in range(32):
            record = EvidenceRecord(**metadata(), source_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1),
                source_kind="document", locator=f"p3-overflow-{index}", content_hash=f"p3-{index}",
                subject_ref=listing_ref,
                period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31), basis="annual"),
                published_at=as_of - timedelta(days=1), acquired_at=as_of - timedelta(days=1),
                available_from=as_of - timedelta(days=1), verification="verified", conflict="none",
                value=Quantity(value="1.000", unit="亿元", currency="CNY"))
            record = record.model_copy(update={"created_at": as_of - timedelta(days=1)})
            session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner",
                payload=record.model_dump(mode="json")))
            refs.append(ObjectRef(kind="evidence", id=record.id, revision=1))
        await session.flush()
    from investment_agent.investor.contracts import Claim, Dependency, NumericNode, TextNode

    claims = tuple(
        Claim(id=uuid4(), kind="fact",
            text=(TextNode(type="text", text=f"Saved comparable fact {index} "),
                  NumericNode(type="numeric_claim", ref=refs[index].model_copy(update={"field_path": "value"}),
                              role="historical_fact", display_format="decimal")),
            materiality="context", support_refs=(refs[index],), opposition_refs=(), assumption_refs=(),
            verification="verified", impact_if_wrong="The comparison must be reconsidered")
        for index in range(len(refs)))
    from investment_agent.agent.research_publication import CompanyViewInput
    from investment_agent.investor.contracts import DIMENSIONS, DimensionAssessment

    dimensions = tuple(DimensionAssessment(dimension=name, status="missing", applicable=True,
        reason="Report detail remains unavailable", refs=()) for name in DIMENSIONS)
    business = CompanyViewInput(case_ref=case_ref, question_scope="Overflow window research",
        change_reason="Overflow scenario", assessed_dimensions=dimensions, claims=claims,
        counter_case=(TextNode(type="text", text="Comparable facts may differ in kind"),),
        gaps=("Comparability not established",),
        dependencies=tuple(Dependency(ref=ref, materiality="contextual", fields=("value",)) for ref in refs),
        business_view="undetermined", price_view="undetermined", sufficiency="bounded")
    result = await run_prepare(claim, business)
    assert result["material_review_blocked"] is True
    assert result["materials"] == []  # never a partial passing review
    assert len(await _coverage_payloads()) == 1
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="review_window_exceeded"):
            await reviewed_candidate(session, claim.task_id, SemanticInput(
                preparation_ref=ObjectRef.model_validate(result["preparation_ref"]),
                assessments=(), checked_sources=(), unresolved_questions=(), conclusion="pass"))

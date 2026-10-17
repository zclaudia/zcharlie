"""AF-P3-06 integration: review, atomic publish closures, and failure semantics.

Every scenario drives the real ToolAction settle chain: admission hook, real
prepare receipts, the real evaluation worker, ``reviewed_candidate``, and
``PublicationService.publish``. No fixture doubles stand in for storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from investment_agent.agent.research_publication import (
    ClosureProposalInput,
    PublicationCandidate,
    SemanticInput,
    reviewed_candidate,
)
from investment_agent.agent.tasks import TaskService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.publication import (
    PublicationService,
    RuleReviewItem,
    SemanticAssessment,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.publication import (
    PublicationArtifact,
    PublicationDependency,
    PublicationEvent,
    PublicationOutbox,
)
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import (
    EvaluationRequest,
    PreparedClosureProposal,
    RuleCoverageManifest,
)
from investment_agent.supplemental_rules.service import SupplementalRuleService
from tests.integration.test_coverage_prepare import _drain_workers
from tests.support.research_closure import (
    BASE_SETTINGS,
    activate_rule,
    admit_research,
    business_input,
    claim_task,
    commit_progress,
    metric_rule,
    requirement_rule,
    run_prepare,
    seed_company_view,
    seed_listing,
    set_runtime,
)


@pytest.fixture(autouse=True)
def _api_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "p3-test-key")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")


@pytest_asyncio.fixture
async def closure_db(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'publish.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


def _assessment_payloads(kind: str):
    async def read():
        async with session_scope() as session:
            rows = list((await session.scalars(
                select(InvestmentAssessment).where(InvestmentAssessment.kind == kind))).all())
        return [row.payload for row in rows]
    return read()


def _applications(**match):
    async def read():
        payloads = await _assessment_payloads("rule_application")
        return [p for p in payloads if all(p.get(k) == v for k, v in match.items())]
    return read()


def _coverages(stage: str | None = None):
    async def read():
        payloads = await _assessment_payloads("rule_coverage")
        if stage is None:
            return payloads
        return [p for p in payloads if p.get("stage") == stage]
    return read()


def _artifacts(kind: str):
    async def read():
        async with session_scope() as session:
            rows = list((await session.scalars(
                select(PublicationArtifact).where(PublicationArtifact.kind == kind))).all())
        return [(row, row.payload) for row in rows]
    return read()


async def _open_application_refs() -> tuple[ObjectRef, ObjectRef]:
    """The requirement-rule and metric-rule live applications, by rule name."""

    async with session_scope() as session:
        rows = list((await session.scalars(select(InvestmentAssessment).where(
            InvestmentAssessment.kind == "rule_application"))).all())
    by_rule = {}
    for row in rows:
        by_rule[row.payload["frozen_pair"]["rule"]["name"]] = ObjectRef(
            kind="rule_application", id=UUID(row.id), revision=1)
    return (by_rule["P3 closure source requirement"], by_rule["P3 closure ROE rule"])


async def _chain(*, ticker: str = "600501"):
    """A bound task with a requirement rule (open issue) plus a metric rule."""

    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker=ticker)
        await activate_rule(session, requirement_rule(listing_ref))
        await activate_rule(session, metric_rule(listing_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    return case, case_ref, evidence_ref, claim


def _review_kwargs(candidate, evidence_ref, *, rule_reviews=(), unresolved=()) -> dict:
    assessments = tuple(SemanticAssessment(claim_id=c.id,
        fact_relation="The saved cash supports a bounded observation",
        counter_evidence_relevance="Temporary changes remain possible",
        materiality_reason="Cash persistence affects the observation")
        for c in candidate.thesis.claims)
    return {"assessments": assessments, "checked_sources": (evidence_ref,),
            "unresolved_questions": tuple(unresolved), "conclusion": "pass",
            "rule_reviews": tuple(rule_reviews)}


def _candidate_refs(result) -> tuple[PublicationCandidate, ObjectRef]:
    candidate = PublicationCandidate.model_validate(result["candidate"])
    refs = tuple(dict.fromkeys(r for c in candidate.thesis.claims
                               for r in (*c.support_refs, *c.opposition_refs, *c.assumption_refs)))
    return candidate, refs[0]


async def _reviewed(claim, result, *, rule_reviews=(), unresolved=()) -> tuple[PublicationCandidate, object]:
    kwargs = _review_kwargs(*_candidate_refs(result), rule_reviews=rule_reviews, unresolved=unresolved)
    review = SemanticInput(preparation_ref=ObjectRef.model_validate(result["preparation_ref"]), **kwargs)
    async with session_scope() as session:
        return await reviewed_candidate(session, claim.task_id, review)


async def _publish_action(claim, case_ref, candidate, semantic, *, attempt: int = 1) -> UUID:
    async with session_scope() as session:
        action = await TaskService(session).prepare_action(claim,
            intent_key=f"publish:{case_ref.id}:{attempt}",
            parameters={"candidate": candidate.model_dump(mode="json"),
                        "semantic": semantic.model_dump(mode="json")})
    return UUID(action.operation_id)


async def _publish(claim, operation, candidate, semantic):
    async with session_scope() as session:
        return await PublicationService(session).publish(
            claim, operation, candidate, semantic, base=BASE_SETTINGS)


def _proposal(target: ObjectRef, *, rationale: str = "Reviewed exposure with the saved source") -> tuple:
    body = {"target_application_refs": (target,), "evidence_refs": (),
            "claim_key": None, "rationale": rationale}
    proposal = ClosureProposalInput(**body)
    return (proposal,), PreparedClosureProposal.model_validate({**body, "proposal_hash": canonical_hash(body)})


@pytest.mark.usefixtures("closure_db")
async def test_publish_commits_closures_manifest_dependency_and_outbox_atomically():
    case, case_ref, evidence_ref, claim = await _chain()
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    proposals, frozen = _proposal(requirement_ref)
    result = await run_prepare(claim, business_input(case, evidence_ref,
        question_scope="After evaluation", closure_proposals=proposals))
    prepared = RuleCoverageManifest.model_validate(next(
        payload for payload in await _coverages("prepared")
        if payload["manifest_hash"] == result["coverage_manifest_hash"]))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref, disposition="resolved_with_closure",
                       proposal_hash=frozen.proposal_hash),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ))
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)

    assert published.published is True
    assert published.thesis_ref is not None and published.recommendation_ref is not None
    assert len(await _coverages("prepared")) == 2  # both prepared manifests survive
    published_payload = (await _coverages("published"))[0]
    assert published_payload["prepared_ref"] == prepared.reference.model_dump(mode="json")
    assert published_payload["prepared_manifest_hash"] == prepared.manifest_hash
    assert published_payload["semantic_review_ref"] == published.check.semantic_review_ref.model_dump(mode="json")
    assert published_payload["thesis_ref"] == published.thesis_ref.model_dump(mode="json")
    assert published_payload["recommendation_ref"] == published.recommendation_ref.model_dump(mode="json")
    assert published_payload["limits_recheck"] == []
    assert len(published_payload["closure_application_refs"]) == 1
    closures = await _applications(authorization="formal_review")
    assert len(closures) == 1
    closure_application = closures[0]
    assert closure_application["evaluation_purpose"] == "issue_review"
    assert closure_application["semantic_status"] == "reviewed"
    assert closure_application["closure_refs"] == [requirement_ref.model_dump(mode="json")]
    assert closure_application["open_issue_refs"] == []
    assert datetime.fromisoformat(closure_application["as_of"]) == prepared.effective_cutoff
    # The formal rule dependency rides inside the saved recommendation.
    async with session_scope() as session:
        row = await session.get(PublicationArtifact, (str(published.recommendation_ref.id), 1))
        dependencies = row.payload["dependency_refs"]
    rule_dependencies = [d for d in dependencies if d["ref"]["kind"] == "rule_coverage"]
    assert len(rule_dependencies) == 1
    assert rule_dependencies[0]["materiality"] == "critical"
    assert set(rule_dependencies[0]["fields"]) == {"rules", "open_issues", "materials"}
    assert rule_dependencies[0]["ref"]["id"] == published_payload["id"]
    async with session_scope() as session:
        dependency_rows = list((await session.scalars(select(PublicationDependency))).all())
    assert len(dependency_rows) == len(dependencies)
    assert any(row.payload["ref"]["kind"] == "rule_coverage" for row in dependency_rows)
    async with session_scope() as session:
        outbox = list((await session.scalars(select(PublicationOutbox))).all())
    assert outbox
    async with session_scope() as session:
        events = list((await session.scalars(select(PublicationEvent))).all())
    assert any(row.payload["type"] == "published" for row in events)
    # The read path rechecks the published manifest and stays unrestricted.
    async with session_scope() as session:
        view = await PublicationService(session).get_recommendation(published.recommendation_ref)
    assert view.status.value == "published", view.limitation_reasons
    assert view.limitation_reasons == ()


@pytest.mark.usefixtures("closure_db")
async def test_missing_rule_review_item_fails_check_and_keeps_issues_open():
    case, case_ref, evidence_ref, claim = await _chain(ticker="600502")
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    proposals, _ = _proposal(requirement_ref)
    result = await run_prepare(claim, business_input(case, evidence_ref,
        question_scope="After evaluation", closure_proposals=proposals))
    # The metric rule's open-issue entry has no review item at all.
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref, disposition="no_action_required"),))
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)

    assert published.published is False
    assert any("rule_reviews" in key for key in published.check.field_errors)
    assert await _applications(authorization="formal_review") == []
    assert len(await _coverages("published")) == 0
    drafts = await _artifacts("draft")
    assert drafts
    assert any("rule_reviews" in key for key in drafts[0][1]["check"]["field_errors"])
    # Open issues are exactly as they were before the failed publication.
    for ref in (requirement_ref, metric_ref):
        payload = next(item for item in await _applications() if item["id"] == str(ref.id))
        assert payload["open_issue_refs"]


@pytest.mark.usefixtures("closure_db")
async def test_proposal_hash_mismatch_fails_check():
    case, case_ref, evidence_ref, claim = await _chain(ticker="600503")
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    proposals, _ = _proposal(requirement_ref)
    result = await run_prepare(claim, business_input(case, evidence_ref,
        question_scope="After evaluation", closure_proposals=proposals))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref, disposition="resolved_with_closure",
                       proposal_hash="b" * 64),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ))
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is False
    assert any("proposal" in message.lower() for message in published.check.field_errors.values())
    assert await _applications(authorization="formal_review") == []


@pytest.mark.usefixtures("closure_db")
async def test_acknowledged_limitation_must_appear_in_unresolved_questions():
    case, case_ref, evidence_ref, claim = await _chain(ticker="600504")
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    result = await run_prepare(claim, business_input(case, evidence_ref,
        question_scope="After evaluation"))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref, disposition="limitation_acknowledged"),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ))
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is False
    assert any("unresolved questions" in message for message in published.check.field_errors.values())
    # The acknowledged limitation passes once the question is carried.
    candidate2, semantic2 = await _reviewed(claim, result,
        rule_reviews=(
            RuleReviewItem(application_ref=requirement_ref, disposition="limitation_acknowledged"),
            RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
        ),
        unresolved=("The source-requirement-open gap stays open as an acknowledged limitation",))
    operation2 = await _publish_action(claim, case_ref, candidate2, semantic2, attempt=2)
    accepted = await _publish(claim, operation2, candidate2, semantic2)
    assert accepted.published is True


@pytest.mark.usefixtures("closure_db")
async def test_series_head_advance_rejects_closure_and_keeps_issue_open():
    case, case_ref, evidence_ref, claim = await _chain(ticker="600505")
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    proposals, frozen = _proposal(requirement_ref)
    result = await run_prepare(claim, business_input(case, evidence_ref,
        question_scope="After evaluation", closure_proposals=proposals))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref, disposition="resolved_with_closure",
                       proposal_hash=frozen.proposal_hash),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ))
    # A concurrent evaluation advances the requirement series before publish.
    async with session_scope() as session:
        service = SupplementalRuleService(session)
        request = EvaluationRequest(idempotency_key=uuid4(), listing_refs=(case.listing_ref,),
            contexts={"company_research"}, mode="live")
        frozen_manifest = await service.freeze(request, as_of=datetime.now(UTC))
        pair = next(pair for pair in frozen_manifest.pairs
                    if pair.rule.name == "P3 closure source requirement")
        await service.save_pair(frozen_manifest, pair, operation_id=uuid4())
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)

    assert published.published is False
    assert "rule_coverage_closure" in published.check.field_errors
    assert await _applications(authorization="formal_review") == []
    requirement_app = next(payload for payload in await _applications()
                           if payload["id"] == str(requirement_ref.id))
    assert requirement_app["open_issue_refs"]


@pytest.mark.usefixtures("closure_db")
async def test_duplicate_publish_settle_writes_nothing_twice():
    case, case_ref, evidence_ref, claim = await _chain(ticker="600506")
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    proposals, frozen = _proposal(requirement_ref)
    result = await run_prepare(claim, business_input(case, evidence_ref,
        question_scope="After evaluation", closure_proposals=proposals))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref, disposition="resolved_with_closure",
                       proposal_hash=frozen.proposal_hash),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ))
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    first = await _publish(claim, operation, candidate, semantic)
    second = await _publish(claim, operation, candidate, semantic)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(await _applications(authorization="formal_review")) == 1
    assert len(await _coverages("published")) == 1
    async with session_scope() as session:
        outbox = list((await session.scalars(select(PublicationOutbox))).all())
    assert len(outbox) == 1


@pytest.mark.usefixtures("closure_db")
async def test_closure_exception_rolls_back_to_unchanged_open_issues():
    case, case_ref, evidence_ref, claim = await _chain(ticker="600507")
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    first_proposals, first_frozen = _proposal(requirement_ref)
    second_proposals, second_frozen = _proposal(metric_ref, rationale="Metric exposure reviewed")
    result = await run_prepare(claim, business_input(case, evidence_ref,
        question_scope="After evaluation",
        closure_proposals=(*first_proposals, *second_proposals)))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref, disposition="resolved_with_closure",
                       proposal_hash=first_frozen.proposal_hash),
        RuleReviewItem(application_ref=metric_ref, disposition="resolved_with_closure",
                       proposal_hash=second_frozen.proposal_hash),
    ))
    operation = await _publish_action(claim, case_ref, candidate, semantic)

    real_save = InvestmentAssessmentRepository.save_publication_closure
    calls = {"n": 0}

    async def flaky(self, owner_id, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("injected closure failure")
        return await real_save(self, owner_id, **kwargs)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(InvestmentAssessmentRepository, "save_publication_closure", flaky)
        published = await _publish(claim, operation, candidate, semantic)

    assert published.published is False
    assert "rule_coverage_closure" in published.check.field_errors
    # The nested savepoint rolled the first closure back as well.
    assert await _applications(authorization="formal_review") == []
    assert len(await _coverages("published")) == 0
    for ref in (requirement_ref, metric_ref):
        payload = next(item for item in await _applications() if item["id"] == str(ref.id))
        assert payload["open_issue_refs"]


@pytest.mark.usefixtures("closure_db")
async def test_legacy_task_publishes_without_rule_dependencies():
    await set_runtime(False)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker="600508")
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    result = await run_prepare(claim, business_input(case, evidence_ref))
    assert "coverage_manifest_ref" not in result
    candidate, semantic = await _reviewed(claim, result)
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is True
    assert await _coverages() == []
    async with session_scope() as session:
        dependency_rows = list((await session.scalars(select(PublicationDependency))).all())
    assert len(dependency_rows) == 1
    assert dependency_rows[0].payload["ref"]["kind"] == "evidence"


@pytest.mark.usefixtures("closure_db")
async def test_disabled_switch_after_binding_fails_the_publication():
    case, case_ref, evidence_ref, claim = await _chain(ticker="600509")
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() == 1
    requirement_ref, metric_ref = await _open_application_refs()
    result = await run_prepare(claim, business_input(case, evidence_ref,
        question_scope="After evaluation"))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref, disposition="no_action_required"),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ))
    await set_runtime(False)  # the switch cannot waive a bound task's chain
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is False
    assert "supplemental_rules_disabled_with_rule_binding" in published.check.field_errors["dependency_refs.current"]
    assert len(await _coverages("published")) == 0


@pytest.mark.usefixtures("closure_db")
async def test_directional_view_refuses_unknown_coverage_but_non_directional_publishes():
    # Directional with unknown coverage (the worker never ran) must fail.
    case, case_ref, evidence_ref, claim = await _chain(ticker="600510")
    result = await run_prepare(claim, business_input(case, evidence_ref, business_view="unfavorable"))
    candidate, semantic = await _reviewed(claim, result)
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    blocked = await _publish(claim, operation, candidate, semantic)
    assert blocked.published is False
    assert blocked.check.field_errors.get("rule_coverage.directional")

    # Non-directional may publish with the same recorded unknown coverage.
    case2, case_ref2, evidence_ref2, claim2 = await _chain(ticker="600511")
    result2 = await run_prepare(claim2, business_input(case2, evidence_ref2))
    candidate2, semantic2 = await _reviewed(claim2, result2)
    operation2 = await _publish_action(claim2, case_ref2, candidate2, semantic2)
    published = await _publish(claim2, operation2, candidate2, semantic2)
    assert published.published is True
    manifest = RuleCoverageManifest.model_validate((await _coverages("published"))[0])
    assert {entry.status for entry in manifest.rule_entries} == {"unknown"}

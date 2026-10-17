"""Real-chain fixtures for the P3 admission/coverage/publication tests.

Everything runs on real SQLite rows: A-share compatible listings so the P2
subject freeze works, active rules seeded through the actual repository, and
admissions through the real ``admit`` boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionGrant, admit
from investment_agent.agent.research import ResearchHypothesis, ResearchProgress
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import DIMENSIONS, DimensionAssessment
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.contracts import RuleDefinition

BASE_SETTINGS = Settings(_env_file=None, api_key="ci-inert-api-key", investor_agent_enabled=True,
                         investor_agent_task_seconds=600)
BOUND_SETTINGS = BASE_SETTINGS.model_copy(update={"supplemental_rules_enabled": True})


async def seed_listing(session: AsyncSession, *, ticker: str = "600101") -> ObjectRef:
    """One A-share SSE listing compatible with the P2 subject freeze."""

    from sqlalchemy import select

    from investment_agent.storage.models.reference import Market as MarketRow

    market = (await session.execute(
        select(MarketRow).where(MarketRow.code == "SSE")
    )).scalar_one_or_none()
    if market is None:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name=f"P3 closure issuer {ticker}")
    session.add(company)
    await session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker=ticker)
    session.add(listing)
    await session.flush()
    return ObjectRef(kind="listing", id=listing.id, revision=1)


def metric_rule(listing_ref: ObjectRef, *, rule_id=None, contexts=("company_research",)) -> RuleDefinition:
    now = datetime.now(UTC)
    return RuleDefinition(
        id=rule_id or uuid4(), revision=1, name="P3 closure ROE rule",
        rationale="A low saved ROE requires review.", source_refs=(),
        selector={"kind": "listing", "listing_ref": listing_ref.model_dump(mode="python")},
        contexts=set(contexts), rule_type="metric_condition",
        definition={"kind": "metric_condition", "condition": {"kind": "all", "conditions": [
            {"kind": "factor", "factor_id": "roe", "operator": "<", "value": "0.05", "unit": "fraction"}]}},
        dimensions={"financial_cash"}, effects={"require_review"}, severity="warning",
        materiality="Synthetic P3 fixture only.", valid_from=now - timedelta(days=1),
        review_due_at=now + timedelta(days=30), release_requirements=("Review exact saved inputs.",),
    )


def requirement_rule(listing_ref: ObjectRef, *, rule_id=None) -> RuleDefinition:
    """A rule whose missing required evidence yields a triggered, open issue."""

    now = datetime.now(UTC)
    return RuleDefinition(
        id=rule_id or uuid4(), revision=1, name="P3 closure source requirement",
        rationale="A missing saved source must stay open until reviewed.",
        source_refs=(),
        selector={"kind": "listing", "listing_ref": listing_ref.model_dump(mode="python")},
        contexts={"company_research"}, rule_type="research_requirement",
        definition={"kind": "research_requirement", "question": "Is the saved source present?",
            "required_evidence": ["p3_missing_locator"], "completion_criteria": "Verified answer",
            "falsification_criteria": "Contradictory saved source", "check_by": now - timedelta(hours=1),
            "incomplete_effects": ["require_review"]},
        dimensions={"business_demand"}, effects={"require_review"}, severity="warning",
        materiality="Synthetic P3 fixture only.", valid_from=now - timedelta(days=1),
        review_due_at=now + timedelta(days=30), release_requirements=("Save the exact source.",),
        issue_key="source-requirement-open",
    )


async def activate_rule(session: AsyncSession, rule: RuleDefinition) -> RuleDefinition:
    """Save and activate one rule revision through the real repository heads."""

    store = SupplementalRulesRepository(session)
    await store.reserve_registry("local_owner")
    saved = await store.save_rule_revision("local_owner", rule)
    await store.put_rule_head("local_owner", saved.id, expected_head_revision=0,
        active_revision=saved.revision, active_state="active", draft_revision=None,
        draft_state=None, last_validation_revision=None, last_validation_hash=None,
        last_validation_payload=None)
    await store.advance_registry("local_owner", rules_delta=1, updated_at=datetime.now(UTC))
    await session.flush()
    return saved


async def admit_research(
    session: AsyncSession, listing_ref: ObjectRef, *, settings: Settings = BOUND_SETTINGS,
    research_kind: str = "company_research", input_key: str | None = None,
    as_of: datetime | None = None,
) -> object:
    """Admit one full-research task through the real boundary.

    A caller-supplied ``as_of`` pins the ContextBinding so an identical retry
    of the same input key stays byte-identical, like a worker re-admission.
    """

    grant = ExecutionGrant(mode="interactive", source="authenticated_message",
        prompt="Research the company", tool_names=("prepare_company_view", "publish_recommendation"),
        scope_refs=(listing_ref,), research_kind=research_kind)  # type: ignore[arg-type]
    return await admit(session, base=settings, input_key=input_key or f"p3-{uuid4()}",
        binding=ContextBinding(context_refs=(listing_ref,), base_refs=(listing_ref,),
                               as_of=as_of or datetime.now(UTC)),
        grant=grant)  # type: ignore[arg-type]


def full_progress() -> ResearchProgress:
    """Minimal valid company_research progress for the prepare prerequisite."""

    dimensions = tuple(DimensionAssessment(dimension=name, status="missing", applicable=True,
        reason="Report detail remains unavailable", refs=()) for name in DIMENSIONS)
    return ResearchProgress(question="Bounded company review", kind="company_research",
        hypotheses=(ResearchHypothesis(question="Is cash durable?", explanation="Operating cash may recur",
            alternative="Temporary working capital release",
            falsification_condition="Cash reverses without operating growth",
            evidence_priority="Check cash components first"),),
        evidence_steps=("Check reported cash and opposing explanations",),
        completion_criteria=("Explain scope and material gaps",),
        stop_conditions=("Stop when critical source unavailable",), dimensions=dimensions,
        completed_steps=("Clarified scope and alternatives",), answered=True)


async def commit_progress(session: AsyncSession, task, claim) -> None:
    """Settle the exact completion:progress action prepare requires."""

    from investment_agent.agent.research import save_progress

    service = TaskService(session)
    action = await service.prepare_action(claim, intent_key="completion:progress",
        parameters={"tool": "save_research_progress", "phase": "research:v1",
                    "business": full_progress().model_dump(mode="json")})
    await service.settle_action(claim, action.operation_id,
        lambda s, op: save_progress(s, op, full_progress()))


async def seed_company_view(session: AsyncSession, listing_ref: ObjectRef) -> tuple[object, ObjectRef]:
    """Mandate, case, and one verified saved fact, mirroring the publication fixture."""

    from datetime import date, timedelta

    from investment_agent.evidence.contracts import EvidenceRecord, Period
    from investment_agent.investor.common import Quantity
    from investment_agent.investor.contracts import Mandate, ResearchAuthorization
    from investment_agent.investor.foundations import CreateCase, InvestorService
    from investment_agent.storage.models.evidence import SavedEvidence
    from tests.support.publication import metadata

    svc = InvestorService(session)
    unknown = Quantity(value=None, unit="USD", unknown_reason="Not specified")
    from investment_agent.investor.foundations import InvestorError

    try:
        await svc.create_mandate("local_owner", Mandate(**metadata(), goals=(),
            scope_refs=(listing_ref,), hard_constraints=(), unknowns=(),
            authorization=ResearchAuthorization(daily_budget=unknown, per_task_budget=unknown,
                max_leads_per_batch=0, max_new_tasks_per_batch=0, source_refs=())), uuid4())
    except InvestorError as exc:
        if exc.detail.code != "revision_conflict":
            raise  # one mandate per owner; later chains reuse it
    case = await svc.create_case("local_owner", CreateCase(listing_ref=listing_ref,
        question="Review the company", idempotency_key=uuid4()))
    as_of = datetime.now(UTC) - timedelta(days=1)
    record = EvidenceRecord(**metadata(), source_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1),
        source_kind="document", locator=f"p3-fixture-{int(listing_ref.id)}", content_hash="p3-fixture",
        subject_ref=listing_ref,
        period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31), basis="annual"),
        published_at=as_of - timedelta(days=1), acquired_at=as_of - timedelta(days=1),
        available_from=as_of - timedelta(days=1), verification="verified", conflict="none",
        value=Quantity(value="12.345", unit="亿元", currency="CNY"))
    record = record.model_copy(update={"created_at": as_of - timedelta(days=1)})
    session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner",
        payload=record.model_dump(mode="json")))
    await session.flush()
    return case, ObjectRef(kind="evidence", id=record.id, revision=1)


def business_input(case, evidence_ref: ObjectRef, *, question_scope: str = "Bounded closure research",
                   closure_proposals: tuple = (), business_view: str = "undetermined",
                   price_view: str = "undetermined") -> object:
    """One valid CompanyViewInput over the seeded fact claim."""

    from investment_agent.agent.research_publication import CompanyViewInput
    from investment_agent.investor.contracts import (
        Claim,
        Dependency,
        NumericNode,
        TextNode,
    )

    claim = Claim(id=uuid4(), kind="fact",
        text=(TextNode(type="text", text="Saved cash position "),
              NumericNode(type="numeric_claim", ref=evidence_ref.model_copy(update={"field_path": "value"}),
                          role="historical_fact", display_format="decimal")),
        materiality="critical", support_refs=(evidence_ref,), opposition_refs=(), assumption_refs=(),
        verification="verified", impact_if_wrong="The cash assessment must be reconsidered")
    dimensions = tuple(DimensionAssessment(dimension=name, status="missing", applicable=True,
        reason="Report detail remains unavailable", refs=()) for name in DIMENSIONS)
    counter = (TextNode(type="text", text="Cash may reflect temporary working capital"),)
    return CompanyViewInput(case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision),
        question_scope=question_scope, change_reason="Initial closure research",
        assessed_dimensions=dimensions, claims=(claim,), counter_case=counter,
        gaps=("Durability not established",),
        dependencies=(Dependency(ref=evidence_ref, materiality="critical", fields=("value",),),),
        business_view=business_view, price_view=price_view, sufficiency="bounded",
        closure_proposals=closure_proposals)


async def claim_task(session: AsyncSession, task) -> object:
    """Claim the task's queued agent_turn run, exactly like the executor would."""

    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease

    run = await PipelineRunRepository(session).claim_next(worker_id=str(task.id), kinds=["agent_turn"])
    assert run is not None
    return await TaskService(session).claim(task.id, RunLease(run.id, str(task.id), run.attempt_count))


async def run_prepare(claim, business) -> dict:
    """Run the real prepare tool through prepare_action/settle_action.

    A committed action replays its saved result without executing again, so
    repeated calls with identical parameters never build a second manifest.
    """

    from investment_agent.agent.research_publication import prepare
    from investment_agent.agent.tasks import TaskService, fingerprint

    parameters = {"tool": "prepare_company_view", "business": business.model_dump(mode="json"), "phase": "research:v1"}
    async with session_scope() as owned:
        service = TaskService(owned)
        action = await service.prepare_action(claim,
            intent_key="tool:" + fingerprint(parameters), parameters=parameters)
        if action.state == "committed":
            assert action.result is not None
            return action.result
        operation = UUID(action.operation_id)
    async with session_scope() as owned:
        result = await TaskService(owned).settle_action(
            claim, operation, lambda s, op: prepare(s, op, business))
    assert isinstance(result, dict)
    return result


async def set_runtime(enabled: bool) -> None:
    """Flip the runtime supplemental switch through the real settings store.

    The one-time settings import is claimed first: the overlay only reads the
    store after ``imported_at`` is set, exactly like the API lifespan does.
    """

    from investment_agent.config import Settings
    from investment_agent.settings.loader import store_for
    from investment_agent.storage.database import session_scope

    async def apply() -> None:
        async with session_scope() as session:
            store = store_for(session, Settings(_env_file=None))
            await store.claim_import()
            # After the import the DB is the source of truth for every runtime
            # key, so the agent gate is stored alongside the P3 switch.
            await store.apply({"supplemental_rules_enabled": enabled,
                               "investor_agent_enabled": True}, actor="p3-closure-test")

    await apply()

"""Method-evolution scenario builders (moved from tests/test_method_evolution.py)."""

from uuid import UUID

from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from investment_agent.agent.research import ResearchOutput
from investment_agent.agent.research_publication import CompanyViewInput, SemanticInput
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.methods import MethodService
from investment_agent.investor.publication import (
    PublicationCandidate,
    PublicationService,
    SemanticAssessment,
)
from investment_agent.storage.database import session_scope
from tests.support.conversations import message, new_conversation, submit
from tests.support.publication import fixture
from tests.support.research import full_progress, response, run


async def samples():
    candidate, evidence, subject = await fixture()
    conversation = await new_conversation()
    failure_receipt = await submit(conversation, message(text="深入研究", context_refs=(subject,)))
    weak = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(evidence,), answered=False)
    failure = await run(failure_receipt, FunctionModel(lambda m, i: response(ResearchOutput(progress=weak))))
    assert failure.status.value == "partial"
    receipt = await submit(conversation, message(text="深入研究", context_refs=(subject,)))
    alternative = weak.hypotheses[0].model_copy(update={"alternative": "Demand may temporarily exceed sustainable capacity"})
    strong = weak.model_copy(update={"hypotheses": (*weak.hypotheses, alternative), "answered": True})
    business = CompanyViewInput(case_ref=candidate.thesis.case_ref, question_scope="Bounded research", change_reason="Initial research", assessed_dimensions=candidate.thesis.assessed_dimensions, claims=candidate.thesis.claims, counter_case=candidate.thesis.counter_case, gaps=candidate.recommendation.gaps, dependencies=candidate.recommendation.dependency_refs, business_view="undetermined", price_view="undetermined", sufficiency="bounded")
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "save_research_progress" for p in returns):
            return ModelResponse([ToolCallPart("save_research_progress", {"business": strong.model_dump(mode="json")})])
        if not any(p.tool_name == "prepare_company_view" for p in returns):
            return ModelResponse([ToolCallPart("prepare_company_view", {"business": business.model_dump(mode="json")})])
        prepared = next(p.content for p in returns if p.tool_name == "prepare_company_view")
        exact = PublicationCandidate.model_validate(prepared["candidate"])
        semantic = SemanticInput(preparation_ref=ObjectRef.model_validate(prepared["preparation_ref"]), assessments=tuple(SemanticAssessment(claim_id=c.id, fact_relation="Exact saved cash supports this limited observation", counter_evidence_relevance="Temporary working capital remains possible", materiality_reason="Cash persistence affects the observation") for c in exact.thesis.claims), checked_sources=(evidence,), unresolved_questions=(), conclusion="pass")
        return response(ResearchOutput(progress=strong, semantic_review=semantic))
    success = await run(receipt, FunctionModel(model))
    assert success.status.value == "succeeded"
    async with session_scope() as session:
        svc = MethodService(session)
        failed_review = await svc.process_review(failure.id)
        success_review = await svc.process_review(success.id)
        assert await svc.process_review(failure.id) == failed_review
    return subject, failure, success, failed_review, success_review, weak, strong


async def fresh_native_tasks(subject, weak, strong):
    """Explicit new actual research after evaluation, under existing executor budgets."""
    from investment_agent.agent.research import ResearchProgress
    from investment_agent.storage.models.investor import InvestorCase
    async with session_scope() as session:
        publication = PublicationService(session)
        case_row = await session.scalar(select(InvestorCase).where(InvestorCase.listing_id == subject.id))
        case = await publication.foundation.get_case("local_owner", UUID(case_row.id))
        thesis = await publication.get_thesis(case.current_thesis_ref)
        view = await publication.get_recommendation(case.current_recommendation_ref)
    weak = ResearchProgress.model_validate(weak) if isinstance(weak, dict) else weak
    strong = ResearchProgress.model_validate(strong) if isinstance(strong, dict) else strong
    failed_receipt = await submit(await new_conversation(), message(text="深入研究新的反向解释", context_refs=(subject,)))
    failed = await run(failed_receipt, FunctionModel(lambda m, i: response(ResearchOutput(progress=weak))))
    assert failed.status.value == "partial", (failed.status, failed.stop_reason)
    success_receipt = await submit(await new_conversation(), message(text="深入研究新的反例", context_refs=(subject,)))
    business = CompanyViewInput(case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision), question_scope="Fresh bounded research", change_reason="Explicit new research", assessed_dimensions=thesis.assessed_dimensions, claims=thesis.claims, counter_case=thesis.counter_case, gaps=view.recommendation.gaps, dependencies=view.recommendation.dependency_refs, business_view="undetermined", price_view="undetermined", sufficiency="bounded")
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "save_research_progress" for p in returns):
            return ModelResponse([ToolCallPart("save_research_progress", {"business": strong.model_dump(mode="json")})])
        if not any(p.tool_name == "prepare_company_view" for p in returns):
            return ModelResponse([ToolCallPart("prepare_company_view", {"business": business.model_dump(mode="json")})])
        prepared = next(p.content for p in returns if p.tool_name == "prepare_company_view")
        exact = PublicationCandidate.model_validate(prepared["candidate"])
        refs = tuple(dict.fromkeys(r for c in exact.thesis.claims for r in (*c.support_refs, *c.opposition_refs, *c.assumption_refs)))
        review = SemanticInput(preparation_ref=ObjectRef.model_validate(prepared["preparation_ref"]), assessments=tuple(SemanticAssessment(claim_id=c.id, fact_relation="Exact saved facts support a bounded observation", counter_evidence_relevance="Temporary changes remain possible", materiality_reason="Persistence affects this observation") for c in exact.thesis.claims), checked_sources=refs, unresolved_questions=(), conclusion="pass")
        return response(ResearchOutput(progress=strong, semantic_review=review))
    success = await run(success_receipt, FunctionModel(model))
    assert success.status.value == "succeeded"
    return failed, success

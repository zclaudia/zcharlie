"""Investor control, monitoring and foundations helpers (moved from
tests/test_investor_controls.py, tests/test_investor_monitoring.py and
tests/test_investor_foundations.py)."""

from datetime import UTC, datetime
from uuid import uuid4

from investment_agent.agent.research import ResearchHypothesis, ResearchProgress
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import Mandate, ResearchAuthorization, TextNode
from investment_agent.investor.controls import (
    ExecuteRequest,
    UserControlService,
    authenticated_request,
)
from investment_agent.investor.foundations import InvestorService
from investment_agent.storage.database import session_scope
from tests.support.publication import fixture, publish


def mandate() -> Mandate:
    unknown = Quantity(value=None, unit="CNY", unknown_reason="budget not provided")
    return Mandate(
        id=uuid4(),
        revision=1,
        created_at=datetime.now(UTC),
        actor="untrusted_client",
        origin_ref=None,
        goals=(),
        scope_refs=(),
        hard_constraints=(),
        unknowns=("investment horizon",),
        authorization=ResearchAuthorization(
            daily_budget=unknown,
            per_task_budget=unknown,
            max_leads_per_batch=0,
            max_new_tasks_per_batch=0,
            source_refs=(),
        ),
    )


def request(parameters, key=None):
    return ExecuteRequest(parameters=parameters, idempotency_key=key or uuid4())


async def execute(session, parameters, key=None):
    return await UserControlService(session).execute(request(parameters, key), authenticated_request(parameters))


async def case_ref(candidate):
    async with session_scope() as session:
        case = await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)
        return ObjectRef(kind="case", id=case.id, revision=case.revision)


async def saved():
    candidate, evidence, subject = await fixture()
    result = await publish(candidate)
    return candidate, evidence, subject, result


def full_progress(candidate, evidence):
    return ResearchProgress(kind="review", question="Review unchanged premises", hypotheses=(ResearchHypothesis(question="Are cash premises unchanged?", explanation="Historical cash supports a bounded view", alternative="Temporary cash explains it", falsification_condition="New verified contradiction", evidence_priority="Query exact cash evidence"),),
        evidence_steps=("Query exact evidence",), completion_criteria=("All critical premises checked",), stop_conditions=("Missing evidence",), dimensions=candidate.thesis.assessed_dimensions,
        fact_refs=(evidence,), completed_steps=("Query exact evidence",), content=(TextNode(type="text", text="The bounded view is maintained after checking its premises."),), answered=True)

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from investment_agent.agent.company_context import prior_company_view, reuse_company_sources
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.evidence import SavedEvidence
from tests.support.conversations import conversation_db as conversation_db
from tests.support.publication import fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["verified", "candidate", "conflict", "future", "other_owner", "wrong_subject", "oversize"])
async def test_reusable_sources_preserve_exact_quality_scope_and_visibility(conversation_db, state):
    candidate, ref, subject = await fixture()
    cutoff = datetime.now(UTC)
    async with session_scope() as session:
        row = await session.get(SavedEvidence, (str(ref.id), 1))
        record = dict(row.payload)
        record["id"] = str(uuid4())
        record["verification"] = "candidate" if state == "candidate" else "verified"
        if state == "conflict":
            record["conflict"] = "unresolved"
        if state == "future":
            record["acquired_at"] = (cutoff + timedelta(seconds=1)).isoformat()
        if state == "wrong_subject":
            record["subject_ref"] = {**record["subject_ref"], "id": subject.id + 1}
        if state == "oversize":
            record["text"] = "untrusted source " * 2000
        session.add(SavedEvidence(id=record["id"], revision=1,
            owner_id="other_owner" if state == "other_owner" else "local_owner", payload=record))
        await session.flush()
        exact = ObjectRef(kind="evidence", id=record["id"], revision=1)
        recommendation = candidate.recommendation.model_copy(update={"provenance": (exact,)})
        result = await reuse_company_sources(session, owner="local_owner", subject=subject,
            cutoff=cutoff, recommendation=recommendation)
        if state in {"future", "other_owner", "wrong_subject", "oversize"}:
            assert not result["records"] and result["omitted"][0]["ref"] == exact.model_dump(mode="json")
        else:
            loaded = result["records"][0]
            assert loaded["record"] == record and loaded["ref"] == exact.model_dump(mode="json")
            assert loaded["eligibility"]["fact_reference_allowed"] == (state != "conflict")
            assert not result["omitted"]
        view = prior_company_view(recommendation, ObjectRef(kind="recommendation", id=recommendation.id, revision=1))
        assert "claims" not in view and view["provenance"] == [exact.model_dump(mode="json")]
        assert view["business_view"] == recommendation.business_view


@pytest.mark.asyncio
async def test_source_window_deduplicates_exact_refs_and_reports_every_omission(conversation_db):
    candidate, ref, subject = await fixture()
    async with session_scope() as session:
        recommendation = candidate.recommendation.model_copy(update={"provenance": (ref, ref)})
        reused = await reuse_company_sources(session, owner="local_owner", subject=subject,
            cutoff=datetime.now(UTC), recommendation=recommendation)
        assert len(reused["records"]) == 1
        bounded = await reuse_company_sources(session, owner="local_owner", subject=subject,
            cutoff=datetime.now(UTC), recommendation=recommendation, max_records=0)
        assert not bounded["records"] and len(bounded["omitted"]) == 1
        assert bounded["omitted"][0]["reason"] == "source_window_limit"


@pytest.mark.asyncio
async def test_real_http_full_company_reuses_published_sources_and_completes_with_small_daily_headroom(conversation_db):
    import json

    import httpx
    from sqlalchemy import select

    from investment_agent.agent.executor import AgentExecutor
    from investment_agent.agent.job import AgentJobHandler
    from investment_agent.agent.research import ResearchOutput, ResearchPolicy, registrations
    from investment_agent.agent.research_publication import CompanyViewInput, SemanticInput
    from investment_agent.agent.tasks import TaskService
    from investment_agent.investor.publication import PublicationCandidate, SemanticAssessment
    from investment_agent.research.worker import ResearchWorker
    from tests.support.conversations import base, message, new_conversation, submit
    from tests.support.publication import publish
    from tests.support.research import full_progress, wire_results

    candidate, ref, subject = await fixture()
    assert (await publish(candidate)).published
    selected = base(investor_agent_daily_tokens=100000, investor_agent_task_tokens=2000000,
        investor_agent_daily_requests=6, agent_max_tokens=16384)
    receipt = await submit(await new_conversation(), message(text="这家公司为什么值得关注？",
        context_refs=(subject,), research_kind="company_research"), settings=selected)
    plan = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(ref,))
    requests = []

    def gateway(request):
        body = json.loads(request.content)
        requests.append(body)
        system = next(m["content"] for m in body["messages"] if m["role"] == "system")
        context = json.JSONDecoder().raw_decode(system.split("Trusted current context:\n", 1)[1])[0]
        company = context["company_context"][0]
        assert "claims" not in company["recommendation"]
        assert company["reusable_sources"]["records"][0]["ref"] == ref.model_dump(mode="json")
        if len(requests) == 1:
            call = ("save_research_progress", plan.model_copy(update={"answered": False}).model_dump(mode="json"))
        elif len(requests) == 2:
            case = company["case"]
            business = CompanyViewInput(case_ref=ObjectRef(kind="case", id=case["id"], revision=case["revision"]),
                question_scope="Bounded whole company study", change_reason="Recheck owned sources",
                assessed_dimensions=plan.dimensions, claims=candidate.thesis.claims, counter_case=candidate.thesis.counter_case,
                gaps=candidate.recommendation.gaps, dependencies=candidate.recommendation.dependency_refs,
                business_view="undetermined", price_view="undetermined", sufficiency="bounded")
            call = ("prepare_company_view", business.model_dump(mode="json"))
        else:
            assert len(requests) == 3
            prepared = wire_results(body, "prepare_company_view")[-1]
            exact = PublicationCandidate.model_validate(prepared["candidate"])
            assert prepared["materials"][0]["exact_record"]
            review = SemanticInput(preparation_ref=ObjectRef.model_validate(prepared["preparation_ref"]),
                assessments=tuple(SemanticAssessment(claim_id=c.id,
                    fact_relation="The saved cash is only a historical observation",
                    counter_evidence_relevance="Temporary working capital could explain cash",
                    materiality_reason="Durability remains unproven") for c in exact.thesis.claims),
                checked_sources=(ref,), unresolved_questions=(), conclusion="pass")
            msg = {"role": "assistant", "content": ResearchOutput(progress=plan, semantic_review=review).model_dump_json()}
        if len(requests) < 3:
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{len(requests)}", "type": "function",
                "function": {"name": call[0], "arguments": json.dumps({"business": call[1]})}}]}
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture",
            "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 17000, "completion_tokens": 2000, "total_tokens": 19000}})

    executor = AgentExecutor(selected, transport=httpx.MockTransport(gateway), registrations=registrations(),
        specification=ResearchPolicy(selected).specification())
    await ResearchWorker(settings=selected, worker_id="small-reuse", handlers=[AgentJobHandler("agent_turn", executor)]).run_once()
    async with session_scope() as session:
        task = await TaskService(session).get_task(receipt.task.id)
        assert task.status == "succeeded" and task.usage.requests == 3 and task.usage.tool_calls == 2
        assert task.usage.input_tokens + task.usage.output_tokens == 57000
        assert len((await session.scalars(select(SavedEvidence))).all()) == 1

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
        if state in {"future", "other_owner", "wrong_subject"}:
            assert not result["records"] and result["omitted"][0]["ref"] == exact.model_dump(mode="json")
        else:
            loaded = result["records"][0]
            if state == "oversize":
                assert loaded["record"] == {k: v for k, v in record.items() if k != "text"}
                assert loaded["text_window"]["text"] == record["text"][:600]
                assert loaded["text_window"]["full_length"] == len(record["text"])
            else:
                assert loaded["record"] == record
            assert loaded["ref"] == exact.model_dump(mode="json")
            assert loaded["eligibility"]["fact_reference_allowed"] == (state != "conflict")
            assert loaded["eligibility"]["critical_premise_allowed"] == (state in {"verified", "oversize"})
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
@pytest.mark.parametrize("large_source", [False, True])
@pytest.mark.parametrize("continuation", [False, True])
async def test_real_http_full_company_reuses_published_sources_and_completes_with_small_daily_headroom(conversation_db, large_source, continuation):
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
    from tests.support.research import full_progress, response, run, wire_results

    candidate, ref, subject = await fixture()
    if large_source:
        from investment_agent.investor.contracts import NumericNode
        async with session_scope() as session:
            original = await session.get(SavedEvidence, (str(ref.id), 1))
            payload = {**original.payload, "id": str(uuid4()), "text": "qualitative original source; " * 340}
            session.add(SavedEvidence(id=payload["id"], revision=1, owner_id="local_owner", payload=payload))
            ref = ObjectRef(kind="evidence", id=payload["id"], revision=1)
            claims = tuple(c.model_copy(update={"support_refs": (ref,), "text": tuple(
                n.model_copy(update={"ref": ref.model_copy(update={"field_path": "value"})})
                if isinstance(n, NumericNode) else n for n in c.text)}) for c in candidate.thesis.claims)
            dependencies = tuple(d.model_copy(update={"ref": ref}) for d in candidate.recommendation.dependency_refs)
            candidate = candidate.model_copy(update={"thesis": candidate.thesis.model_copy(update={"claims": claims}),
                "recommendation": candidate.recommendation.model_copy(update={"claims": claims,
                    "provenance": (ref,), "dependency_refs": dependencies})})
    assert (await publish(candidate)).published
    conversation = await new_conversation()
    previous = None
    if continuation:
        from pydantic_ai.models.function import FunctionModel
        previous = await submit(conversation, message(text="这家公司为什么值得关注？",
            context_refs=(subject,), research_kind="company_research"))
        partial = full_progress(candidate.thesis.assessed_dimensions, answered=False, stopped_because="Source coverage remains incomplete")
        previous = await run(previous, FunctionModel(lambda messages, info: response(ResearchOutput(progress=partial))))
        assert previous.status == "partial"
    selected = base(investor_agent_daily_tokens=68000 if continuation else 100000, investor_agent_task_tokens=2000000,
        investor_agent_daily_requests=4 if continuation else 6, agent_max_tokens=8192 if continuation else 16384)
    receipt = await submit(conversation, message(text="这家公司为什么值得关注？",
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
        step = len(requests) + int(continuation)
        if continuation:
            assert context["reused_research_plan"]["source_task_id"] == str(previous.id)
        if step == 1:
            assert {t["function"]["name"] for t in body["tools"]} == {"save_research_progress"}
            call = ("save_research_progress", plan.model_copy(update={"answered": False}).model_dump(mode="json"))
        elif step == 2:
            if continuation:
                assert {t["function"]["name"] for t in body["tools"]} == {"prepare_company_view"}
            case = company["case"]
            business = CompanyViewInput(case_ref=ObjectRef(kind="case", id=case["id"], revision=case["revision"]),
                question_scope="Bounded whole company study", change_reason="Recheck owned sources",
                assessed_dimensions=plan.dimensions, claims=candidate.thesis.claims, counter_case=candidate.thesis.counter_case,
                gaps=candidate.recommendation.gaps, dependencies=candidate.recommendation.dependency_refs,
                business_view="undetermined", price_view="undetermined", sufficiency="bounded")
            call = ("prepare_company_view", business.model_dump(mode="json"))
        else:
            assert step == 3
            assert not body.get("tools")
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
        if step < 3:
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
        assert task.status == "succeeded" and task.usage.requests == 3 - int(continuation) and task.usage.tool_calls == 2 - int(continuation)
        assert task.usage.input_tokens + task.usage.output_tokens == 19000 * (3 - int(continuation))
        assert len((await session.scalars(select(SavedEvidence))).all()) == 1 + int(large_source)


def test_synthesis_only_schema_gate_restores_preparation_on_feedback_and_keeps_blocked_materials():
    from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolReturnPart

    from investment_agent.agent.tools import final_review_ready

    prepared = ToolReturnPart(tool_name="prepare_company_view", tool_call_id="prepared", content={"candidate": {"claims": ["exact"]}})
    assert final_review_ready([ModelRequest(parts=[prepared])])
    feedback = RetryPromptPart(content="Revise the exact candidate before publication", tool_name=None)
    assert not final_review_ready([ModelRequest(parts=[prepared]), ModelRequest(parts=[feedback])])
    blocked = ToolReturnPart(tool_name="prepare_company_view", tool_call_id="blocked",
        content={"candidate": {"claims": ["exact"]}, "material_review_blocked": True})
    assert not final_review_ready([ModelRequest(parts=[blocked])])


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["question", "conversation", "scope", "method", "cutoff"])
async def test_public_plan_continuation_never_crosses_question_scope_method_or_cutoff(conversation_db, mismatch):
    from pydantic_ai.models.function import FunctionModel

    from investment_agent.agent.company_plan import reuse_question_plan
    from investment_agent.agent.execution import ExecutionGrant
    from investment_agent.agent.research import ResearchOutput
    from investment_agent.agent.tasks import TaskService
    from investment_agent.investor.methods import task_method
    from investment_agent.storage.models.agent import ExecutionGrantRow, MessageRow
    from tests.support.conversations import claim, message, new_conversation, submit
    from tests.support.research import full_progress, response, run

    candidate, _, subject = await fixture()
    conversation = await new_conversation()
    first = await submit(conversation, message(text="这家公司为什么值得关注？", context_refs=(subject,), research_kind="company_research"))
    partial = full_progress(candidate.thesis.assessed_dimensions, answered=False, stopped_because="Specific original materials remain missing")
    await run(first, FunctionModel(lambda messages, info: response(ResearchOutput(progress=partial))))
    if mismatch == "conversation":
        conversation = await new_conversation()
    arguments = {"text": "检查资产负债" if mismatch == "question" else "这家公司为什么值得关注？",
                 "context_refs": (subject,), "research_kind": "company_research"}
    if mismatch == "cutoff":
        arguments["research_at"] = first.task.binding.as_of - timedelta(minutes=1)
    second = await submit(conversation, message(**arguments))
    owned = await claim(second.task)
    async with session_scope() as session:
        grant = ExecutionGrant.model_validate((await session.get(ExecutionGrantRow, str(second.task.id))).payload)
        if mismatch == "scope":
            grant = grant.model_copy(update={"scope_refs": (subject.model_copy(update={"id": subject.id + 1}),)})
        method = (await task_method(session, second.task.id)).method_ref
        if mismatch == "method":
            method = method.model_copy(update={"id": uuid4()})
        source = await session.get(MessageRow, str(second.task.message_ref.id))
        assert await reuse_question_plan(session, owned, grant, source, method) is None
        assert not (await TaskService(session).get_task(second.task.id)).artifact_refs

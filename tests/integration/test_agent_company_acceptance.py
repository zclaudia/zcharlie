"""AG20 paired-attitude engineering boundary; FunctionModel is not semantic QA."""
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from investment_agent.agent.research import ResearchOutput, research_artifacts
from investment_agent.storage.database import session_scope
from tests.test_agent_conversations import conversation_db as conversation_db
from tests.test_agent_conversations import message, new_conversation, submit
from tests.test_agent_research import full_progress, run
from tests.test_investor_publication import fixture


@pytest.mark.asyncio
async def test_same_saved_material_opposite_attitudes_preserve_fact_scope_and_gap(conversation_db):
    candidate, fact, subject = await fixture()
    plan = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(fact,),
        content=candidate.thesis.claims[0].text, answered=False,
        gaps=("Durability unresolved; user attitude is not company evidence",))
    seen = []
    def respond(messages, info):
        seen.append(str(messages))
        return ModelResponse([TextPart(ResearchOutput(progress=plan).model_dump_json())],
            usage=RequestUsage(input_tokens=10, output_tokens=5))
    outputs = []
    for attitude in ("我很喜欢它", "我很讨厌它"):
        conversation = await new_conversation()
        receipt = await submit(conversation, message(text=attitude + "，请深入研究。",
            context_refs=(subject,), research_kind="company_research"))
        task = await run(receipt, FunctionModel(respond))
        assert task.status == "partial"
        async with session_scope() as session:
            notes = await research_artifacts(session, task.id)
            outputs.append(notes[0]["progress"])
    assert outputs[0] == outputs[1]
    assert "我很喜欢它" in seen[0] and "我很讨厌它" in seen[1]
    assert all("User preferences" in prompt for prompt in seen)
    # Fixture equality demonstrates controller invariants, never model resistance to flattery.

@pytest.mark.asyncio
@pytest.mark.parametrize("request_cap", [1, 8])
async def test_native_wrong_ref_repairs_discovers_captures_real_source_and_keeps_gap(conversation_db, request_cap):
    from datetime import UTC, date, datetime, timedelta

    from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart

    from investment_agent.agent.contracts import MessageInput
    from investment_agent.agent.conversations import ConversationService
    from investment_agent.investor.contracts import NumericNode
    from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
    from tests.test_agent_conversations import base
    candidate, _, subject = await fixture()
    cutoff = datetime.now(UTC)
    async with session_scope() as session:
        from investment_agent.storage.models.reference import Listing, Market
        listing = await session.get(Listing, subject.id)
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        await session.flush()
        listing.market_code = "SSE"
        listing.ticker = "600000"
        await session.flush()
        await FinancialSnapshotRepository(session).insert(listing_id=subject.id,
            report_period=date(2025, 12, 31), period_kind="annual",
            fields={"part": "cash_flow", "operating_cash_flow": 123.45},
            provider="akshare", captured_at=cutoff - timedelta(days=2),
            availability="actual", available_from=date(2026, 1, 2), is_backfill=False)
    conversation = await new_conversation()
    receipt = await submit(conversation, MessageInput(client_message_id=uuid4(),
        text="深入研究", context_refs=(subject,), research_kind="company_research", research_at=cutoff))
    calls = []
    def respond(messages, info):
        calls.append(messages)
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if len(calls) == 1:
            return ModelResponse([ToolCallPart("get_evidence", {"business": {"ref": subject.model_dump(mode="json"), "subject_ref": subject.model_dump(mode="json"), "as_of": cutoff.isoformat()}})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        assert any(isinstance(p, RetryPromptPart) for m in messages for p in m.parts)
        args = {"ticker": "600000.SH", "field": "operating_cash_flow", "as_of": cutoff.isoformat(), "report_period": "2025-12-31"}
        if not any(p.tool_name == "get_snapshot" for p in returns):
            return ModelResponse([ToolCallPart("get_snapshot", {"business": args})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        if not any(p.tool_name == "capture_snapshot" for p in returns):
            return ModelResponse([ToolCallPart("capture_snapshot", {"business": args})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        captured = next(p.content for p in returns if p.tool_name == "capture_snapshot")
        from investment_agent.investor.common import ObjectRef
        fact = ObjectRef.model_validate(captured["refs"][0])
        if not any(p.tool_name == "get_evidence" for p in returns):
            return ModelResponse([ToolCallPart("get_evidence", {"business": {"ref": fact.model_dump(mode="json"), "subject_ref": subject.model_dump(mode="json"), "as_of": cutoff.isoformat()}})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        plan = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(fact,),
            content=(NumericNode(type="numeric_claim", ref=fact.model_copy(update={"field_path": "value"}), role="historical_fact", display_format="decimal"),),
            answered=False, gaps=("Official source and durable cash premise remain unverified",))
        return ModelResponse([TextPart(ResearchOutput(progress=plan).model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=5))
    task = await run(receipt, FunctionModel(respond), base(investor_agent_task_requests=request_cap))
    assert task.usage.tool_calls >= 1  # rejected schema attempt is charged too
    if request_cap == 1:
        assert len(calls) == 1 and task.stop_reason == "budget_exhausted"
        return
    
    assert len(calls) == 5 and task.status == "partial"
    async with session_scope() as session:
        notes = await research_artifacts(session, task.id)
        assert "candidate" in notes[0]["text"] and "123.45" in notes[0]["text"]
        answers = await ConversationService(session, base()).messages(conversation.id)
        assert "remain unverified" in answers[-1].text


@pytest.mark.asyncio
async def test_rejected_numeric_field_is_private_diagnostic_with_safe_public_gap(conversation_db):
    from investment_agent.agent.conversations import ConversationService
    from investment_agent.agent.tasks import TaskService
    from tests.test_agent_conversations import base
    candidate, _, subject = await fixture()
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    plan = full_progress(candidate.thesis.assessed_dimensions, answered=False,
        gaps=("PRIVATE_REJECTED_AMOUNT 999999 元",))
    task = await run(receipt, FunctionModel(lambda messages, info: ModelResponse([TextPart(ResearchOutput(progress=plan).model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=5))))
    async with session_scope() as session:
        envelope, _ = await TaskService(session).get_checkpoint(task.id)
        diagnostic = envelope["data"]["rejected_completion"]
        assert diagnostic["code"] == "research_numeric_text_requires_reference"
        assert diagnostic["field_errors"][0]["field"] == "progress.gaps.0"
        events = await TaskService(session).get_events(task.id)
        public = str([event.model_dump(mode="json") for event in events])
        messages = await ConversationService(session, base()).messages(conversation.id)
        assert "progress.gaps.0" in public and "progress.gaps.0" in messages[-1].text
        assert "PRIVATE_REJECTED_AMOUNT" not in public + messages[-1].text
        assert "下一步" in messages[-1].text and task.status == "failed"


@pytest.mark.asyncio
async def test_last_request_concludes_and_persists_without_an_extra_save_tool_round(conversation_db):
    from investment_agent.agent.research import ResearchOutput
    from tests.test_agent_conversations import base
    candidate, _, subject = await fixture()
    plan = full_progress(candidate.thesis.assessed_dimensions, answered=False, gaps=("Official source unavailable",))
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    calls = []
    def respond(messages, info):
        calls.append(messages)
        assert not info.function_tools
        final = plan.model_copy(update={"stopped_because": "Only bounded synthesis remains; original evidence is unavailable"})
        return ModelResponse([TextPart(ResearchOutput(progress=final).model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=5))
    task = await run(receipt, FunctionModel(respond), base(investor_agent_task_requests=1))
    assert len(calls) == 1 and task.status == "partial" and task.stop_reason == "evidence_blocked"
    async with session_scope() as session:
        notes = await research_artifacts(session, task.id)
        assert notes[0]["progress"]["gaps"] == ["Official source unavailable"]
    assert any(ref.kind == "research_progress" for ref in task.artifact_refs)


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [False, True])
async def test_native_sdk_instruction_estimate_matches_priority_and_keeps_fallback(structured):
    import httpx
    from pydantic_ai.messages import (
        InstructionPart,
        ModelRequest,
        UserPromptPart,
    )
    from pydantic_ai.models import ModelRequestParameters

    from investment_agent.agent.model import AgentModelConfig, HTTPTrace, create_model
    from investment_agent.agent.runtime import _BudgetedModel
    from tests.test_agent_conversations import base
    captured, estimates, wire_sizes = [], [], []
    async def respond(request):
        import json
        captured.append(json.loads(request.content))
        wire_sizes.append(len(request.content))
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0,
            "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    class Lifecycle:
        async def reserve(self, estimate):
            estimates.append(estimate)
            return uuid4()
        async def sent(self, reservation): pass
        async def settle(self, reservation, usage, *, sent): pass
        async def observe_tools(self, count): pass
    config = AgentModelConfig.from_settings(base(agent_base_url="https://fixture.invalid/v1", agent_model="fixture", agent_api_key="inert"), 0)
    trace = HTTPTrace()
    model, client = create_model(config, trace, transport=httpx.MockTransport(respond))
    guarded = _BudgetedModel(model, Lifecycle(), trace, False)
    trace.before_request = guarded.before_send
    trace.reserve_request = guarded.reserve_wire
    messages = [ModelRequest(parts=[UserPromptPart("bounded")], instructions="legacy marker " * 20)]
    params = ModelRequestParameters(instruction_parts=[InstructionPart("current marker " * 20)] if structured else None)
    try:
        await guarded.request(messages, None, params)
    finally:
        await client.close()
    assert estimates == wire_sizes
    text = str(captured[0]["messages"])
    assert ("current marker" in text) == structured
    assert ("legacy marker" in text) != structured


@pytest.mark.asyncio
async def test_native_tool_validation_preserves_safe_field_and_private_action(conversation_db):
    from pydantic_ai.messages import ToolCallPart
    from sqlalchemy import select

    from investment_agent.agent.conversations import ConversationService
    from investment_agent.agent.tasks import TaskService
    from investment_agent.storage.models.agent import AgentModelRun, ToolAction
    from tests.test_agent_conversations import base
    candidate, _, subject = await fixture()
    plan = full_progress(candidate.thesis.assessed_dimensions, answered=False, question="PRIVATE_UNVALIDATED 999999", gaps=("Official source unavailable",))
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    task = await run(receipt, FunctionModel(lambda messages, info: ModelResponse([ToolCallPart("save_research_progress", {"business": plan.model_dump(mode="json")})], usage=RequestUsage(input_tokens=10, output_tokens=5))))
    assert task.status == "failed" and task.stop_reason == "evidence_blocked"
    assert task.usage.requests == 2 and task.usage.tool_calls == 2  # one bounded native repair
    async with session_scope() as session:
        envelope, _ = await TaskService(session).get_checkpoint(task.id)
        assert envelope["data"]["rejected_tool_validation"]["field_errors"][0]["field"] == "progress.question"
        audit = await session.scalar(select(AgentModelRun).where(AgentModelRun.task_id == str(task.id)))
        assert audit.error_code == "research_numeric_text_requires_reference"
        actions = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id)))).all()
        assert len(actions) == 1 and actions[0].state == "prepared"
        assert "PRIVATE_UNVALIDATED" in actions[0].parameters["business"]["question"]
        messages = await ConversationService(session, base()).messages(conversation.id)
        events = await TaskService(session).get_events(task.id)
        public = messages[-1].text + str([event.model_dump(mode="json") for event in events])
        assert "progress.question" in public and "progress.content" in public
        assert "仅写无量化描述" in public
        assert "PRIVATE_UNVALIDATED" not in public and "999999" not in public
        assert not await research_artifacts(session, task.id)

    from pydantic_ai.messages import ToolReturnPart
    valid = plan.model_copy(update={"question": "现金流持续性是否获得官方材料支持？"})
    another = await new_conversation()
    receipt = await submit(another, message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    def valid_response(messages, info):
        if not any(isinstance(part, ToolReturnPart) for msg in messages for part in msg.parts):
            return ModelResponse([ToolCallPart("save_research_progress", {"business": valid.model_dump(mode="json")})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        return ModelResponse([TextPart(ResearchOutput(progress=valid).model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=5))
    accepted = await run(receipt, FunctionModel(valid_response))
    assert accepted.status == "partial" and accepted.stop_reason == "evidence_blocked"
    async with session_scope() as session:
        assert (await research_artifacts(session, accepted.id))[0]["progress"]["question"] == valid.question

@pytest.mark.asyncio
@pytest.mark.parametrize("historical,wrong_scope", [(False,False), (True,False), (False,True)])
async def test_default_native_worker_authorized_identity_reads_exact_source(conversation_db, monkeypatch, historical, wrong_scope):
    import json
    from datetime import UTC, date, datetime, timedelta

    import httpx

    from investment_agent.agent import runtime
    from investment_agent.agent.contracts import MessageInput
    from investment_agent.agent.tasks import TaskService
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.storage.models.reference import Company, Listing, Market
    from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
    from tests.test_agent_conversations import base

    candidate, _, subject = await fixture()
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    async with session_scope() as session:
        listing = await session.get(Listing, subject.id)
        company = await session.get(Company, listing.company_id)
        company.canonical_name = "同名公司"
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        await session.flush()
        listing.market_code, listing.ticker = "SSE", "600000"
        peer_company = Company(canonical_name="同名公司")
        session.add(peer_company)
        await session.flush()
        session.add(Listing(company_id=peer_company.id, market_code="SSE", ticker="600001"))
        for when,value in [(cutoff-timedelta(days=2),123.45),(cutoff+timedelta(minutes=30),9999)]:
            await FinancialSnapshotRepository(session).insert(listing_id=subject.id, report_period=date(2025,12,31), period_kind="annual", fields={"part":"cash_flow","operating_cash_flow":value}, provider="akshare", captured_at=when, availability="actual", available_from=date(2026,1,2), is_backfill=False)
    conversation = await new_conversation()
    receipt = await submit(conversation, MessageInput(client_message_id=uuid4(), text="深入研究这家公司", context_refs=(candidate.thesis.case_ref,), research_kind="company_research", research_at=cutoff if historical else None))
    seen=[]
    async def gateway(request):
        body=json.loads(request.content)
        seen.append(body)
        if len(seen)==1:
            instructions=next(m["content"] for m in body["messages"] if m["role"]=="system")
            context=json.JSONDecoder().raw_decode(instructions.split("Trusted current context:\n",1)[1])[0]
            assert context["target_identities"] == [{"listing_ref":subject.model_dump(mode="json"), "status":"available", "canonical_name":"同名公司", "canonical_ticker":"600000.SH"}]
            assert "not financial evidence" in context["identity_metadata_policy"]
            if historical:
                assert context["company_context"] == []
                assert context["observation_cutoff"] == cutoff.isoformat()
            ticker="600001.SH" if wrong_scope else context["target_identities"][0]["canonical_ticker"]
            msg={"role":"assistant","content":None,"tool_calls":[{"id":"identity-read","type":"function","function":{"name":"get_snapshot","arguments":json.dumps({"business":{"ticker":ticker,"field":"operating_cash_flow","as_of":context["observation_cutoff"],"report_period":"2025-12-31"}})}}]}
        else:
            returned=next(m["content"] for m in body["messages"] if m["role"]=="tool")
            assert "123.45" in returned if historical else "9999" in returned
            plan=full_progress(candidate.thesis.assessed_dimensions, answered=False, gaps=("官方核验材料仍不足",))
            msg={"role":"assistant","content":ResearchOutput(progress=plan).model_dump_json()}
        return httpx.Response(200,json={"id":"identity-fixture","object":"chat.completion","created":1,"model":"fixture-model","choices":[{"index":0,"message":msg,"finish_reason":"stop"}],"usage":{"prompt_tokens":15,"completion_tokens":7,"total_tokens":22}})
    original=runtime.create_model
    monkeypatch.setattr(runtime,"create_model",lambda config,trace,**kwargs:original(config,trace,transport=httpx.MockTransport(gateway)))
    await ResearchWorker(settings=base(),worker_id="default-identity",kinds={"agent_turn"}).run_once()
    async with session_scope() as session:
        task=await TaskService(session).get_task(receipt.task.id)
        assert task.status == ("failed" if wrong_scope else "partial")
    assert len(seen)==(1 if wrong_scope else 2)

@pytest.mark.asyncio
async def test_unavailable_authorized_identity_does_not_guess(conversation_db):
    from investment_agent.agent.research import authorized_identities
    from investment_agent.investor.common import ObjectRef
    ref=ObjectRef(kind="listing",id=987654,revision=1)
    async with session_scope() as session:
        assert await authorized_identities(session,(ref,)) == [{"listing_ref":ref.model_dump(mode="json"),"status":"unavailable"}]

"""AG13 native Agent, trusted completion, persisted research and collector PIT."""
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from investment_agent.agent.conversations import ConversationService
from investment_agent.agent.execution import ExecutionDeniedError, NoProgressError
from investment_agent.agent.executor import AgentExecutor
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.observation import effective_cutoff, resume_collectors
from investment_agent.agent.research import (
    ResearchOutput,
    ResearchPolicy,
    check_progress_round,
    registrations,
    research_artifacts,
    validate_progress,
)
from investment_agent.agent.tasks import TaskService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import TextNode
from investment_agent.investor.enums import StopReason, TaskStatus
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask, ExecutionGrantRow, ToolAction
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.publication import PublicationEvent
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.support.conversations import (
    base,
    claim,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.support.publication import fixture, publish
from tests.support.research import full_progress, progress, response, run


@pytest.mark.asyncio
async def test_explanation_default_policy_native_answer_and_query(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    task = await run(receipt, FunctionModel(lambda messages, info: response(ResearchOutput(progress=progress()))))
    assert task.status == TaskStatus.SUCCEEDED and task.stop_reason == StopReason.ANSWERED
    async with session_scope() as session:
        notes = await research_artifacts(session, task.id)
        messages = await ConversationService(session, base()).messages(conversation.id)
        assert notes[0]["progress"]["kind"] == "explanation"
        assert messages[-1].text.startswith("The available material")
        assert messages[-1].artifact_refs == task.artifact_refs
        envelope, _ = await TaskService(session).get_checkpoint(task.id)
        assert envelope["data"]["context_stamp"]["revision"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["budget", "repeat", "no_progress"])
async def test_real_saved_progress_survives_native_stop(conversation_db, failure):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    saved = progress(answered=False, completed_steps=("Clarified the meaning",), gaps=("Company materials unavailable",))
    turns = 0
    def model(messages, info):
        nonlocal turns
        turns += 1
        if turns == 1 or failure == "repeat":
            return ModelResponse([ToolCallPart("save_research_progress", {"business": saved.model_dump(mode="json")}, tool_call_id=f"progress-{turns}")], usage=RequestUsage(input_tokens=10, output_tokens=5))
        # Structured output validation retries without new material.
        return ModelResponse([TextPart("invalid structured output")], usage=RequestUsage(input_tokens=10, output_tokens=5))
    selected = base(investor_agent_task_requests=1) if failure == "budget" else base(agent_retries=5)
    task = await run(receipt, FunctionModel(model), selected)
    assert task.status == TaskStatus.PARTIAL
    assert task.stop_reason == (StopReason.BUDGET_EXHAUSTED if failure == "budget" else StopReason.NO_PROGRESS)
    async with session_scope() as session:
        notes = await research_artifacts(session, task.id)
        assert len(notes) == 1 and notes[0]["progress"]["completed_steps"] == ["Clarified the meaning"]


@pytest.mark.asyncio
async def test_local_never_replaces_global_and_critical_counter_restricts(conversation_db):
    candidate, evidence, subject = await fixture()
    published = await publish(candidate)
    assert published.published
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="Check cash locally", context_refs=(subject,)))
    output = ResearchOutput(progress=progress(kind="local_check", fact_refs=(evidence,), counter_refs=(evidence,)), critical_counter_evidence=(evidence,))
    task = await run(receipt, FunctionModel(lambda messages, info: response(output)))
    assert task.status == TaskStatus.SUCCEEDED
    async with session_scope() as session:
        from investment_agent.investor.publication import PublicationService
        current = await PublicationService(session).foundation.get_case("local_owner", candidate.thesis.case_ref.id)
        assert current.current_recommendation_ref == published.recommendation_ref
        view = await PublicationService(session).get_recommendation(published.recommendation_ref)
        assert view.status.value == "review_required"
        events = (await session.scalars(select(PublicationEvent))).all()
        assert len([e for e in events if e.payload["type"] == "published"]) == 1
        assert "formal_review_required" in str(view)


@pytest.mark.asyncio
async def test_native_counter_reference_mismatch_gets_repair_before_completion(conversation_db):
    from pydantic_ai.messages import RetryPromptPart
    _, evidence, subject = await fixture()
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="Check cash locally", context_refs=(subject,)))
    rounds = []
    def model(messages, info):
        rounds.append(messages)
        if len(rounds) > 1:
            assert any(isinstance(p, RetryPromptPart) and "research_counter_references_mismatch" in str(p.content) for m in messages for p in m.parts)
        return response(ResearchOutput(progress=progress(kind="local_check", fact_refs=(evidence,),
            counter_refs=(evidence,) if len(rounds) > 1 else ()), critical_counter_evidence=(evidence,)))
    task = await run(receipt, FunctionModel(model))
    assert len(rounds) == 2 and task.status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_pending_direction_gets_reprepared_as_explicitly_undetermined_study(conversation_db):
    from pydantic_ai.messages import RetryPromptPart
    from sqlalchemy import update

    from investment_agent.agent.research_publication import CompanyViewInput, SemanticInput
    from investment_agent.investor.publication import PublicationCandidate, SemanticAssessment
    from investment_agent.storage.models.evidence import SavedEvidence
    candidate, evidence, subject = await fixture()
    async with session_scope() as session:
        row = await session.get(SavedEvidence, (str(evidence.id), evidence.revision))
        await session.execute(update(SavedEvidence).where(SavedEvidence.id == str(evidence.id)).values(payload={**row.payload, "verification": "candidate"}))
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="深入研究", context_refs=(subject,)))
    plan = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(evidence,))
    source_claim = candidate.thesis.claims[0]
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "save_research_progress" for p in returns):
            return ModelResponse([ToolCallPart("save_research_progress", {"business": plan.model_dump(mode="json")})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        preparations = [p.content for p in returns if p.tool_name == "prepare_company_view"]
        repairs = [p for m in messages for p in m.parts if isinstance(p, RetryPromptPart) and "research_publication_pending_premises" in str(p.content)]
        revised = bool(repairs)
        if revised:
            assert "claims.0" in str(repairs[-1].content)
        if not preparations or (revised and preparations[-1]["candidate"]["recommendation"]["business_view"] != "undetermined"):
            claim = source_claim.model_copy(update={"materiality": "context", "text": (TextNode(type="text", text="Saved cash is pending verification and supports no business or price direction."),)}) if revised else source_claim
            business = CompanyViewInput(case_ref=candidate.thesis.case_ref, question_scope="Company study with explicit verification gaps",
                change_reason="Candidate sources cannot support direction", assessed_dimensions=plan.dimensions,
                claims=(claim,), counter_case=candidate.thesis.counter_case, gaps=("Source verification and durable economics remain unresolved",),
                dependencies=() if revised else candidate.recommendation.dependency_refs,
                business_view="undetermined" if revised else "favorable", price_view="undetermined", sufficiency="bounded")
            return ModelResponse([ToolCallPart("prepare_company_view", {"business": business.model_dump(mode="json")})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        prepared = preparations[-1]
        exact = PublicationCandidate.model_validate(prepared["candidate"])
        review = SemanticInput(preparation_ref=ObjectRef.model_validate(prepared["preparation_ref"]),
            assessments=tuple(SemanticAssessment(claim_id=c.id, fact_relation="The source is pending; it supports only a transparent research gap",
                counter_evidence_relevance="Temporary cash remains an untested alternative", materiality_reason="No business or price direction is asserted") for c in exact.thesis.claims),
            checked_sources=(evidence,), unresolved_questions=(), conclusion="pass")
        final = plan.model_copy(update={"content": (TextNode(type="text", text="The company warrants further research; candidate cash material does not establish a business or price direction."),)})
        return response(ResearchOutput(progress=final, semantic_review=review))
    task = await run(receipt, FunctionModel(model))
    async with session_scope() as session:
        prepared = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id)))).all()
        errors = [e.payload for e in await TaskService(session).get_events(task.id) if e.type == "error"]
        assert task.status == TaskStatus.SUCCEEDED, (task.stop_reason, errors, [(a.intent_key, a.result) for a in prepared if a.intent_key == "completion:publish"])
        assert len([a for a in prepared if a.parameters.get("tool") == "prepare_company_view" and a.state == "committed"]) == 1
        assert task.usage.tool_calls == 3  # saved plan, rejected preparation, revised preparation
        row = await session.get(SavedEvidence, (str(evidence.id), evidence.revision))
        assert row.payload["verification"] == "candidate"


@pytest.mark.asyncio
async def test_native_publication_preflight_repairs_review_before_any_formal_write(conversation_db):
    from pydantic_ai.messages import RetryPromptPart

    from investment_agent.agent.research_publication import CompanyViewInput, SemanticInput
    from investment_agent.investor.publication import PublicationCandidate, SemanticAssessment
    candidate, evidence, subject = await fixture()
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="深入研究", context_refs=(subject,)))
    plan = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(evidence,))
    business = CompanyViewInput(case_ref=candidate.thesis.case_ref, question_scope="Company research with historical cash only",
        change_reason="Initial bounded study", assessed_dimensions=plan.dimensions, claims=candidate.thesis.claims,
        counter_case=candidate.thesis.counter_case, gaps=candidate.recommendation.gaps,
        dependencies=candidate.recommendation.dependency_refs, business_view="undetermined", price_view="undetermined", sufficiency="bounded")
    corrected = []
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "save_research_progress" for p in returns):
            return ModelResponse([ToolCallPart("save_research_progress", {"business": plan.model_dump(mode="json")})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        if not any(p.tool_name == "prepare_company_view" for p in returns):
            return ModelResponse([ToolCallPart("prepare_company_view", {"business": business.model_dump(mode="json")})], usage=RequestUsage(input_tokens=10, output_tokens=5))
        repaired = any(isinstance(p, RetryPromptPart) and "research_publication_requires_revision" in str(p.content) for m in messages for p in m.parts)
        corrected.append(repaired)
        prepared = next(p.content for p in returns if p.tool_name == "prepare_company_view")
        exact = PublicationCandidate.model_validate(prepared["candidate"])
        review = SemanticInput(preparation_ref=ObjectRef.model_validate(prepared["preparation_ref"]),
            assessments=tuple(SemanticAssessment(claim_id=c.id, fact_relation="Only saved historical cash is established; no durability conclusion is asserted",
                counter_evidence_relevance="Temporary working capital remains an untested alternative",
                materiality_reason="Historical cash alone cannot establish durable returns",
                issues=() if repaired else ("Durability was not established",)) for c in exact.thesis.claims),
            checked_sources=(evidence,), unresolved_questions=(), conclusion="pass")
        return response(ResearchOutput(progress=plan, semantic_review=review))
    task = await run(receipt, FunctionModel(model))
    assert corrected == [False, True] and task.status == TaskStatus.SUCCEEDED
    assert not any(r.kind == "draft" for r in task.artifact_refs)


@pytest.mark.asyncio
async def test_numeric_text_and_foreign_refs_rejected(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="numeric_text"):
            await validate_progress(session, receipt.task.id, progress(content=(TextNode(type="text", text="Cash is 123 million"),)))
        with pytest.raises(ExecutionDeniedError, match="reference_unavailable"):
            await validate_progress(session, receipt.task.id, progress(fact_refs=(ObjectRef(kind="evidence", id=uuid4(), revision=1),)))


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["evidence", "calculation"])
async def test_native_numeric_value_path_gets_bounded_repair(conversation_db, reference_kind):
    from pydantic_ai.messages import RetryPromptPart

    from investment_agent.investor.contracts import NumericNode

    _, evidence, subject = await fixture()
    source = evidence
    field_path, role, display, rendered = "value", "historical_fact", "decimal", "1234500000"
    if reference_kind == "calculation":
        from investment_agent.evidence.contracts import CalculationRecord
        from investment_agent.investor.common import Quantity
        from investment_agent.storage.models.evidence import SavedCalculation

        record = CalculationRecord(id=uuid4(), schema_version=1, revision=1, actor="fixture", origin_ref=None,
            created_at=datetime.now(UTC), method="fixture_percentile", method_version="fixture",
            fact_refs=(evidence,), assumption_refs=(), input_hash="fixture", as_of=datetime.now(UTC),
            applicable=True, status="ok", results={"percentile": Quantity(value="0.25", unit="fraction")})
        async with session_scope() as session:
            session.add(SavedCalculation(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
        source = ObjectRef(kind="calculation", id=record.id, revision=1)
        field_path, role, display, rendered = "results.percentile", "calculation_output", "percent", "25.00%"
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="Check saved cash", context_refs=(subject,), research_kind="local_check"))
    rounds = 0

    def model(messages, info):
        nonlocal rounds
        rounds += 1
        if rounds == 2:
            retries = [p for m in messages for p in m.parts if isinstance(p, RetryPromptPart)]
            assert retries and field_path in str(retries[-1].content)
        ref = (source.model_copy(update={"field_path": "result.percentile"}) if reference_kind == "calculation" else source) if rounds == 1 else source.model_copy(update={"field_path": field_path})
        output = progress(kind="local_check", fact_refs=(source,), content=(
            NumericNode(type="numeric_claim", ref=ref, role=role, display_format=display),))
        return response(ResearchOutput(progress=output))

    task = await run(receipt, FunctionModel(model), base(agent_retries=1))
    assert rounds == 2 and task.status == TaskStatus.SUCCEEDED
    async with session_scope() as session:
        answers = await ConversationService(session, base()).messages(conversation.id)
        assert answers[-1].role == "assistant" and rendered in answers[-1].text


@pytest.mark.asyncio
async def test_no_progress_ignores_changed_metadata(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    trusted = await claim(receipt.task)
    async with session_scope() as session:
        await check_progress_round(session, trusted)
        service = TaskService(session)
        for index in range(2):
            action = await service.prepare_action(trusted, intent_key=f"metadata:{index}", effect="database", parameters={"tool": "get_snapshot", "as_of": str(index)})
            async def write(s, op):
                return {"status": "ok", "as_of": datetime.now(UTC).isoformat(), "operation": str(op), "data": []}
            await service.settle_action(trusted, UUID(action.operation_id), write)
            if index == 0:
                await check_progress_round(session, trusted)
            else:
                with pytest.raises(NoProgressError):
                    await check_progress_round(session, trusted)


@pytest.mark.asyncio
async def test_progress_guard_round_persists_through_cas_checkpoint(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    trusted = await claim(receipt.task)
    async with session_scope() as session:
        before = (await TaskService(session).get_task(receipt.task.id)).revision
        await check_progress_round(session, trusted)
    async with session_scope() as session:
        service = TaskService(session)
        assert (await service.get_task(receipt.task.id)).revision > before  # CAS revision moved
        envelope, _ = await service.get_checkpoint(receipt.task.id)
        assert envelope["data"]["progress_guard"]["stagnant"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
async def test_real_collector_deferred_resumption_preserves_pit_and_budget(conversation_db, tmp_path, historical):
    from investment_agent.providers.baostock_market import BaostockMarketClient
    from investment_agent.snapshots.breadth import collect_baostock_market
    from investment_agent.snapshots.collect import ArtifactWriter
    from investment_agent.snapshots.jobs import MarketSnapshotPayload
    from tests.unit._baostock_fake import FakeBaostockData
    candidate, _, subject = await fixture()
    async with session_scope() as session:
        from investment_agent.storage.models.reference import Listing, Market
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        await session.flush()
        listing = await session.get(Listing, subject.id)
        listing.ticker = "600519"
        listing.market_code = "SSE"
        session.add(WatchlistItem(listing_id=subject.id, enabled=True, added_at=datetime.now(UTC)))
    conversation = await new_conversation()
    cutoff = datetime.now(UTC)
    receipt = await submit(conversation, message(text="Check fresh close", context_refs=(subject,), research_at=cutoff if historical else None))
    first_turns = 0
    def first(messages, info):
        nonlocal first_turns
        first_turns += 1
        return ModelResponse([ToolCallPart("request_data_refresh", {"business": {"ticker": "600519.SH", "as_of": "2026-09-30", "report_period": None}}, tool_call_id="refresh")], usage=RequestUsage(input_tokens=10, output_tokens=5))
    task = await run(receipt, FunctionModel(first))
    assert task.status == TaskStatus.WAITING_DATA and first_turns == 1
    async with session_scope() as session:
        action = await session.scalar(select(ToolAction).where(ToolAction.task_id == str(task.id), ToolAction.parameters["tool"].as_string() == "request_data_refresh"))
        run_id = action.result["data"]["id"]
        run_row = await PipelineRunRepository(session).claim_next(worker_id="collector", run_id=run_id)
        lease = RunLease(run_id, "collector", run_row.attempt_count)
        payload = MarketSnapshotPayload.model_validate(run_row.request_json)
    acquired = datetime.now(UTC)
    report = await collect_baostock_market(payload,
        client=BaostockMarketClient(FakeBaostockData(), clock=lambda: acquired, retry_backoff_seconds=0),
        artifacts=ArtifactWriter(tmp_path, run_id=run_id, attempt=lease.attempt), lease=lease, now=lambda: acquired)
    assert report.status == "success"
    async with session_scope() as session:
        await PipelineRunRepository(session).mark_success(run_id, lease=lease, metadata=report.to_metadata())
        assert await resume_collectors(session) == 1
        assert await resume_collectors(session) == 0
        observed = await effective_cutoff(session, task.id)
        if historical:
            assert observed == task.binding.as_of
        else:
            assert observed > acquired > task.binding.as_of
        snapshots = (await session.scalars(select(MarketSnapshot))).all()
        assert snapshots and all(r.captured_at.replace(tzinfo=UTC) == acquired for r in snapshots)
    def resumed(messages, info):
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "capture_snapshot" for p in returned):
            deferred = next(p for p in returned if p.tool_name == "request_data_refresh")
            return ModelResponse([ToolCallPart("capture_snapshot", {"business": {"ticker": "600519.SH", "field": "close", "as_of": deferred.content["as_of"], "report_period": None}}, tool_call_id="capture")], usage=RequestUsage(input_tokens=10, output_tokens=5))
        captured = next(p.content for p in returned if p.tool_name == "capture_snapshot")
        refs = tuple(ObjectRef.model_validate(r) for r in captured["refs"])
        return response(ResearchOutput(progress=progress(kind="local_check", fact_refs=refs, answered=bool(refs), stopped_because=None if refs else "Historical cutoff cannot see later captures")))
    task = await run(receipt, FunctionModel(resumed))
    assert task.status == (TaskStatus.PARTIAL if historical else TaskStatus.SUCCEEDED)
    assert task.binding == receipt.task.binding
    async with session_scope() as session:
        from investment_agent.storage.models.agent import BudgetEntry
        entries = (await session.scalars(select(BudgetEntry))).all()
        assert len([r for r in entries if r.kind == "model"]) == 3 and {r.task_id for r in entries} == {str(task.id)}
        if not historical:
            from investment_agent.storage.models.evidence import SavedEvidence
            saved = (await session.scalars(select(SavedEvidence))).all()
            fresh = [r for r in saved if r.payload["source_ref"]["kind"] == "market_snapshot"]
            assert fresh and all(r.payload["acquired_at"] == acquired.isoformat().replace("+00:00", "Z") for r in fresh)


@pytest.mark.asyncio
@pytest.mark.parametrize("semantic_pass", [True, False])
@pytest.mark.parametrize("finish_only", [False, True])
@pytest.mark.parametrize("repair", [False, True])
async def test_native_full_preparation_and_exact_semantic_publication(conversation_db, semantic_pass, finish_only, repair):
    from sqlalchemy import delete

    from investment_agent.agent.research_publication import (
        CompanyViewInput,
        HypothesisInput,
        MonitorInput,
        SemanticInput,
    )
    from investment_agent.investor.publication import (
        PublicationCandidate,
        PublicationService,
        SemanticAssessment,
    )
    from investment_agent.storage.models.investor import MandateHead, MandateRevision
    candidate, evidence, subject = await fixture()
    # Real native plan -> prepare -> semantic review -> atomic publication must
    # work without creating a placeholder personal investment mandate.
    async with session_scope() as session:
        await session.execute(delete(MandateHead))
        await session.execute(delete(MandateRevision))
    selected = base(investor_agent_task_requests=4 if repair else 3) if finish_only else base()
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="深入研究它", context_refs=(subject,)), settings=selected)
    plan = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(evidence,))
    business = CompanyViewInput(case_ref=candidate.thesis.case_ref, question_scope="Bounded whole company research", change_reason="Initial research",
        assessed_dimensions=candidate.thesis.assessed_dimensions, claims=candidate.thesis.claims, counter_case=candidate.thesis.counter_case,
        gaps=candidate.recommendation.gaps, dependencies=candidate.recommendation.dependency_refs,
        business_view="undetermined", price_view="undetermined", sufficiency="bounded",
        hypotheses=(HypothesisInput(key="cash", question="Is cash durable?", scope="Operating cash", support_condition="Cash follows operating demand", falsification_condition="Cash reverses without operating growth"),),
        condition_hypothesis_keys=("cash",), monitors=(MonitorInput(hypothesis_key="cash",trigger="calendar"),))
    invalid_sent = False
    def model(messages, info):
        nonlocal invalid_sent
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        names = {tool.name for tool in info.function_tools}
        if not any(p.tool_name == "save_research_progress" for p in returns):
            assert "prepare_company_view" not in names
            return ModelResponse([ToolCallPart("save_research_progress", {"business": plan.model_dump(mode="json")}, tool_call_id="plan")], usage=RequestUsage(input_tokens=10, output_tokens=5))
        assert "prepare_company_view" in names
        if finish_only and (not repair or invalid_sent):
            assert names == {"prepare_company_view"}
        if repair and not invalid_sent:
            invalid_sent = True
            malformed = business.model_dump(mode="json")
            del malformed["question_scope"]
            return ModelResponse([ToolCallPart("prepare_company_view", {"business": malformed}, tool_call_id="invalid-prepare")], usage=RequestUsage(input_tokens=10, output_tokens=5))
        if not any(p.tool_name == "prepare_company_view" for p in returns):
            return ModelResponse([ToolCallPart("prepare_company_view", {"business": business.model_dump(mode="json")}, tool_call_id="prepare")], usage=RequestUsage(input_tokens=10, output_tokens=5))
        prepared = next(p.content for p in returns if p.tool_name == "prepare_company_view")
        exact = PublicationCandidate.model_validate(prepared["candidate"])
        assert exact.recommendation.mandate_revision_id is None
        review = SemanticInput(preparation_ref=ObjectRef.model_validate(prepared["preparation_ref"]),
            assessments=tuple(SemanticAssessment(claim_id=c.id, fact_relation="The exact saved cash supports a bounded observation", counter_evidence_relevance="Temporary working capital remains an alternative", materiality_reason="Cash persistence affects the observation", issues=() if semantic_pass else ("Durability is unresolved",)) for c in exact.thesis.claims),
            checked_sources=(evidence,), unresolved_questions=(), conclusion="pass" if semantic_pass else "revise")
        return response(ResearchOutput(progress=plan, semantic_review=review))
    task = await run(receipt, FunctionModel(model), selected)
    assert task.status == (TaskStatus.SUCCEEDED if semantic_pass else TaskStatus.PARTIAL)
    async with session_scope() as session:
        if repair:
            from investment_agent.storage.models.agent import AgentModelRun
            audit = await session.scalar(select(AgentModelRun).where(AgentModelRun.task_id == str(task.id)))
            assert audit.config_snapshot["validation_repairs"][0]["errors"] == [{"type": "missing", "loc": ["question_scope"]}]
            assert task.usage.requests == 4
        notes = await research_artifacts(session, task.id)
        assert notes and notes[0]["progress"]["hypotheses"][0]["falsification_condition"]
        current = await PublicationService(session).foundation.get_case("local_owner", candidate.thesis.case_ref.id)
        assert bool(current.current_recommendation_ref) == semantic_pass
        if semantic_pass:
            view = await PublicationService(session).get_recommendation(current.current_recommendation_ref)
            assert view.recommendation.mandate_revision_id is None
            assert view.recommendation.assumptions[0].falsification_condition == "Cash reverses without operating growth"
            monitor = await PublicationService(session).artifact(view.recommendation.monitor_refs[0])
            assert monitor.payload["trigger"] == "calendar" and monitor.payload["conditions"] == []
            assert monitor.payload["hypothesis_ref"]["id"] == str(current.current_thesis_ref.id)
            assert view.recommendation.conditions[0].id == current.current_thesis_ref.id
        action = await session.scalar(select(ToolAction).where(ToolAction.task_id == str(task.id), ToolAction.intent_key == "completion:publish"))
        assert action.parameters["semantic"]["candidate_hash"] != "fixture-material"
        assert action.parameters["candidate"]["thesis"]["actor"] == "research_service"
        assert action.parameters["candidate"]["thesis"]["claims"][0]["id"] != str(candidate.thesis.claims[0].id)
        assert any(r.kind == ("recommendation" if semantic_pass else "draft") for r in task.artifact_refs)


@pytest.mark.asyncio
async def test_clear_user_commands_execute_once_without_model_or_second_approval(conversation_db):
    candidate, _, subject = await fixture()
    conversation = await new_conversation()
    body = message(text="暂停这家公司的后台研究", context_refs=(subject,))
    receipt = await submit(conversation, body)
    assert receipt.task.status == TaskStatus.SUCCEEDED
    repeated = await submit(conversation, body)
    assert repeated.task.id == receipt.task.id
    async with session_scope() as session:
        from investment_agent.investor.foundations import InvestorService
        case = await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)
        assert case.background_paused
        assert len(await ConversationService(session, base()).messages(conversation.id)) == 2
    remember = await submit(conversation, message(text="记住我的偏好：希望解释现金流来源"))
    assert remember.task.status == TaskStatus.SUCCEEDED
    async with session_scope() as session:
        from investment_agent.investor.controls import UserControlService
        context = await UserControlService(session).context(conversation_id=conversation.id)
        assert context.revision == 1
        assert context.memories[0].text == "希望解释现金流来源"
        assert "偏好" not in str(context.memories[0].scope_refs)


@pytest.mark.asyncio
async def test_model_operation_is_proposal_not_user_authority(conversation_db):
    from investment_agent.investor.controls import CaseAction, UserControlService
    candidate, _, subject = await fixture()
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="Should we pause?", context_refs=(subject,)))
    output = ResearchOutput(progress=progress(kind="local_check"), proposal=CaseAction(case_ref=candidate.thesis.case_ref, action="pause", reason="Suggested pause"))
    task = await run(receipt, FunctionModel(lambda messages, info: response(output)))
    async with session_scope() as session:
        from investment_agent.investor.foundations import InvestorService
        case = await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)
        assert not case.background_paused
        proposal_ref = next(r for r in task.artifact_refs if r.kind == "proposal")
        proposal = await UserControlService(session).get_proposal(proposal_ref.id)
        assert proposal.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [False, True])
async def test_historical_prompt_omits_future_answers_and_company_heads(conversation_db, historical):
    candidate, evidence, subject = await fixture()
    conversation = await new_conversation()
    cutoff = datetime.now(UTC)
    past = await submit(conversation, message(text="Explain prior context", context_refs=(subject,)))
    output = ResearchOutput(progress=progress(content=(TextNode(type="text", text="FUTURE_ASSISTANT_MARKER"),)))
    await run(past, FunctionModel(lambda messages, info: response(output)))
    published = await publish(candidate)
    assert published.published
    receipt = await submit(conversation, message(text="Explain historical view", context_refs=(subject,), research_at=cutoff if historical else None))
    def model(messages, info):
        instructions = info.instructions
        context = json.JSONDecoder().raw_decode(instructions.split("Trusted current context:\n")[1])[0]
        assert ("FUTURE_ASSISTANT_MARKER" in instructions) != historical
        assert bool(context["company_context"]) != historical
        if historical:
            assert str(published.recommendation_ref.id) not in instructions
            assert "historical_context_limit" in context
        return response(ResearchOutput(progress=progress()))
    task = await run(receipt, FunctionModel(model))
    assert task.status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_memory_revocation_blocks_old_task_and_rebuild_filters_source(conversation_db):
    from investment_agent.investor.controls import (
        ExecuteRequest,
        RevokeAction,
        UserControlService,
        authenticated_request,
    )
    conversation = await new_conversation()
    await submit(conversation, message(text="记住我的偏好：REVOKED_PREFERENCE_MARKER"))
    old = await submit(conversation)
    async with session_scope() as session:
        controls = UserControlService(session)
        memory = (await controls.get_memories(active_only=True))[0]
        action = RevokeAction(memory_ref=ObjectRef(kind="memory", id=memory.id, revision=memory.revision), reason="Forget preference")
        await controls.execute(ExecuteRequest(parameters=action, idempotency_key=uuid4()), authenticated_request(action))
    invoked = False
    def old_model(messages, info):
        nonlocal invoked
        invoked = True
        return response(ResearchOutput(progress=progress()))
    stopped = await run(old, FunctionModel(old_model))
    assert stopped.status == TaskStatus.FAILED and not invoked
    fresh = await submit(conversation)
    def fresh_model(messages, info):
        assert "REVOKED_PREFERENCE_MARKER" not in info.instructions
        return response(ResearchOutput(progress=progress()))
    task = await run(fresh, FunctionModel(fresh_model))
    assert task.status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_comparison_requires_same_period_and_basis(conversation_db):
    from investment_agent.evidence.contracts import EvidenceRecord, Period
    from investment_agent.storage.models.evidence import SavedEvidence
    from investment_agent.storage.models.reference import Company, Listing
    candidate, evidence, first = await fixture()
    async with session_scope() as session:
        company = Company(canonical_name="Peer")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SH", ticker="600001.SH", is_primary=True)
        session.add(listing)
        await session.flush()
        peer = ObjectRef(kind="listing", id=listing.id, revision=1)
        original = await session.get(SavedEvidence, (str(evidence.id), 1))
        record = EvidenceRecord.model_validate(original.payload).model_copy(update={"id": uuid4(), "subject_ref": peer, "period": Period(start=date(2024,1,1), end=date(2024,12,31), basis="annual")})
        session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
        peer_evidence = ObjectRef(kind="evidence", id=record.id, revision=1)
        # Extend the fixture mandate through its own trusted service; never add
        # the peer implicitly from ticker text.
        from investment_agent.investor.common import Mutation
        from investment_agent.investor.foundations import InvestorService
        foundation = InvestorService(session)
        mandate = await foundation.get_mandate("local_owner")
        await foundation.revise_mandate("local_owner", mandate.model_copy(update={"scope_refs": (first, peer), "revision": mandate.revision+1}), Mutation(expected_revision=mandate.revision,idempotency_key=uuid4()))
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="Compare these", context_refs=(first, peer)))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="comparison_period_or_basis"):
            await validate_progress(session, receipt.task.id, progress(kind="comparison", fact_refs=(evidence,peer_evidence)))
        original_record = EvidenceRecord.model_validate(original.payload)
        comparable = original_record.model_copy(update={"id": uuid4(), "source_ref": original_record.source_ref.model_copy(update={"field_path": "cash"})})
        session.add(SavedEvidence(id=str(comparable.id),revision=1,owner_id="local_owner",payload=comparable.model_dump(mode="json")))
        evidence = ObjectRef(kind="evidence",id=comparable.id,revision=1)
        record = record.model_copy(update={"id": uuid4(), "period": original_record.period, "source_ref": record.source_ref.model_copy(update={"field_path": "cash"})})
        session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
        await session.flush()
        peer_evidence = ObjectRef(kind="evidence", id=record.id, revision=1)
        text, refs = await validate_progress(session, receipt.task.id, progress(kind="comparison", fact_refs=(evidence,peer_evidence)))
        assert len(refs) == 2 and text


@pytest.mark.asyncio
async def test_real_http_cancel_settles_sent_usage_without_publishing(conversation_db):
    import httpx

    from investment_agent.storage.models.agent import BudgetEntry
    conversation = await new_conversation()
    receipt = await submit(conversation)
    async def gateway(request):
        async with session_scope() as session:
            await TaskService(session).cancel(receipt.task.id)
        output = ResearchOutput(progress=progress())
        return httpx.Response(200, json={"id":"fixture","object":"chat.completion","created":1,"model":"fixture-model",
            "choices":[{"index":0,"message":{"role":"assistant","content":output.model_dump_json()},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":15,"completion_tokens":7,"total_tokens":22}})
    executor = AgentExecutor(base(), registrations=registrations(), specification=ResearchPolicy(base()).specification(), transport=httpx.MockTransport(gateway))
    worker = ResearchWorker(settings=base(), worker_id="mock-http", handlers=[AgentJobHandler("agent_turn",executor)])
    await worker.run_once()
    async with session_scope() as session:
        task = await TaskService(session).get_task(receipt.task.id)
        assert task.status == TaskStatus.CANCELLED
        assert len(await ConversationService(session,base()).messages(conversation.id)) == 1
        charges = (await session.scalars(select(BudgetEntry).where(BudgetEntry.kind == "model"))).all()
        assert len(charges) == 1 and charges[0].input_tokens == 15 and charges[0].output_tokens == 7
        assert charges[0].state == "settled"


@pytest.mark.asyncio
async def test_natural_memory_expiry_filters_original_and_derived_context(conversation_db, monkeypatch):
    from datetime import timedelta

    from investment_agent.investor.controls import (
        ExecuteRequest,
        MemoryAction,
        UserControlService,
        authenticated_request,
    )
    conversation = await new_conversation()
    source = await submit(conversation, message(text="EXPIRED_SOURCE_MARKER"))
    await run(source, FunctionModel(lambda messages,info: response(ResearchOutput(progress=progress(kind="local_check",content=(TextNode(type="text",text="EXPIRED_DERIVED_MARKER"),))))))
    anchor = datetime.now(UTC)
    async with session_scope() as session:
        operation = MemoryAction(kind="explicit_preference",text="EXPIRED_PREFERENCE_MARKER",
            source_refs=(ObjectRef(kind="message",id=source.message.id,revision=1),), valid_until=anchor+timedelta(seconds=60))
        await UserControlService(session).execute(ExecuteRequest(parameters=operation,idempotency_key=uuid4()),authenticated_request(operation))
    # Injected clock instead of a real sleep (repo convention, test_investor_controls).
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor + timedelta(seconds=120)
    monkeypatch.setattr("investment_agent.agent.execution.datetime", Clock)
    monkeypatch.setattr("investment_agent.investor.controls.datetime", Clock)
    receipt = await submit(conversation)
    def model(messages,info):
        assert "EXPIRED_SOURCE_MARKER" not in info.instructions
        assert "EXPIRED_DERIVED_MARKER" not in info.instructions
        assert "EXPIRED_PREFERENCE_MARKER" not in info.instructions
        return response(ResearchOutput(progress=progress()))
    task = await run(receipt,FunctionModel(model))
    assert task.status == TaskStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize('via_tool', [False, True])
async def test_native_repair_feedback_preserves_gates_and_same_task_budget(conversation_db, via_tool):
    from pydantic_ai.messages import RetryPromptPart

    from investment_agent.storage.models.agent import BudgetEntry
    conversation = await new_conversation()
    receipt = await submit(conversation)
    invalid = progress(question='Unreferenced financial value 123')
    valid = progress(question='Explain the available material')
    turns = 0
    def model(messages, info):
        nonlocal turns
        turns += 1
        if turns == 1:
            if via_tool:
                return ModelResponse([ToolCallPart('save_research_progress', {'business': invalid.model_dump(mode='json')})], usage=RequestUsage(input_tokens=20, output_tokens=10))
            return response(ResearchOutput(progress=invalid))
        assert any(isinstance(part, RetryPromptPart) and 'progress.question' in str(part.content) for m in messages for part in m.parts)
        return response(ResearchOutput(progress=valid))
    task = await run(receipt, FunctionModel(model), base(agent_retries=2))
    assert turns == 2 and task.status == TaskStatus.SUCCEEDED
    async with session_scope() as session:
        notes = await research_artifacts(session, task.id)
        assert all(n['progress']['question'] == valid.question for n in notes)
        entries = (await session.scalars(select(BudgetEntry).where(BudgetEntry.task_id == str(task.id), BudgetEntry.kind == 'model', BudgetEntry.state == 'settled'))).all()
        assert len(entries) == 2 and sum(e.input_tokens + e.output_tokens for e in entries) == 60


@pytest.mark.asyncio
async def test_native_repair_stops_at_limit_and_never_publishes_invalid_output(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    turns = 0
    def model(messages, info):
        nonlocal turns
        turns += 1
        return response(ResearchOutput(progress=progress(question='Unreferenced 123')))
    task = await run(receipt, FunctionModel(model), base(agent_retries=1))
    assert turns == 2 and task.status == TaskStatus.FAILED
    async with session_scope() as session:
        assert not await research_artifacts(session, task.id)
        messages = await ConversationService(session, base()).messages(conversation.id)
        assert '123' not in messages[-1].text


@pytest.mark.asyncio
async def test_native_repair_cannot_exceed_request_allowance(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    turns = 0
    def model(messages, info):
        nonlocal turns
        turns += 1
        return response(ResearchOutput(progress=progress(question='Unreferenced 123')))
    task = await run(receipt, FunctionModel(model), base(agent_retries=2, investor_agent_task_requests=1))
    assert turns == 1 and task.stop_reason == StopReason.BUDGET_EXHAUSTED
    async with session_scope() as session:
        assert not await research_artifacts(session, task.id)


@pytest.mark.asyncio
async def test_native_initial_plan_requires_tool_progress_or_explicit_stop(conversation_db):
    from pydantic_ai.messages import RetryPromptPart

    from investment_agent.investor.contracts import DIMENSIONS, DimensionAssessment
    candidate, _, subject = await fixture()
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text='深入研究', context_refs=(subject,), research_kind='company_research'))
    dimensions = tuple(DimensionAssessment(dimension=d, status='missing', applicable=True, reason='Not yet checked', refs=()) for d in DIMENSIONS)
    plan = full_progress(dimensions, answered=False)
    turns = 0
    def model(messages, info):
        nonlocal turns
        turns += 1
        if turns == 1:
            return response(ResearchOutput(progress=plan))
        assert any(isinstance(p, RetryPromptPart) and 'research_plan_is_not_final_answer' in str(p.content) for m in messages for p in m.parts)
        return response(ResearchOutput(progress=plan.model_copy(update={'stopped_because': 'Required original disclosure unavailable', 'gaps': ('Original disclosure not verified',)})))
    task = await run(receipt, FunctionModel(model), base(agent_retries=2))
    assert turns == 2 and task.status == TaskStatus.PARTIAL


def test_security_identity_schema_and_parser_exclude_internal_references():
    from pydantic import ValidationError

    from investment_agent.investor.contracts import EntityNode
    valid = ObjectRef(kind='listing', id=1, revision=1)
    assert EntityNode(type='entity_ref', ref=valid).ref == valid
    for kind in ('case', 'method', 'message', 'task'):
        with pytest.raises(ValidationError, match='listing'):
            EntityNode(type='entity_ref', ref=ObjectRef(kind=kind, id=uuid4(), revision=1))
    assert EntityNode.model_json_schema()['properties']['ref']['allOf'][0]['properties']['kind']['const'] == 'listing'


@pytest.mark.asyncio
async def test_native_progress_receipt_is_compact_but_durable_research_is_complete(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    saved = progress(content=(TextNode(type='text', text='有来源的研究进展。' * 100),))
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse([ToolCallPart('save_research_progress', {'business': saved.model_dump(mode='json')})], usage=RequestUsage(input_tokens=20, output_tokens=10))
        assert set(returns[0].content) == {'status', 'artifact_ref', 'refs'}
        assert returns[0].content['status'] == 'saved'
        return response(ResearchOutput(progress=saved))
    task = await run(receipt, FunctionModel(model))
    assert task.status == TaskStatus.SUCCEEDED
    async with session_scope() as session:
        notes = await research_artifacts(session, task.id)
        assert all(n['progress']['content'] == saved.model_dump(mode='json')['content'] for n in notes)
        assert all(n['text'].startswith(saved.content[0].text) for n in notes)


@pytest.mark.parametrize("output_name", ["CompanyResearchOutput", "LocalResearchOutput"])
def test_company_schema_omits_unrelated_contracts_without_weakening_runtime_validation(output_name):
    from pydantic import ValidationError

    from investment_agent.agent import research
    output_type = getattr(research, output_name)
    schema = output_type.model_json_schema()
    assert 'portfolio_review' not in schema['properties'] and 'proposal' not in schema['properties']
    assert len(json.dumps(schema)) < len(json.dumps(ResearchOutput.model_json_schema())) * .6
    with pytest.raises(ValidationError):
        output_type.model_validate({'progress': progress().model_dump(mode='json'), 'proposal': {'unknown': True}})


@pytest.mark.asyncio
@pytest.mark.parametrize("research_kind", ["company_research", "local_check"])
async def test_http_company_plan_capture_conclusion_with_reserved_headroom(conversation_db, research_kind):
    from datetime import timedelta

    import httpx

    from investment_agent.storage.models.agent import AgentModelRun, BudgetEntry
    from investment_agent.storage.models.evidence import SavedEvidence
    from investment_agent.storage.models.reference import Listing, Market
    candidate, _, subject = await fixture()
    cutoff = datetime.now(UTC)
    async with session_scope() as session:
        session.add(Market(code='SSE', name='Shanghai', timezone='Asia/Shanghai', currency='CNY'))
        await session.flush()
        listing = await session.get(Listing, subject.id)
        listing.ticker, listing.market_code = '600519', 'SSE'
        session.add(MarketSnapshot(listing_id=subject.id, provider='baostock', trade_date=cutoff.date() - timedelta(days=1),
            captured_at=cutoff - timedelta(minutes=1), version=1,
            open=10, high=10, low=10, close=10, volume=100, adjust='none', raw_ref='fixture:close'))
    selected = base(investor_agent_task_requests=4, investor_agent_task_tokens=120000, agent_max_tokens=4096)
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text='深入研究', context_refs=(subject,), research_at=cutoff, research_kind=research_kind), settings=selected)
    plan = (full_progress(candidate.thesis.assessed_dimensions, answered=False) if research_kind == "company_research"
            else progress(kind=research_kind, answered=False))
    bodies = []
    captured = None
    def handle(request):
        nonlocal captured
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            call = ('save_research_progress', plan.model_dump(mode='json'))
        elif len(bodies) == 2:
            call = ('capture_snapshot', {'ticker': '600519.SH', 'field': 'close', 'as_of': cutoff.isoformat()})
        else:
            assert len(bodies) == 3
            assert {t['function']['name'] for t in body.get('tools', [])} == ({'prepare_company_view'} if research_kind == 'company_research' else set())
            captured = next(json.loads(m['content']) for m in body['messages'] if m['role'] == 'tool' and 'materials' in m['content'])
            record = captured['materials'][0]['record']
            assert record['verification'] == 'candidate' and record['value']['value'] is not None
            material = captured['materials'][0]
            assert material['eligibility']['fact_reference_allowed'] is True
            assert material['numeric_claim']['ref'] == {**material['ref'], 'field_path': 'value'}
            assert material['numeric_claim']['role'] == 'historical_fact'
            missing = [m for item in body['messages'] if item['role'] == 'tool' and 'materials' in item['content']
                       for m in json.loads(item['content'])['materials'] if not m['record'].get('value') or m['record']['value']['value'] is None]
            assert missing and all(not m['eligibility']['fact_reference_allowed'] and 'numeric_claim' not in m for m in missing)
            final = plan.model_copy(update={'fact_refs': tuple(ObjectRef.model_validate(r) for r in captured['refs']),
                'inferences': ('A visible quote establishes price availability but cannot establish durable business value.',),
                'assumptions': ('Operating durability still requires original disclosures.',),
                'gaps': ('Original disclosures and business-specific counter evidence are unavailable.',),
                'stopped_because': 'The available candidate price cannot answer the business thesis.'})
            msg = {'role': 'assistant', 'content': ResearchOutput(progress=final).model_dump_json()}
        if len(bodies) < 3:
            msg = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': f'call-{len(bodies)}', 'type': 'function',
                   'function': {'name': call[0], 'arguments': json.dumps({'business': call[1]})}}]}
        if len(bodies) == 2:
            msg['tool_calls'].append({'id': 'missing-field', 'type': 'function', 'function': {'name': 'capture_snapshot', 'arguments': json.dumps({'business': {**call[1], 'field': 'pb_mrq'}})}})
        return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 1, 'model': 'fixture',
            'choices': [{'index': 0, 'message': msg, 'finish_reason': 'tool_calls' if len(bodies) < 3 else 'stop'}],
            'usage': {'prompt_tokens': 5000, 'completion_tokens': 1000, 'total_tokens': 6000}})
    executor = AgentExecutor(selected, transport=httpx.MockTransport(handle), registrations=registrations(), specification=ResearchPolicy(selected).specification())
    worker = ResearchWorker(settings=selected, worker_id='closure-http', handlers=[AgentJobHandler('agent_turn', executor)])
    await worker.run_once()
    async with session_scope() as session:
        task = await TaskService(session).get_task(receipt.task.id)
        assert len(bodies) == 3 and task.status == TaskStatus.PARTIAL
        assert task.usage.requests == 3 and task.usage.input_tokens == 15000
        notes = await research_artifacts(session, task.id)
        assert any(n['progress']['fact_refs'] and n['progress']['inferences'] for n in notes)
        for material in captured['materials']:
            ref = material['ref']
            saved = await session.get(SavedEvidence, (ref['id'], ref['revision']))
            assert material['record'] == saved.payload
        audit = await session.scalar(select(AgentModelRun).where(AgentModelRun.task_id == str(task.id)))
        assert audit.success and task.stop_reason == StopReason.EVIDENCE_BLOCKED
        entries = (await session.scalars(select(BudgetEntry).where(BudgetEntry.task_id == str(task.id), BudgetEntry.kind == 'model'))).all()
        assert sum(e.estimated_tokens for e in entries) == sum(d['input_estimate'] + 4096 for d in audit.config_snapshot['request_sizes'])
        assert audit.config_snapshot['request_sizes'][-1]['tools_bytes'] < audit.config_snapshot['request_sizes'][1]['tools_bytes']

    if research_kind == "local_check":
        return  # Cross-task evidence continuation is specific to full research/review.

    # Resume from owned source records, not private model history or stale plans.
    from investment_agent.agent.context import pack_evidence
    from investment_agent.settings.store import SettingsStore
    packed = pack_evidence(captured['materials'])
    assert [{**{k: v for k, v in p.items() if k not in {'shared_index', 'record'}},
             "record": {**packed["shared"][p["shared_index"]], **p["record"]}} for p in packed["records"]] == captured['materials']
    final_settings = selected.model_copy(update={"investor_agent_task_requests": 1})
    async with session_scope() as session:
        await SettingsStore(session).apply({"investor_agent_task_requests": 1}, actor="fixture")
    continuation = await submit(conversation, message(text='深入研究并完成结论', context_refs=(subject,), research_at=cutoff, research_kind=research_kind), settings=final_settings)
    # Public continuation admission: the same grant, re-admitted as a child of
    # the finished research through admit() rather than private row writes.
    from investment_agent.agent.execution import ExecutionGrant, admit
    async with session_scope() as session:
        grant = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(continuation.task.id))).payload)
        child = await admit(session, base=final_settings, input_key=f"continuation:{uuid4()}",
            binding=continuation.task.binding, grant=grant, parent_task_id=task.id,
            message_ref=continuation.task.message_ref)
        child_run = (await session.get(AgentTask, str(child.id))).run_id
    def conclude(messages, info):
        assert not info.function_tools
        assert 'recovered_evidence' in info.instructions
        recovered = json.JSONDecoder().raw_decode(info.instructions.split("Trusted current context:\n")[1])[0]["recovered_evidence"]
        assert any(not record["eligibility"]["fact_reference_allowed"] for record in recovered["materials"]["records"])
        assert captured['materials'][0]['record']['content_hash'] in info.instructions
        final = plan.model_copy(update={
            'fact_refs': tuple(ObjectRef.model_validate(r) for r in captured['refs']),
            'inferences': ('Recovered price evidence cannot establish a business thesis.',),
            'gaps': ('Original business disclosures remain unavailable.',),
            'stopped_because': 'Conclude without a directional opinion while original evidence is missing.'})
        return response(ResearchOutput(progress=final))
    finished = await run(SimpleNamespace(task=child), FunctionModel(conclude), final_settings, run_id=child_run)
    assert finished.status == TaskStatus.PARTIAL and finished.stop_reason == StopReason.EVIDENCE_BLOCKED
    assert finished.usage.requests == 1 and finished.usage.tool_calls == 0


@pytest.mark.asyncio
async def test_two_bounded_output_repairs_do_not_get_misclassified_as_research_stagnation(conversation_db):
    conversation = await new_conversation()
    receipt = await submit(conversation)
    turns = 0
    def model(messages, info):
        nonlocal turns
        turns += 1
        if turns == 1:
            item = progress(question='Unreferenced 123')
        elif turns == 2:
            item = progress(content=(TextNode(type='text', text='Another unreferenced 456'),))
        else:
            item = progress()
        return response(ResearchOutput(progress=item))
    task = await run(receipt, FunctionModel(model), base(agent_retries=2))
    assert turns == task.usage.requests == 3 and task.status == TaskStatus.SUCCEEDED
    assert task.usage.input_tokens == 60 and task.usage.output_tokens == 30


def test_native_search_projection_preserves_risks_and_exact_refs_without_mutating_receipt():
    from copy import deepcopy

    from pydantic import ValidationError

    from investment_agent.agent.builtin_tools import AgentSearchRequest, model_search_result

    hit = {"evidence_ref": {"kind": "evidence", "id": str(uuid4()), "revision": 1},
           "subject_ref": {"kind": "listing", "id": 178, "revision": 1},
           "text": "context " * 600, "text_truncated": False,
           "effective_status": "conflict", "verification_states": ["candidate"],
           "warnings": ["source_corrected"], "original_hash": "exact-original",
           "source_copy_count": 2, "adjacent_context": [{"text": "Full adjacent disclosure"}],
           "source_audit": [{"verification": "candidate"}, {"verification": "verified"}]}
    receipt = {"matches": [hit], "scanned_records": 4, "window_exhausted": True}
    before = deepcopy(receipt)
    projection = model_search_result(receipt)
    assert receipt == before
    shown = projection["matches"][0]
    assert shown["evidence_ref"] == hit["evidence_ref"]
    assert shown["warnings"] == hit["warnings"] and shown["effective_status"] == "conflict"
    assert shown["verification_states"] == ["candidate"] and shown["source_copy_count"] == 2
    assert shown["text_truncated"] and shown["read_required"] and len(shown["text"]) == 500
    assert projection["window_exhausted"] and shown["source_audit_count"] == 2
    with pytest.raises(ValidationError):
        AgentSearchRequest(listing_ids=(178,), query="玻璃", as_of=datetime.now(UTC), limit=6)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [False, True])
async def test_native_discovery_then_readback_progress_and_repeated_search_stop(conversation_db, repeat):
    import hashlib

    from investment_agent.evidence.contracts import EvidenceRecord
    from investment_agent.storage.models.evidence import SavedEvidence

    _, _, subject = await fixture()
    now = datetime.now(UTC)
    async with session_scope() as session:
        for text in ("alpha disclosure", "beta disclosure"):
            record = EvidenceRecord(id=uuid4(), revision=1, actor="fixture", created_at=now, origin_ref=None,
                source_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1), source_kind="document",
                locator="fixture", content_hash=hashlib.sha256(text.encode()).hexdigest(), subject_ref=subject,
                period=None, published_at=now, acquired_at=now, available_from=now,
                verification="candidate", conflict="none", text=text)
            session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
    conversation = await new_conversation()
    receipt = await submit(conversation, message(text="Inspect saved disclosures", context_refs=(subject,), research_kind="local_check"))
    turns = 0
    def respond(messages, info):
        nonlocal turns
        turns += 1
        if turns <= (3 if repeat else 2):
            query = "alpha" if turns == 1 or repeat else "beta"
            return ModelResponse([ToolCallPart("search_saved_sources", {"business": {
                "listing_ids": [subject.id], "query": query, "as_of": receipt.task.binding.as_of.isoformat(), "limit": 1
            }}, tool_call_id=f"search-{turns}")], usage=RequestUsage(input_tokens=10, output_tokens=5))
        hits = [p.content["matches"][0] for m in messages for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == "search_saved_sources"]
        ref = ObjectRef.model_validate(hits[0]["evidence_ref"])
        if turns == 3:
            assert hits[0]["read_required"]
            return ModelResponse([ToolCallPart("get_evidence", {"business": {
                "ref": ref.model_dump(mode="json"), "subject_ref": subject.model_dump(mode="json"),
                "as_of": receipt.task.binding.as_of.isoformat()
            }}, tool_call_id="read")], usage=RequestUsage(input_tokens=10, output_tokens=5))
        return response(ResearchOutput(progress=progress(kind="local_check", fact_refs=(ref,))))
    task = await run(receipt, FunctionModel(respond))
    assert task.stop_reason == (StopReason.NO_PROGRESS if repeat else StopReason.ANSWERED)
    assert task.usage.requests == (2 if repeat else 4)


def test_native_schema_retry_stagnation_exemption_is_finite():
    from investment_agent.agent.executor import _RequestBudget

    budget = _RequestBudget(SimpleNamespace(), SimpleNamespace())
    for _ in range(2):
        budget.validation_retry()
        assert budget.output_repair_pending
        budget.output_repair_pending = False
    budget.validation_retry()
    assert not budget.output_repair_pending and budget.native_validation_retries == 2

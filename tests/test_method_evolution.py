"""AG30 actual native outputs, file SQLite history, scoped user approval."""
import asyncio
import json
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import delete, func, select

from investment_agent.agent.contracts import Task
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant
from investment_agent.agent.research import ResearchOutput, validate_progress
from investment_agent.agent.tasks import fingerprint
from investment_agent.api.app import create_app
from investment_agent.api.auth import get_bootstrap_settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.methods import (
    CreateProposal,
    EvaluateProposal,
    EvaluationCase,
    MethodConfig,
    MethodDecision,
    MethodRollback,
    MethodService,
    ProspectiveRequest,
    ShadowRequest,
    run_offline_shadow,
    task_method,
)
from investment_agent.investor.publication import (
    FIXED_METHOD,
    PublicationService,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import BudgetEntry, ExecutionGrantRow, ToolAction
from investment_agent.storage.models.investor import MethodArtifact, TaskMethodBinding
from investment_agent.storage.models.publication import PublicationEvent, PublicationOutbox
from tests.support.conversations import (
    base,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.support.methods import fresh_native_tasks, samples
from tests.support.publication import fixture
from tests.support.research import full_progress, progress, response, run


def replay_model(messages, info):
    prompt = next(p.content for m in reversed(messages) for p in m.parts if getattr(p, "part_kind", None) == "user-prompt")
    return ModelResponse([ToolCallPart(info.output_tools[0].name, json.loads(prompt)["frozen_progress"])])


async def proposal_for(subject, failure):
    request = CreateProposal(mutation_id=uuid4(), review_id=UUID(failure["id"]), listing_ref=subject, old_method=FIXED_METHOD, error_pattern="Only one alternative survived the incomplete process", summary="Require two independent opposing explanations", config=MethodConfig(require_two_counter_hypotheses=True))
    async with session_scope() as session:
        proposal = await MethodService(session).create(request)
        assert await MethodService(session).create(request) == proposal
    return proposal


async def evaluated(proposal, failure, counter):
    outside_task = await submit(await new_conversation(), message(text="解释经营研究方法", research_kind="explanation"))
    outside_task = await run(outside_task, FunctionModel(lambda m, i: response(ResearchOutput(progress=progress()))))
    async with session_scope() as session:
        outside = await MethodService(session).process_review(outside_task.id)
    request = EvaluateProposal(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), expected_revision=proposal["revision"], cases=(EvaluationCase(review_id=UUID(failure["id"]), role="failure", expected="block"), EvaluationCase(review_id=UUID(counter["id"]), role="counterexample", expected="pass"), EvaluationCase(review_id=UUID(outside["id"]), role="counterexample", expected="pass")))
    async with session_scope() as session:
        result = await MethodService(session).evaluate(request)
    assert result["evaluation"]["passed"]
    return result["proposal"], result["evaluation"]


async def decision_for(proposal, subject, decision="approve"):
    async with session_scope() as session:
        svc = MethodService(session)
        evidence = await svc.evidence(await svc.proposal(UUID(proposal["id"])))
        revision, _ = await svc.current(subject)
    return MethodDecision(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), expected_revision=proposal["revision"], expected_head_revision=revision, listing_ref=subject, evidence_hash=evidence["evidence_hash"], summary=evidence["approval_summary"], decision=decision)


@pytest.mark.asyncio
async def test_actual_failure_counterexamples_native_shadow_approval_binding_rollback(conversation_db):
    subject, failure_task, success_task, failure, counter, weak, strong = await samples()
    proposal = await proposal_for(subject, failure)
    proposal, evaluation = await evaluated(proposal, failure, counter)
    async with session_scope() as session:
        svc = MethodService(session)
        assert (await svc.current(subject))[1].method_ref == FIXED_METHOD
        before = [await session.scalar(select(func.count()).select_from(t)) for t in (PublicationEvent, PublicationOutbox, BudgetEntry)]
        old_payload = (await session.get(ExecutionGrantRow, str(failure_task.id))).payload
        task_payload = Task.model_validate(failure["inputs"]["task"]).model_dump(mode="json")
    request = ShadowRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), expected_revision=proposal["revision"], evaluation_id=UUID(evaluation["id"]))
    shadow = await run_offline_shadow(session_scope, request, FunctionModel(replay_model))
    assert shadow["passed"] and {c["checked"]["result"] for c in shadow["cases"]} == {"pass", "block"}
    assert await run_offline_shadow(session_scope, request, FunctionModel(lambda m, i: pytest.fail("lost ack must not call again"))) == shadow
    async with session_scope() as session:
        assert before == [await session.scalar(select(func.count()).select_from(t)) for t in (PublicationEvent, PublicationOutbox, BudgetEntry)]
        proposal = (await MethodService(session).proposal(UUID(proposal["id"]))).payload
    # Offline echo evidence alone never permits activation.
    offline_only = await decision_for(proposal, subject)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="完整"):
            await MethodService(session).decide(offline_only, actor="authenticated_user")
    fresh_failure, fresh_success = await fresh_native_tasks(subject, weak, strong)
    prospective_request = ProspectiveRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), expected_revision=proposal["revision"], evaluation_id=UUID(evaluation["id"]), task_ids=(fresh_failure.id, fresh_success.id))
    async with session_scope() as session:
        before_prospective = [await session.scalar(select(func.count()).select_from(t)) for t in (PublicationEvent, PublicationOutbox, BudgetEntry)]
        prospective_result = await MethodService(session).prospective(prospective_request)
        assert prospective_result["prospective"]["passed"]
        assert await MethodService(session).prospective(prospective_request) == prospective_result
        assert before_prospective == [await session.scalar(select(func.count()).select_from(t)) for t in (PublicationEvent, PublicationOutbox, BudgetEntry)]
        proposal = prospective_result["proposal"]
    decision = await decision_for(proposal, subject)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="模型"):
            await MethodService(session).decide(decision, actor="model")
    async with session_scope() as session:
        active = await MethodService(session).decide(decision, actor="authenticated_user")
        assert await MethodService(session).decide(decision, actor="authenticated_user") == active
        binding = (await MethodService(session).current(subject))[1]
        assert binding.config.require_two_counter_hypotheses
        assert (await task_method(session, failure_task.id)).method_ref == FIXED_METHOD
        assert (await session.get(ExecutionGrantRow, str(failure_task.id))).payload == old_payload
        assert Task.model_validate(task_payload).model_dump(mode="json") == task_payload
    # Actual native formal publication carries the exact approved task binding.
    _, scoped_success = await fresh_native_tasks(subject, strong.model_copy(update={"answered": False}), strong)
    async with session_scope() as session:
        published_refs = [r for r in scoped_success.artifact_refs if r.kind == "recommendation"]
        assert published_refs
        view = await PublicationService(session).get_recommendation(published_refs[-1])
        assert view.recommendation.method_revision_id == binding.method_ref
        assert (await PublicationService(session).get_thesis(view.recommendation.thesis_revision_refs[0])).method_ref == binding.method_ref
    new = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,)))
    async with session_scope() as session:
        assert (await task_method(session, new.task.id)) == binding
        with pytest.raises(ExecutionDeniedError, match="counter_hypotheses"):
            await validate_progress(session, new.task.id, weak)
        await validate_progress(session, failure_task.id, weak)
        await validate_progress(session, new.task.id, strong)
    # Actual native policy uses the strengthening; no formal output may bypass it.
    stopped = await run(new, FunctionModel(lambda m, i: response(ResearchOutput(progress=weak))), base(agent_retries=0))
    assert stopped.status.value == "failed"
    rollback = MethodRollback(mutation_id=uuid4(), listing_ref=subject, expected_head_revision=1, target_method=FIXED_METHOD, reason="Restore baseline after inspecting engineering evidence")
    async with session_scope() as session:
        svc = MethodService(session)
        result = await svc.rollback(rollback, actor="authenticated_user")
        assert await svc.rollback(rollback, actor="authenticated_user") == result
        assert not (await svc.current(subject))[1].config.require_two_counter_hypotheses
        assert (await svc.proposal(UUID(active["id"]))).payload["status"] == "retired"
        assert (await task_method(session, new.task.id)) == binding
        assert (await svc.known(binding.method_ref)) == binding
        assert await svc.decide(decision, actor="authenticated_user") == active
        assert (await PublicationService(session).get_recommendation(published_refs[-1])).recommendation.method_revision_id == binding.method_ref
        assert await session.scalar(select(func.count()).select_from(MethodArtifact)) > 10
    renewed = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,)))
    async with session_scope() as session:
        assert (await task_method(session, renewed.task.id)).method_ref != FIXED_METHOD
        assert not (await task_method(session, renewed.task.id)).config.require_two_counter_hypotheses


@pytest.mark.asyncio
async def test_incomplete_evaluation_no_counterexample_no_shadow_and_no_self_activation(conversation_db):
    subject, _, _, failure, counter, _, _ = await samples()
    proposal = await proposal_for(subject, failure)
    async with session_scope() as session:
        svc = MethodService(session)
        with pytest.raises(InvestorError, match="完整"):
            await svc.decide(await decision_for(proposal, subject), actor="authenticated_user")
    with pytest.raises(ValidationError):
        EvaluateProposal(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), expected_revision=1, cases=(EvaluationCase(review_id=UUID(failure["id"]), role="failure", expected="block"),))
    for invalid in ({"formula": "new formula"}, {"require_two_counter_hypotheses": "yes"}, {"permissions": ["relax"]}, {"profit": "winning trade"}):
        with pytest.raises(ValidationError):
            MethodConfig.model_validate(invalid)
    proposal, _ = await evaluated(proposal, failure, counter)
    request = await decision_for(proposal, subject)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="完整"):
            await MethodService(session).decide(request, actor="authenticated_user")
    async with session_scope() as session:
        rejected = await MethodService(session).decide(request.model_copy(update={"decision": "reject"}), actor="authenticated_user")
        assert rejected["status"] == "rejected"
        assert (await MethodService(session).current(subject))[0] == 0


@pytest.mark.asyncio
async def test_shadow_fence_concurrent_user_reject_no_side_effects(conversation_db):
    subject, _, _, failure, counter, _, _ = await samples()
    proposal = await proposal_for(subject, failure)
    proposal, evaluation = await evaluated(proposal, failure, counter)
    entered, release = asyncio.Event(), asyncio.Event()
    async def model(messages, info):
        entered.set()
        await release.wait()
        return replay_model(messages, info)
    request = ShadowRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), expected_revision=proposal["revision"], evaluation_id=UUID(evaluation["id"]))
    pending = asyncio.create_task(run_offline_shadow(session_scope, request, FunctionModel(model)))
    await entered.wait()
    async with session_scope() as session:
        current = (await MethodService(session).proposal(UUID(proposal["id"]))).payload
    rejection = await decision_for(current, subject, "reject")
    async with session_scope() as session:
        await MethodService(session).decide(rejection, actor="authenticated_user")
    release.set()
    result = await pending
    assert result["fenced"] and not result["passed"]
    async with session_scope() as session:
        assert (await MethodService(session).current(subject))[0] == 0


@pytest.mark.asyncio
async def test_http_auth_csrf_unsupported_body_and_legacy_no_method_hash_change(conversation_db, monkeypatch, tmp_path):
    monkeypatch.setattr("investment_agent.web.routes.get_bootstrap_settings", lambda: base(data_dir=tmp_path))
    app = create_app()
    app.dependency_overrides[get_bootstrap_settings] = lambda: base()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get('/investor/methods')).status_code == 401
        assert (await client.get('/investor/methods', headers={"X-API-Key": "fixture-app"})).status_code == 200
        assert (await client.post('/investor/methods/decisions', json={"actor": "model"}, headers={"X-API-Key": "fixture-app"})).status_code == 422
        from investment_agent.web.session import SESSION_COOKIE_NAME, create_session_token
        client.cookies.set(SESSION_COOKIE_NAME, create_session_token(api_key="fixture-app", ttl_minutes=30))
        assert (await client.post('/investor/methods/rollback', json={}, headers={"Origin": "https://evil.invalid"})).status_code == 403
        assert (await client.get('/app/methods')).status_code == 200
    receipt = await submit(await new_conversation())
    async with session_scope() as session:
        grant_row = await session.get(ExecutionGrantRow, str(receipt.task.id))
        legacy_grant = json.dumps(grant_row.payload, sort_keys=True)
        legacy_task = json.dumps(receipt.task.model_dump(mode="json"), sort_keys=True)
        # Absence is the actual pre-040 storage shape; never pick today's head.
        await session.execute(delete(TaskMethodBinding).where(TaskMethodBinding.task_id == str(receipt.task.id)))
        assert (await task_method(session, receipt.task.id)).method_ref == FIXED_METHOD
        assert fingerprint(json.loads(legacy_grant)) == fingerprint(ExecutionGrant.model_validate_json(legacy_grant).model_dump(mode="json"))
        assert fingerprint(json.loads(legacy_task)) == fingerprint(Task.model_validate_json(legacy_task).model_dump(mode="json"))


@pytest.mark.asyncio
async def test_missing_outside_counterexample_and_historical_shadow_cannot_activate(conversation_db):
    subject, failure_task, success_task, failure, counter, _, _ = await samples()
    proposal = await proposal_for(subject, failure)
    request = EvaluateProposal(mutation_id=uuid4(), proposal_id=UUID(proposal['id']), expected_revision=1, cases=(EvaluationCase(review_id=UUID(failure['id']), role='failure', expected='block'), EvaluationCase(review_id=UUID(counter['id']), role='counterexample', expected='pass')))
    async with session_scope() as session:
        incomplete = await MethodService(session).evaluate(request)
        assert not incomplete['evaluation']['complete'] and not incomplete['evaluation']['passed']
        assert incomplete['proposal']['status'] == 'evaluating'
    proposal, evaluation = await evaluated(incomplete['proposal'], failure, counter)
    historical = ProspectiveRequest(mutation_id=uuid4(), proposal_id=UUID(proposal['id']), expected_revision=proposal['revision'], evaluation_id=UUID(evaluation['id']), task_ids=(failure_task.id, success_task.id))
    async with session_scope() as session:
        with pytest.raises(InvestorError, match='历史重放'):
            await MethodService(session).prospective(historical)
        assert not (await MethodService(session).proposal(UUID(proposal['id']))).payload['prospective_ids']


@pytest.mark.asyncio
async def test_native_verified_counter_and_stop_strengthening_keep_baseline_valid(conversation_db):
    subject, failure_task, _, _, _, weak, strong = await samples()
    verified = MethodConfig(require_verified_counter_evidence=True)
    stop = MethodConfig(require_explicit_stop_decision=True)
    async with session_scope() as session:
        await validate_progress(session, failure_task.id, weak)
        with pytest.raises(ExecutionDeniedError, match='verified_counter'):
            await validate_progress(session, failure_task.id, strong, method_config=verified, final=True)
        await validate_progress(session, failure_task.id, strong.model_copy(update={'counter_refs': strong.fact_refs}), method_config=verified, final=True)
        with pytest.raises(ExecutionDeniedError, match='stop_decision'):
            await validate_progress(session, failure_task.id, weak, method_config=stop, final=True)
        await validate_progress(session, failure_task.id, weak.model_copy(update={'stopped_because': 'Material gap prevents completion'}), method_config=stop, final=True)


def test_pre040_literal_serialized_contracts_remain_unchanged():
    # Literal old complete serialized grant, rather than a new-model fixture.
    old_grant = '{"schema_version":1,"owner":"local_owner","mode":"interactive","source":"authenticated_message","prompt":"Explain the saved question","tool_names":[],"scope_refs":[],"watchlist":false,"memory_revision":0,"memory_valid_until":null,"mandate_id":null,"expires_at":null,"task_cost_usd":null,"daily_cost_usd":null,"purpose":"interactive","current_research":false,"research_kind":"explanation","disclosure_permissions":[],"portfolio_account_id":null,"portfolio_snapshot_ref":null,"portfolio_valuation_ref":null,"peer_context":null}'
    # The pre-valuation payload (no ``portfolio_valuation_ref``) still loads:
    # persisted grants predate the field and must not fail re-validation.
    prior_grant = old_grant.replace(',"portfolio_valuation_ref":null}', '}')
    # The same holds for the pre-peer payload (no ``peer_context``, WP1).
    pre_peer_grant = old_grant.replace(',"peer_context":null}', '}')
    old_task = '{"schema_version":1,"id":"00000000-0000-0000-0000-000000000001","revision":1,"actor":"local_owner","created_at":"2026-10-10T00:00:00Z","origin_ref":null,"parent_task_id":null,"message_ref":null,"binding":{"context_refs":[],"base_refs":[],"as_of":"2026-10-10T00:00:00Z"},"status":"queued","cancel_requested":false,"stop_reason":null,"waiting":null,"artifact_refs":[],"usage":{"tool_calls":0,"requests":0,"input_tokens":0,"output_tokens":0,"cost":{"value":null,"unit":"currency","currency":null,"unknown_reason":"not settled"}}}'
    assert fingerprint(ExecutionGrant.model_validate_json(old_grant).model_dump(mode='json')) == fingerprint(json.loads(old_grant))
    assert ExecutionGrant.model_validate_json(prior_grant) == ExecutionGrant.model_validate_json(old_grant)
    assert ExecutionGrant.model_validate_json(pre_peer_grant) == ExecutionGrant.model_validate_json(old_grant)
    assert fingerprint(Task.model_validate_json(old_task).model_dump(mode='json')) == fingerprint(json.loads(old_task))
    from pydantic_ai.messages import ModelMessagesTypeAdapter
    history = b'[{"parts":[{"content":"Saved question","timestamp":"2026-10-10T00:00:00Z","part_kind":"user-prompt"}],"kind":"request"},{"parts":[{"content":"Saved answer","part_kind":"text"}],"usage":{"input_tokens":20,"output_tokens":10},"timestamp":"2026-10-10T00:00:01Z","kind":"response"}]'
    # Old history is loaded privately; neither method service nor activation rewrites it.
    messages = ModelMessagesTypeAdapter.validate_json(history)
    assert messages[0].parts[0].content == 'Saved question'
    assert messages[1].parts[0].content == 'Saved answer'
    assert json.loads(history)[0]['parts'][0]['timestamp'] == '2026-10-10T00:00:00Z'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['require_two_counter_hypotheses', 'require_verified_counter_evidence', 'require_explicit_stop_decision'])
async def test_actual_native_plan_read_final_phase_checks_and_honest_stop(conversation_db, change):
    from investment_agent.investor.methods import MethodBinding
    from investment_agent.storage.models.investor import MethodHead
    candidate, evidence, subject = await fixture()
    weak = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(evidence,), answered=False)
    second = weak.hypotheses[0].model_copy(update={'alternative': 'Sustainable demand may be temporarily overstated'})
    plan = weak.model_copy(update={'hypotheses': (*weak.hypotheses, second)})
    config = MethodConfig(**{change: True})
    # A known scoped fixture version isolates native phase behavior. Actual
    # proposal/user-approval history is tested by the end-to-end test above.
    binding = MethodBinding(method_ref=ObjectRef(kind='method', id=uuid4(), revision=1), config=config, listing_ref=subject)
    async with session_scope() as session:
        session.add(MethodArtifact(id=str(binding.method_ref.id), owner_id='local_owner', kind='version', payload={'binding': binding.model_dump(mode='json')}))
        session.add(MethodHead(owner_id='local_owner', listing_id=subject.id, revision=1, payload=binding.model_dump(mode='json')))
    async def execute(final):
        receipt = await submit(await new_conversation(), message(text='深入研究', context_refs=(subject,)))
        def model(messages, info):
            returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            if not any(p.tool_name == 'save_research_progress' for p in returns):
                return ModelResponse([ToolCallPart('save_research_progress', {'business': plan.model_dump(mode='json')})])
            if not any(p.tool_name == 'get_evidence' for p in returns):
                return ModelResponse([ToolCallPart('get_evidence', {'business': {'ref': evidence.model_dump(mode='json'), 'subject_ref': subject.model_dump(mode='json'), 'as_of': receipt.task.binding.as_of.isoformat()}})])
            return response(ResearchOutput(progress=final))
        task = await run(receipt, FunctionModel(model), base(agent_retries=0))
        async with session_scope() as session:
            actions = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id)))).all()
            assert any(a.parameters.get('tool') == 'get_evidence' and a.state == 'committed' for a in actions)
            assert any(a.parameters.get('tool') == 'save_research_progress' and a.intent_key != 'completion:progress' and a.state == 'committed' for a in actions)
            return task, any(a.intent_key == 'completion:progress' and a.state == 'committed' for a in actions)
    final = plan.model_copy(update={'answered': True, 'counter_refs': (evidence,)})
    task, completed = await execute(final)
    assert completed and task.status.value == 'partial'  # no formal review was requested
    honest = plan.model_copy(update={'answered': False, 'stopped_because': 'Opposing source remains unavailable', 'gaps': ('Cannot establish an opposing original source',), 'counter_refs': ()})
    task, completed = await execute(honest)
    assert completed and task.status.value == 'partial'
    if change == 'require_two_counter_hypotheses':
        missing = final.model_copy(update={'hypotheses': (plan.hypotheses[0],)})
        duplicate = plan.hypotheses[0].model_copy(update={'alternative': '  ' + plan.hypotheses[0].alternative.upper() + '  '})
        async with session_scope() as session:
            with pytest.raises(ExecutionDeniedError, match='counter_hypotheses'):
                await validate_progress(session, task.id, plan.model_copy(update={'hypotheses': (plan.hypotheses[0], duplicate)}))
    elif change == 'require_verified_counter_evidence':
        async with session_scope() as session:
            with pytest.raises(ExecutionDeniedError, match='verified_counter'):
                await validate_progress(session, task.id, honest, final=True, formal_directional=True)
        missing = final.model_copy(update={'counter_refs': ()})
    else:
        missing = plan
    task, completed = await execute(missing)
    assert not completed and task.status.value == 'partial'


@pytest.mark.asyncio
async def test_actual_file_sqlite_user_cas_race_wrong_scope_and_mutation_payload(conversation_db):
    subject, _, _, failure, counter, weak, strong = await samples()
    proposal = await proposal_for(subject, failure)
    proposal, evaluation = await evaluated(proposal, failure, counter)
    failed, succeeded = await fresh_native_tasks(subject, weak, strong)
    request = ProspectiveRequest(mutation_id=uuid4(), proposal_id=UUID(proposal['id']), expected_revision=proposal['revision'], evaluation_id=UUID(evaluation['id']), task_ids=(failed.id, succeeded.id))
    async with session_scope() as session:
        prospective = await MethodService(session).prospective(request)
    decision = await decision_for(prospective['proposal'], subject)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match='范围'):
            await MethodService(session).decide(decision.model_copy(update={'listing_ref': subject.model_copy(update={'id': 999})}), actor='authenticated_user')
    async def approve(body):
        async with session_scope() as session:
            return await MethodService(session).decide(body, actor='authenticated_user')
    concurrent = await asyncio.gather(approve(decision), approve(decision.model_copy(update={'mutation_id': uuid4()})), return_exceptions=True)
    winners = [r for r in concurrent if isinstance(r, dict)]
    assert len(winners) == 1 and len([r for r in concurrent if isinstance(r, InvestorError)]) == 1
    async with session_scope() as session:
        assert (await MethodService(session).current(subject))[0] == 1
        assert (await MethodService(session).proposal(UUID(proposal['id']))).payload['status'] == 'active'
        # Failed racing transaction does not append a second version or event.
        assert await session.scalar(select(func.count()).select_from(MethodArtifact).where(MethodArtifact.kind == 'version')) == 1
        winner_request = decision if concurrent[0] == winners[0] else decision.model_copy(update={'mutation_id': UUID(winners[0]['decision']['mutation_id'])})
        with pytest.raises(InvestorError, match='操作编号'):
            await MethodService(session).decide(winner_request.model_copy(update={'summary': 'Different body'}), actor='authenticated_user')


@pytest.mark.asyncio
async def test_bounded_newest_workbench_owner_filter_and_exact_detail(conversation_db):
    from datetime import UTC, datetime, timedelta

    from investment_agent.storage.models.investor import MethodProposal
    subject, _, _, review, _, _, _ = await samples()
    original = await proposal_for(subject, review)
    newest = None
    private = uuid4()
    async with session_scope() as session:
        for index in range(25):
            identifier = str(uuid4())
            payload = {**original, 'id': identifier, 'created_at': (datetime.now(UTC) + timedelta(seconds=index)).isoformat()}
            session.add(MethodProposal(id=identifier, owner_id='local_owner', revision=1, payload=payload))
            newest = identifier
        session.add(MethodProposal(id=str(private), owner_id='other_owner', revision=1, payload={**original, 'id': str(private), 'created_at': '2099-01-01T00:00:00Z'}))
        private_review = uuid4()
        session.add(MethodArtifact(id=str(private_review), owner_id='other_owner', kind='process_review', payload={**review, 'id': str(private_review)}))
    app = create_app()
    app.dependency_overrides[get_bootstrap_settings] = lambda: base()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver', headers={'X-API-Key': 'fixture-app'}) as client:
        page = (await client.get('/investor/methods')).json()
        assert len(page['proposals']) == 20 and page['page']['truncated'] and page['page']['next_offset'] == 20
        assert page['proposals'][0]['proposal']['id'] == newest
        assert not any(p['proposal']['id'] == str(private) for p in page['proposals'])
        assert not any('evaluations' in p for p in page['proposals'])
        assert not any('admission' in r['inputs'] for r in page['reviews'])
        second = (await client.get('/investor/methods?offset=20')).json()
        assert len(second['proposals']) == 6 and not second['page']['truncated']
        assert (await client.get('/investor/methods?limit=100')).status_code == 422
        detail = (await client.get('/investor/methods/proposals/' + newest)).json()
        assert detail['detail_loaded'] and detail['evidence_hash'] and detail['approval_summary']
        assert (await client.get('/investor/methods/proposals/' + original['id'] + '/history')).json()['items']
        assert (await client.get('/investor/methods/proposals/' + str(private) + '/history')).status_code == 404
        assert (await client.get('/investor/methods/task-bindings/' + str(uuid4()))).status_code == 404
        assert (await client.get('/investor/methods/proposals/' + str(private))).status_code == 404
        assert (await client.get('/investor/methods/artifacts/process_review/' + str(private_review))).status_code == 404

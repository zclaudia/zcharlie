"""AG30 actual native outputs, file SQLite history, scoped user approval."""
import asyncio
import hashlib
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
from investment_agent.agent.method_receipt import VALIDATOR_V1, build_receipt, render_method
from investment_agent.agent.research import ResearchOutput, validate_progress
from investment_agent.agent.tasks import fingerprint
from investment_agent.api.app import create_app
from investment_agent.api.auth import get_bootstrap_settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.method_packages import (
    LEGACY_BASELINE_DEFINITION,
    CalculationRequirement,
    DimensionOutputRequirement,
    HypothesesRequirement,
    MethodStep,
    canonical_hash,
)
from investment_agent.investor.methods import (
    ActivateGlobal,
    CandidateShadowResultRequest,
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
    task_method_receipt,
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


def gap_step():
    """One blocking expectations-gap step: a strict §8 enhancement of the baseline."""
    return MethodStep(id="expectations_gap", title="Expectations gap", instruction_kind="expectations_gap", blocking=True, required_outputs=(CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"), DimensionOutputRequirement(type="dimension", dimension="price_expectations", minimum_status="qualitative")))


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


@pytest.mark.asyncio
async def test_global_package_default_head_freezes_v2_receipts_and_keeps_v1_tasks(conversation_db):
    subject, failure_task, _, _, _, _, _ = await samples()
    async with session_scope() as session:
        svc = MethodService(session)
        saved = await svc.save_package(LEGACY_BASELINE_DEFINITION, source='sync')
        package_ref = ObjectRef(kind='package', id=UUID(saved['artifact_id']), revision=saved['version'])
        await svc.activate_global(ActivateGlobal(mutation_id=uuid4(), package_ref=package_ref, expected_revision=0, reason='Enable the reviewed baseline package'), actor='authenticated_user')
    receipt = await submit(await new_conversation(), message(text='深入研究', context_refs=(subject,), research_kind='company_research'))
    async with session_scope() as session:
        binding = await task_method(session, receipt.task.id)
        frozen = await task_method_receipt(session, receipt.task.id)
        assert binding.schema_version == 2 and binding.package_ref == package_ref
        assert binding.package_hash == saved['content_hash']
        assert frozen is not None and frozen.binding == binding
        # Card 07: new v2 tasks freeze as enforcing (validator v1).
        assert frozen.renderer_version == '2' and frozen.validator_version == '1'
        assert 'Step falsifiable_plan' in frozen.rendered_method
        # The baseline package adds no capability requirement over the frozen grant.
        assert frozen.tool_family_members == {}
        # The pre-activation task stays exactly v1: no receipt ever existed.
        assert await task_method_receipt(session, failure_task.id) is None
        assert (await task_method(session, failure_task.id)).method_ref == FIXED_METHOD


@pytest.mark.asyncio
async def test_package_proposal_freezes_candidate_and_replays(conversation_db):
    subject, _, _, failure, _, _, _ = await samples()
    candidate = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (*LEGACY_BASELINE_DEFINITION.steps, gap_step())})
    request = CreateProposal(mutation_id=uuid4(), review_id=UUID(failure["id"]), listing_ref=subject, old_method=FIXED_METHOD, error_pattern="Price view moved without an expectations gap", summary="Require reverse DCF anchored gap before directional price views", config=candidate.discipline, package=candidate)
    async with session_scope() as session:
        svc = MethodService(session)
        proposal = await svc.create(request)
        frozen = proposal["request"]
        assert frozen["old_package_kind"] == "legacy"
        assert frozen["content_hash"] == canonical_hash(candidate)
        assert frozen["rendered_method_hash"] == hashlib.sha256(render_method(candidate).encode("utf-8")).hexdigest()
        stored = await svc.package_by_ref(ObjectRef.model_validate(frozen["package_ref"]))
        assert stored["source"] == "proposal" and stored["proposal_id"] == proposal["id"] and stored["evidence_hash"] is None
        assert stored["content_hash"] == frozen["content_hash"]
        # Duplicate create walks the existing replay idempotency.
        assert await svc.create(request) == proposal
    async with session_scope() as session:
        # revise never rewrites the frozen candidate fields.
        svc = MethodService(session)
        row = await svc.proposal(UUID(proposal["id"]))
        await svc.revise(row, row.revision, {"status": "rejected"})
        assert (await svc.proposal(UUID(proposal["id"]))).payload["request"]["package_ref"] == frozen["package_ref"]


@pytest.mark.asyncio
async def test_package_proposal_rejects_config_mismatch_relaxation_and_no_enhancement(conversation_db):
    subject, _, _, failure, _, _, _ = await samples()
    candidate = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (*LEGACY_BASELINE_DEFINITION.steps, gap_step())})
    def request(config, package):
        return CreateProposal(mutation_id=uuid4(), review_id=UUID(failure["id"]), listing_ref=subject, old_method=FIXED_METHOD, error_pattern="Price view moved without an expectations gap", summary="Require reverse DCF anchored gap before directional price views", config=config, package=package)
    async with session_scope() as session:
        svc = MethodService(session)
        # config must equal package.discipline field by field.
        with pytest.raises(InvestorError, match="不一致"):
            await svc.create(request(MethodConfig(require_two_counter_hypotheses=True), candidate))
        # Removing a baseline step breaks the §8 strengthening order.
        removed = candidate.model_copy(update={"steps": tuple(step for step in candidate.steps if step.id != "dimension_assessment")})
        with pytest.raises(InvestorError, match="加强约束"):
            await svc.create(request(candidate.discipline, removed))
        # Presentation-only changes strengthen nothing (§8).
        with pytest.raises(InvestorError, match="无实质增强"):
            await svc.create(request(candidate.discipline, LEGACY_BASELINE_DEFINITION.model_copy(update={"description": "Same requirements, new words"})))
        # config-only proposals keep the existing path: no frozen candidate keys.
        config_only = await svc.create(request(MethodConfig(require_two_counter_hypotheses=True), None))
        assert not {"package_ref", "content_hash", "rendered_method_hash", "old_package_kind"} & set(config_only["request"])


@pytest.mark.asyncio
async def test_package_proposal_against_v2_version_compares_the_stored_package(conversation_db):
    from investment_agent.investor.methods import MethodBinding
    subject, failure_task, _, _, _, _, _ = await samples()
    def with_counter(blocking):
        return LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": tuple(step.model_copy(update={"blocking": blocking}) if step.id == "counter_analysis" else step for step in LEGACY_BASELINE_DEFINITION.steps)})
    stored_old = with_counter(True)
    async with session_scope() as session:
        svc = MethodService(session)
        saved = await svc.save_package(stored_old, source="sync")
        package_ref = ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"])
        # Freeze the historical task as a v2 method version backed by the stored
        # package (frozen rows are immutable: re-freeze deletes and re-inserts).
        binding = MethodBinding(schema_version=2, method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), config=stored_old.discipline, listing_ref=subject, package_ref=package_ref, package_hash=saved["content_hash"])
        receipt = build_receipt(binding, stored_old, validator_version=VALIDATOR_V1)
        await session.execute(delete(TaskMethodBinding).where(TaskMethodBinding.task_id == str(failure_task.id)))
        session.add(TaskMethodBinding(task_id=str(failure_task.id), payload={"schema_version": 2, "receipt": receipt.model_dump(mode="json")}))
        # The frozen review dedupes on inputs (old_method excluded), so the
        # re-frozen binding needs its cached process_review cleared too.
        await session.execute(delete(MethodArtifact).where(MethodArtifact.kind == "process_review", MethodArtifact.payload["task_id"].as_string() == str(failure_task.id)))
    async with session_scope() as session:
        svc = MethodService(session)
        review = await svc.process_review(failure_task.id)
        old = MethodBinding.model_validate(review["old_method"])
        assert old.package_ref == package_ref
        legal = stored_old.model_copy(update={"steps": (*stored_old.steps, gap_step())})
        request = CreateProposal(mutation_id=uuid4(), review_id=UUID(review["id"]), listing_ref=subject, old_method=old.method_ref, error_pattern="Price view moved without an expectations gap", summary="Require reverse DCF anchored gap before directional price views", config=legal.discipline, package=legal)
        proposal = await svc.create(request)
        assert proposal["request"]["old_package_kind"] == "package"
        assert (await svc.package_by_ref(ObjectRef.model_validate(proposal["request"]["package_ref"])))["content_hash"] == canonical_hash(legal)
        # Demoting the stored package's blocking step violates §8 against that
        # stored artifact, even though it would be legal against the virtual
        # legacy baseline: the comparison reads the package artifact.
        relaxed = with_counter(False).model_copy(update={"steps": (*with_counter(False).steps, gap_step())})
        with pytest.raises(InvestorError, match="加强约束"):
            await svc.create(request.model_copy(update={"mutation_id": uuid4(), "package": relaxed}))


# --------------------------------------------------------------------------- #
# Card 13: three-state historical replay inside evaluate (§7.1)
# --------------------------------------------------------------------------- #


def dimension_gap_candidate():
    """Baseline plus one blocking expectations-gap step demanding a raised dimension."""
    return LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (*LEGACY_BASELINE_DEFINITION.steps, MethodStep(id="expectations_gap", title="Expectations gap", instruction_kind="expectations_gap", blocking=True, depends_on=("dimension_assessment",), required_outputs=(DimensionOutputRequirement(type="dimension", dimension="price_expectations", minimum_status="qualitative"),)))})


def hypotheses_raise_candidate(min_count=2):
    """Baseline with the falsifiable-plan hypothesis minimum tightened."""
    return LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": tuple(
        step.model_copy(update={"required_outputs": tuple(
            requirement.model_copy(update={"min_count": min_count}) if isinstance(requirement, HypothesesRequirement) else requirement
            for requirement in step.required_outputs)})
        if step.id == "falsifiable_plan" else step for step in LEGACY_BASELINE_DEFINITION.steps)})


def dimension_evidenced_candidate():
    """Baseline with the price_expectations minimum raised to evidenced."""
    return LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": tuple(
        step.model_copy(update={"required_outputs": tuple(
            requirement.model_copy(update={"minimum_status": "evidenced"})
            if isinstance(requirement, DimensionOutputRequirement) and requirement.dimension == "price_expectations" else requirement
            for requirement in step.required_outputs)})
        if step.id == "dimension_assessment" else step for step in LEGACY_BASELINE_DEFINITION.steps)})


def depends_on_candidate():
    """Baseline with a newly required dependency edge between existing blocking steps."""
    return LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": tuple(
        step.model_copy(update={"depends_on": ("falsifiable_plan",)}) if step.id == "dimension_assessment" else step
        for step in LEGACY_BASELINE_DEFINITION.steps)})


def package_request(mutation, review, subject, package):
    return CreateProposal(mutation_id=mutation, review_id=UUID(review["id"]), listing_ref=subject, old_method=FIXED_METHOD, error_pattern="Price view moved without an expectations gap", summary="Require the expectations gap before directional price views", config=package.discipline, package=package)


async def explanation_review():
    """A real completed out-of-scope explanation task's frozen review."""
    outside_task = await submit(await new_conversation(), message(text="解释经营研究方法", research_kind="explanation"))
    outside_task = await run(outside_task, FunctionModel(lambda m, i: response(ResearchOutput(progress=progress()))))
    async with session_scope() as session:
        return await MethodService(session).process_review(outside_task.id)


async def raised_success_review(candidate, evidence, subject, weak):
    """One real succeeded research task whose price_expectations reaches qualitative."""
    from investment_agent.agent.research_publication import CompanyViewInput, SemanticInput
    from investment_agent.investor.contracts import DimensionAssessment
    from investment_agent.investor.publication import PublicationCandidate, SemanticAssessment
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,)))
    raised = weak.model_copy(update={
        "hypotheses": (*weak.hypotheses, weak.hypotheses[0].model_copy(update={"alternative": "Demand may temporarily exceed sustainable capacity"})),
        "dimensions": tuple(DimensionAssessment(dimension=d.dimension, status="qualitative" if d.dimension == "price_expectations" else d.status, applicable=d.applicable, reason=d.reason, refs=d.refs) for d in weak.dimensions),
        "answered": True})
    business = CompanyViewInput(case_ref=candidate.thesis.case_ref, question_scope="Bounded research", change_reason="Initial research", assessed_dimensions=candidate.thesis.assessed_dimensions, claims=candidate.thesis.claims, counter_case=candidate.thesis.counter_case, gaps=candidate.recommendation.gaps, dependencies=candidate.recommendation.dependency_refs, business_view="undetermined", price_view="undetermined", sufficiency="bounded")
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "save_research_progress" for p in returns):
            return ModelResponse([ToolCallPart("save_research_progress", {"business": raised.model_dump(mode="json")})])
        if not any(p.tool_name == "prepare_company_view" for p in returns):
            return ModelResponse([ToolCallPart("prepare_company_view", {"business": business.model_dump(mode="json")})])
        prepared = next(p.content for p in returns if p.tool_name == "prepare_company_view")
        exact = PublicationCandidate.model_validate(prepared["candidate"])
        semantic = SemanticInput(preparation_ref=ObjectRef.model_validate(prepared["preparation_ref"]), assessments=tuple(SemanticAssessment(claim_id=c.id, fact_relation="Exact saved cash supports this limited observation", counter_evidence_relevance="Temporary working capital remains possible", materiality_reason="Cash persistence affects the observation") for c in exact.thesis.claims), checked_sources=(evidence,), unresolved_questions=(), conclusion="pass")
        return response(ResearchOutput(progress=raised, semantic_review=semantic))
    success = await run(receipt, FunctionModel(model))
    assert success.status.value == "succeeded"
    async with session_scope() as session:
        return await MethodService(session).process_review(success.id)


async def two_hypothesis_failure(subject, weak):
    """A real failed research task that happens to satisfy a two-hypothesis minimum."""
    two = weak.model_copy(update={"hypotheses": (*weak.hypotheses, weak.hypotheses[0].model_copy(update={"alternative": "Sustainable demand may be temporarily overstated"}))})
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,)))
    failed = await run(receipt, FunctionModel(lambda m, i: response(ResearchOutput(progress=two))))
    assert failed.status.value == "partial"
    async with session_scope() as session:
        return await MethodService(session).process_review(receipt.task.id)


async def evaluate_package(proposal, failure, in_scope, outside):
    request = EvaluateProposal(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), expected_revision=proposal["revision"], cases=(EvaluationCase(review_id=UUID(failure["id"]), role="failure", expected="block"), EvaluationCase(review_id=UUID(in_scope["id"]), role="counterexample", expected="pass"), EvaluationCase(review_id=UUID(outside["id"]), role="counterexample", expected="pass")))
    async with session_scope() as session:
        return await MethodService(session).evaluate(request)


async def replay_samples():
    """The fixture scenario plus one succeeded task reaching price_expectations qualitative."""
    candidate, evidence, subject = await fixture()
    weak = full_progress(candidate.thesis.assessed_dimensions, fact_refs=(evidence,), answered=False)
    failure_receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,)))
    failure = await run(failure_receipt, FunctionModel(lambda m, i: response(ResearchOutput(progress=weak))))
    assert failure.status.value == "partial"
    async with session_scope() as session:
        failure_review = await MethodService(session).process_review(failure_receipt.task.id)
    raised = await raised_success_review(candidate, evidence, subject, weak)
    return candidate, evidence, subject, weak, failure_review, raised


@pytest.mark.asyncio
async def test_package_evaluate_replays_three_states_and_records_shadow_set(conversation_db):
    _, _, subject, _, failure, raised = await replay_samples()
    candidate = dimension_gap_candidate()
    async with session_scope() as session:
        proposal = await MethodService(session).create(package_request(uuid4(), failure, subject, candidate))
    outside = await explanation_review()
    result = await evaluate_package(proposal, failure, raised, outside)
    evaluation = result["evaluation"]
    assert evaluation["passed"]
    failure_case = next(c for c in evaluation["cases"] if c["role"] == "failure")
    in_scope = next(c for c in evaluation["cases"] if c["role"] == "counterexample" and c["applicable"])
    outside_case = next(c for c in evaluation["cases"] if c["role"] == "counterexample" and not c["applicable"])
    # The failure history misses the raised dimension: a replayable block whose
    # ids intersect the §8 strengthening order - valid failure evidence.
    assert failure_case["proposed"]["state"] == "block"
    assert failure_case["proposed"]["replayed"]["outputs:expectations_gap:dimension:price_expectations"] == "block"
    assert "dimension_min:price_expectations" in failure_case["proposed"]["blocked_requirements"]
    assert failure_case["baseline"]["result"] == "pass"
    # The in-scope success reaches the dimension but cannot prove the new
    # blocking step: not_replayable - its structural ids feed the prospective
    # candidate-shadow coverage instead of failing the evaluation (§7.1).
    assert in_scope["proposed"]["state"] == "not_replayable"
    assert in_scope["proposed"]["replayed"]["outputs:expectations_gap:dimension:price_expectations"] == "pass"
    assert in_scope["proposed"]["replayed"]["blocking:expectations_gap"] == "not_replayable"
    assert "blocking:expectations_gap" in in_scope["proposed"]["requires_candidate_shadow"]
    assert in_scope["baseline"]["result"] == "pass"
    assert outside_case["proposed"]["result"] == "pass"
    proposal = result["proposal"]
    assert proposal["status"] == "shadow"
    assert "blocking:expectations_gap" in proposal["requires_candidate_shadow"]
    assert "results:expectations_gap" in proposal["requires_candidate_shadow"]
    assert "depends_on:expectations_gap:dimension_assessment" in proposal["requires_candidate_shadow"]


@pytest.mark.asyncio
async def test_package_evaluate_failure_block_requires_substantive_requirement(conversation_db):
    subject, _, _, failure, counter, _, _ = await samples()
    candidate = hypotheses_raise_candidate()
    async with session_scope() as session:
        proposal = await MethodService(session).create(package_request(uuid4(), failure, subject, candidate))
    outside = await explanation_review()
    result = await evaluate_package(proposal, failure, counter, outside)
    evaluation = result["evaluation"]
    assert evaluation["passed"]
    failure_case = next(c for c in evaluation["cases"] if c["role"] == "failure")
    in_scope = next(c for c in evaluation["cases"] if c["role"] == "counterexample" and c["applicable"])
    # One hypothesis against a two-hypothesis minimum: a replayable block on a
    # requirement the candidate strictly tightens.
    assert failure_case["proposed"]["state"] == "block"
    assert failure_case["proposed"]["blocked_requirements"] == ["outputs:falsifiable_plan:hypotheses", "outputs:falsifiable_plan:hypotheses:min_count"]
    assert failure_case["baseline"]["result"] == "pass"
    # The success history satisfies the tightened requirement; the unchanged
    # blocking structure stays not_replayable and never blocks.
    assert in_scope["proposed"]["state"] == "not_replayable"
    assert in_scope["baseline"]["result"] == "pass"
    proposal = result["proposal"]
    assert proposal["status"] == "shadow"
    assert "blocking:falsifiable_plan" in proposal["requires_candidate_shadow"]


@pytest.mark.asyncio
async def test_package_evaluate_rejects_non_substantive_failure_evidence(conversation_db):
    subject, _, _, _, counter, weak, _ = await samples()
    failure = await two_hypothesis_failure(subject, weak)
    candidate = hypotheses_raise_candidate()
    async with session_scope() as session:
        proposal = await MethodService(session).create(package_request(uuid4(), failure, subject, candidate))
    outside = await explanation_review()
    result = await evaluate_package(proposal, failure, counter, outside)
    evaluation = result["evaluation"]
    assert not evaluation["passed"]
    failure_case = next(c for c in evaluation["cases"] if c["role"] == "failure")
    # The failed task happens to satisfy the strengthened requirement, so no
    # replayable requirement blocks: mere not-replayable structure is not valid
    # failure evidence and the evaluation fails (§7.1).
    assert failure_case["proposed"]["state"] == "not_replayable"
    assert "blocking:falsifiable_plan" in failure_case["proposed"]["requires_candidate_shadow"]
    assert result["proposal"]["status"] == "evaluating"
    assert result["proposal"]["requires_candidate_shadow"]


@pytest.mark.asyncio
async def test_package_evaluate_counterexample_block_fails_evaluation(conversation_db):
    subject, _, _, failure, counter, _, _ = await samples()
    candidate = dimension_evidenced_candidate()
    async with session_scope() as session:
        proposal = await MethodService(session).create(package_request(uuid4(), failure, subject, candidate))
    outside = await explanation_review()
    result = await evaluate_package(proposal, failure, counter, outside)
    evaluation = result["evaluation"]
    assert not evaluation["passed"]
    in_scope = next(c for c in evaluation["cases"] if c["role"] == "counterexample" and c["applicable"])
    # The succeeded task's price_expectations is missing: the candidate would
    # block a good task, so the evaluation fails.
    assert in_scope["proposed"]["state"] == "block"
    assert in_scope["proposed"]["replayed"]["outputs:dimension_assessment:dimension:price_expectations"] == "block"
    assert result["proposal"]["status"] == "evaluating"


@pytest.mark.asyncio
async def test_package_evaluate_dependency_order_is_not_replayable(conversation_db):
    subject, _, _, failure, counter, _, _ = await samples()
    candidate = depends_on_candidate()
    async with session_scope() as session:
        proposal = await MethodService(session).create(package_request(uuid4(), failure, subject, candidate))
    outside = await explanation_review()
    result = await evaluate_package(proposal, failure, counter, outside)
    evaluation = result["evaluation"]
    assert not evaluation["passed"]
    in_scope = next(c for c in evaluation["cases"] if c["role"] == "counterexample")
    # Historical v1 actions carry no committed_sequence: the newly required
    # order edge cannot be replayed and joins the candidate-shadow set.
    assert in_scope["proposed"]["state"] == "not_replayable"
    assert in_scope["proposed"]["replayed"]["depends_on:dimension_assessment:falsifiable_plan"] == "not_replayable"
    assert "depends_on:dimension_assessment:falsifiable_plan" in result["proposal"]["requires_candidate_shadow"]


# --------------------------------------------------------------------------- #
# Card 16: package approval gates, v2 head binding and v2 rollback (§7.4)
# --------------------------------------------------------------------------- #


def raise_hypotheses(base, min_count=2):
    """Generic §8 enhancement: tighten the falsifiable-plan hypothesis minimum."""
    return base.model_copy(update={"steps": tuple(
        step.model_copy(update={"required_outputs": tuple(
            requirement.model_copy(update={"min_count": min_count}) if isinstance(requirement, HypothesesRequirement) else requirement
            for requirement in step.required_outputs)})
        if step.id == "falsifiable_plan" else step for step in base.steps)})


async def register_candidate_shadow(proposal, weak, expected):
    """One real candidate-package prospective shadow run and its registered
    result (§7.3); the proposal payload it returns carries the new revision."""
    from tests.unit.test_candidate_shadow import (
        release_fixture_budget,
        run_shadow,
        shadow_block_model,
        shadow_pass_model,
        shadow_plan,
        shadow_settings,
        start_shadow,
    )
    started = await start_shadow(proposal["id"], expected=expected)
    task_id = UUID(started["task_id"])
    plan = shadow_plan(weak)
    await release_fixture_budget()
    if expected == "pass":
        finished = await run_shadow(task_id, FunctionModel(shadow_pass_model(plan)))
    else:
        finished = await run_shadow(task_id, FunctionModel(shadow_block_model(plan)), shadow_settings(agent_retries=0))
    assert finished.status.value == ("succeeded" if expected == "pass" else "partial")
    async with session_scope() as session:
        return await MethodService(session).candidate_shadow_result(CandidateShadowResultRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), task_id=task_id, expected_revision=proposal["revision"]))


@pytest.mark.asyncio
async def test_package_approval_binds_v2_head_and_freezes_candidate_receipts(conversation_db):
    from investment_agent.storage.models.investor import MethodDefaultHead, MethodHead
    from tests.unit.test_candidate_shadow import shadow_proposal, shadow_ready_mandate
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    # Without any prospective candidate shadow the approval is refused, naming
    # the exact open requirements (§7.4); a stale evidence_hash keeps fencing.
    premature = await decision_for(proposal, subject)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="blocking:expectations_gap"):
            await MethodService(session).decide(premature, actor="authenticated_user")
        with pytest.raises(InvestorError, match="冻结证据"):
            await MethodService(session).decide(premature.model_copy(update={"evidence_hash": "0" * 64}), actor="authenticated_user")
    blocked = await register_candidate_shadow(proposal, weak, expected="block")
    assert blocked["candidate_shadow"]["matched"] and blocked["candidate_shadow"]["outcome"] == "blocked"
    block_only = await decision_for(blocked["proposal"], subject)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="未覆盖全部待影子要求"):
            await MethodService(session).decide(block_only, actor="authenticated_user")
    passed = await register_candidate_shadow(blocked["proposal"], weak, expected="pass")
    assert passed["candidate_shadow"]["matched"]
    assert set(passed["candidate_shadow"]["requirement_ids"]) == set(passed["proposal"]["requires_candidate_shadow"])
    async with session_scope() as session:
        svc = MethodService(session)
        evidence = await svc.evidence(await svc.proposal(UUID(passed["proposal"]["id"])))
        shadow_artifact = await svc.artifact(UUID(passed["candidate_shadow"]["id"]), "candidate_shadow")
    # The evidence binds the candidate side: frozen package/render hashes,
    # renderer/validator versions, last evaluation and latest prospective
    # shadow by exact id + fingerprint.
    bound = evidence["candidate_binding"]
    assert bound["package_ref"] == passed["proposal"]["request"]["package_ref"]
    assert bound["content_hash"] == passed["proposal"]["request"]["content_hash"]
    assert bound["rendered_method_hash"] == passed["proposal"]["request"]["rendered_method_hash"]
    assert bound["renderer_version"] == "2" and bound["validator_version"] == VALIDATOR_V1
    assert bound["evaluation_id"] == passed["proposal"]["evaluation_ids"][-1]
    assert bound["evaluation_hash"] == evidence["artifact_hashes"][bound["evaluation_id"]]
    assert bound["candidate_shadow_id"] == passed["candidate_shadow"]["id"]
    assert bound["candidate_shadow_hash"] == fingerprint(shadow_artifact)
    decision = await decision_for(passed["proposal"], subject)
    async with session_scope() as session:
        svc = MethodService(session)
        assert (await session.scalar(select(func.count()).select_from(MethodDefaultHead))) == 0
        assert (await session.scalar(select(func.count()).select_from(MethodHead))) == 0
        active = await svc.decide(decision, actor="authenticated_user")
        assert await svc.decide(decision, actor="authenticated_user") == active
        revision, head = await svc.current(subject)
        assert revision == 1
        # The head binds the proposal-frozen package as a v2 method version.
        assert head.schema_version == 2
        assert head.method_ref == ObjectRef.model_validate(active["method_ref"])
        assert head.package_ref == ObjectRef.model_validate(passed["proposal"]["request"]["package_ref"])
        assert head.package_hash == passed["proposal"]["request"]["content_hash"]
        assert head.config == MethodConfig.model_validate(passed["proposal"]["request"]["config"])
        # Approval moves only the per-listing head; the global default head
        # stays untouched (§5: 全局默认只经 activate-global).
        assert (await session.scalar(select(func.count()).select_from(MethodDefaultHead))) == 0
        stored = await svc.package_by_ref(head.package_ref)
        assert stored["artifact_id"] == str(head.package_ref.id) and stored["content_hash"] == head.package_hash
        events = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "event"))).all()
        approved_event = next(item.payload for item in events if item.payload["operation"] == "approve" and str(item.payload["proposal_id"]) == str(active["id"]))
        assert approved_event["candidate"]["package_hash"] == head.package_hash
        assert approved_event["candidate"]["rendered_method_hash"] == passed["proposal"]["request"]["rendered_method_hash"]
        assert approved_event["candidate"]["evaluation_id"] == bound["evaluation_id"]
        assert approved_event["candidate"]["candidate_shadow_id"] == bound["candidate_shadow_id"]
    # A new per-listing task freezes the approved v2 receipt; the package
    # content is exactly the proposal-frozen candidate (card 04 four checks).
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        binding = await task_method(session, receipt.task.id)
        frozen = await task_method_receipt(session, receipt.task.id)
        assert binding == head
        assert frozen is not None and frozen.binding == binding and frozen.shadow is None
        assert binding.schema_version == 2 and binding.package_ref == head.package_ref
        assert binding.package_hash == head.package_hash
        assert frozen.renderer_version == "2" and frozen.validator_version == VALIDATOR_V1
        assert frozen.rendered_method == render_method(dimension_gap_candidate())
        assert frozen.rendered_method_hash == passed["proposal"]["request"]["rendered_method_hash"]
        assert frozen.tool_family_members == {}


@pytest.mark.asyncio
async def test_package_approval_refuses_without_block_counterexample(conversation_db):
    from tests.unit.test_candidate_shadow import shadow_proposal, shadow_ready_mandate
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    passed = await register_candidate_shadow(proposal, weak, expected="pass")
    assert passed["candidate_shadow"]["matched"]
    # Full coverage alone never approves: one expected-block candidate task
    # must have been rejected by the package-level validation (§7.3).
    positive_only = await decision_for(passed["proposal"], subject)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="预期失败候选影子"):
            await MethodService(session).decide(positive_only, actor="authenticated_user")
        assert (await MethodService(session).current(subject))[0] == 0
    blocked = await register_candidate_shadow(passed["proposal"], weak, expected="block")
    assert blocked["candidate_shadow"]["matched"]
    # The newest block run is the bound evidence and covers nothing.
    stale = await decision_for(blocked["proposal"], subject)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="未覆盖全部待影子要求"):
            await MethodService(session).decide(stale, actor="authenticated_user")
        assert (await MethodService(session).current(subject))[0] == 0


@pytest.mark.asyncio
async def test_package_rollback_copies_v2_package_and_restores_v1_baseline(conversation_db):
    from datetime import UTC, datetime

    from investment_agent.investor.methods import MethodBinding
    from investment_agent.storage.models.reference import Listing
    from tests.unit.test_candidate_shadow import shadow_proposal, shadow_ready_mandate
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    blocked = await register_candidate_shadow(proposal, weak, expected="block")
    passed = await register_candidate_shadow(blocked["proposal"], weak, expected="pass")
    decision = await decision_for(passed["proposal"], subject)
    async with session_scope() as session:
        svc = MethodService(session)
        approved = await svc.decide(decision, actor="authenticated_user")
        binding_a = (await svc.current(subject))[1]
        # known() reads a v2 method version with its package fields intact.
        assert await svc.known(ObjectRef.model_validate(approved["method_ref"])) == binding_a
    task_a = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    # A fixture-strengthened v2 version takes the head, making the approved v2
    # version the rollback target. Known scoped fixtures isolate the rollback
    # mechanics; the actual approval chain is pinned by the end-to-end test
    # above (the same precedent as the scoped native-phase fixtures).
    async with session_scope() as session:
        svc = MethodService(session)
        saved_b = await svc.save_package(raise_hypotheses(dimension_gap_candidate()), source="fixture")
        binding_b = MethodBinding(schema_version=2, method_ref=ObjectRef(kind="method", id=uuid4(), revision=1), config=MethodConfig.model_validate(saved_b["discipline"]), listing_ref=subject, package_ref=ObjectRef(kind="package", id=UUID(saved_b["artifact_id"]), revision=saved_b["version"]), package_hash=saved_b["content_hash"])
        session.add(MethodArtifact(id=str(binding_b.method_ref.id), owner_id="local_owner", kind="version", payload={"id": str(binding_b.method_ref.id), "created_at": datetime.now(UTC).isoformat(), "binding": binding_b.model_dump(mode="json")}))
        await svc.move_head(subject, 1, binding_b)
    task_b = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        frozen = {}
        for task in (task_a.task, task_b.task):
            method, receipt = await task_method(session, task.id), await task_method_receipt(session, task.id)
            frozen[str(task.id)] = (method.model_dump(mode="json"), receipt.model_dump(mode="json") if receipt else None)
        assert frozen[str(task_a.task.id)][0]["method_ref"] == binding_a.method_ref.model_dump(mode="json")
        assert frozen[str(task_b.task.id)][0]["package_ref"] == binding_b.package_ref.model_dump(mode="json")
    # v2 -> earlier v2: one appended version copying the target binding
    # verbatim, package fields included, under a fresh method_ref.
    async with session_scope() as session:
        svc = MethodService(session)
        before = await session.scalar(select(func.count()).select_from(MethodArtifact).where(MethodArtifact.kind == "version"))
        request = MethodRollback(mutation_id=uuid4(), listing_ref=subject, expected_head_revision=2, target_method=ObjectRef.model_validate(approved["method_ref"]), reason="Restore the approved gap package")
        result = await svc.rollback(request, actor="authenticated_user")
        assert await svc.rollback(request, actor="authenticated_user") == result
        assert await session.scalar(select(func.count()).select_from(MethodArtifact).where(MethodArtifact.kind == "version")) == before + 1
        _, head = await svc.current(subject)
        assert head.schema_version == 2 and head.package_ref == binding_a.package_ref
        assert head.package_hash == binding_a.package_hash and head.config == binding_a.config
        assert head.method_ref == ObjectRef(kind="method", id=UUID(result["id"]), revision=1)
        assert head.method_ref != binding_a.method_ref
        assert result["binding"]["package_ref"] == binding_a.package_ref.model_dump(mode="json")
        assert result["previous"]["method_ref"] == binding_b.method_ref.model_dump(mode="json")
        assert await svc.known(head.method_ref) == head
    restored = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        binding_restored = await task_method(session, restored.task.id)
        frozen_restored = await task_method_receipt(session, restored.task.id)
        assert binding_restored.schema_version == 2 and binding_restored.package_ref == binding_a.package_ref
        assert binding_restored.package_hash == binding_a.package_hash and binding_restored.method_ref == head.method_ref
        assert frozen_restored is not None and frozen_restored.binding == binding_restored and frozen_restored.shadow is None
        # Historical tasks keep their exact approved method marks and receipts.
        assert await task_method(session, task_a.task.id) == binding_a
        assert await task_method(session, task_b.task.id) == binding_b
        assert (await task_method_receipt(session, task_a.task.id)).model_dump(mode="json") == frozen[str(task_a.task.id)][1]
        assert (await task_method_receipt(session, task_b.task.id)).model_dump(mode="json") == frozen[str(task_b.task.id)][1]
    # The target.listing_ref consistency check also holds for v2 targets.
    async with session_scope() as session:
        other = Listing(company_id=(await session.get(Listing, subject.id)).company_id, market_code="SH", ticker="600001.SH", is_primary=False)
        session.add(other)
        await session.flush()
        other_ref = ObjectRef(kind="listing", id=other.id, revision=1)
    wrong = MethodRollback(mutation_id=uuid4(), listing_ref=other_ref, expected_head_revision=3, target_method=ObjectRef.model_validate(approved["method_ref"]), reason="Wrong scope")
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="其他证券"):
            await MethodService(session).rollback(wrong, actor="authenticated_user")
    # v2 -> v1 baseline: one appended v1 version; new tasks run the plain
    # baseline without a receipt, historical marks stay untouched.
    baseline_request = MethodRollback(mutation_id=uuid4(), listing_ref=subject, expected_head_revision=3, target_method=FIXED_METHOD, reason="Restore the v1 baseline")
    async with session_scope() as session:
        svc = MethodService(session)
        before = await session.scalar(select(func.count()).select_from(MethodArtifact).where(MethodArtifact.kind == "version"))
        result = await svc.rollback(baseline_request, actor="authenticated_user")
        assert await svc.rollback(baseline_request, actor="authenticated_user") == result
        assert await session.scalar(select(func.count()).select_from(MethodArtifact).where(MethodArtifact.kind == "version")) == before + 1
        head = await svc.known(ObjectRef(kind="method", id=UUID(result["id"]), revision=1))
        assert head.schema_version == 1 and head.package_ref is None and head.package_hash is None
        assert head.method_ref != FIXED_METHOD and not head.config.require_two_counter_hypotheses
        assert head.listing_ref == subject
        assert result["previous"]["package_ref"] == binding_a.package_ref.model_dump(mode="json")
    baseline_task = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        binding = await task_method(session, baseline_task.task.id)
        assert binding.schema_version == 1 and binding.package_ref is None and binding.method_ref != FIXED_METHOD
        assert await task_method_receipt(session, baseline_task.task.id) is None
        assert await task_method(session, restored.task.id) == binding_restored
        assert await task_method(session, task_a.task.id) == binding_a


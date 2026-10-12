"""AG14 real transactions, exact user authority and revocable context."""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from investment_agent.agent.contracts import ContextBinding, MessageInput
from investment_agent.agent.conversations import ConversationCreate, ConversationService
from investment_agent.agent.execution import (
    ExecutionDeniedError,
    ExecutionGrant,
    admit,
    check_boundary,
)
from investment_agent.agent.tasks import TaskService
from investment_agent.investor.common import Mutation, ObjectRef
from investment_agent.investor.controls import (
    CaseAction,
    DecisionAction,
    ExecuteRequest,
    MandateAction,
    MemoryAction,
    PrepareRequest,
    ResolveRequest,
    RevokeAction,
    UserAuthority,
    UserControlService,
    authenticated_message,
    authenticated_request,
    memory_revision,
)
from investment_agent.investor.enums import CaseStatus, DecisionKind, RecommendationStatus
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.investor.publication import PublicationService
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.agent import ExecutionGrantRow, MessageRow
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.investor import UserObject, UserObjectRevision
from investment_agent.storage.models.user_domain import HoldingItem, WatchlistItem
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.test_investor_publication import base, fixture, publish


@pytest_asyncio.fixture
async def controls_db(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'controls.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


def request(parameters, key=None):
    return ExecuteRequest(parameters=parameters, idempotency_key=key or uuid4())


async def execute(session, parameters, key=None):
    return await UserControlService(session).execute(request(parameters, key), authenticated_request(parameters))


async def case_ref(candidate):
    async with session_scope() as session:
        case = await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)
        return ObjectRef(kind="case", id=case.id, revision=case.revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(DecisionKind))
async def test_decisions_are_exact_history_not_transactions(controls_db, kind):
    candidate, _, _ = await fixture()
    published = await publish(candidate)
    ref = await case_ref(candidate)
    parameters = DecisionAction(case_ref=ref, recommendation_ref=published.recommendation_ref,
        decision=kind, user_text="My explicit decision", reason="Personal reason")
    async with session_scope() as session:
        before = await InvestorService(session).get_case("local_owner", ref.id)
        counts = [await session.scalar(select(func.count()).select_from(model)) for model in (HoldingItem, WatchlistItem)]
        body = request(parameters)
        saved = await UserControlService(session).execute(body, authenticated_request(parameters))
        assert saved.actor == "user" and saved.case_ref == ref and saved.recommendation_ref == published.recommendation_ref
        assert saved.origin_ref.kind == "instruction"
        assert await UserControlService(session).execute(body, authenticated_request(parameters)) == saved
        assert await InvestorService(session).get_case("local_owner", ref.id) == before
        assert counts == [await session.scalar(select(func.count()).select_from(model)) for model in (HoldingItem, WatchlistItem)]
        with pytest.raises(InvestorError, match="different request"):
            await UserControlService(session).execute(body.model_copy(update={"parameters": parameters.model_copy(update={"reason": "Changed"})}), authenticated_request(parameters.model_copy(update={"reason": "Changed"})))
    async with session_scope() as session:
        assert len(await UserControlService(session).list_objects("decision")) == 1


@pytest.mark.asyncio
async def test_archive_withdraws_preserves_pin_and_user_reopen_episode(controls_db):
    candidate, _, _ = await fixture()
    published = await publish(candidate)
    ref = await case_ref(candidate)
    async with session_scope() as session:
        pinned = await InvestorService(session).set_pinned("local_owner", ref.id, True, Mutation(expected_revision=ref.revision, idempotency_key=uuid4()))
        action = CaseAction(case_ref=ref.model_copy(update={"revision": pinned.revision}), action="archive", reason="User finished this research")
        archived = await execute(session, action)
        assert archived.pinned and archived.user_archived and archived.status == CaseStatus.ARCHIVED
        assert archived.current_recommendation_ref is None and archived.current_thesis_ref is None
        assert (await PublicationService(session).get_recommendation(published.recommendation_ref)).status == RecommendationStatus.WITHDRAWN
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="scope_archived"):
            await admit(session, base=base(), input_key=str(uuid4()),
                binding=ContextBinding(context_refs=(), base_refs=(), as_of=datetime.now(UTC)),
                grant=ExecutionGrant(mode="interactive", source="authenticated_message", prompt="Automatic reopen", scope_refs=(ref,)))
        reopened = await execute(session, CaseAction(case_ref=ref.model_copy(update={"revision": archived.revision}), action="reopen", reason="New explicit user research"))
        assert reopened.episode == archived.episode + 1 and not reopened.user_archived and reopened.pinned
        assert (await PublicationService(session).get_recommendation(published.recommendation_ref)).status == RecommendationStatus.WITHDRAWN
        assert len((await InvestorService(session).detail("local_owner", ref.id)).episodes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "mute"])
async def test_pause_and_mute_preserve_current_opinion_interactive_admission(controls_db, action):
    candidate, _, _ = await fixture()
    published = await publish(candidate)
    ref = await case_ref(candidate)
    async with session_scope() as session:
        changed = await execute(session, CaseAction(case_ref=ref, action=action, reason="User control"))
        assert changed.current_recommendation_ref == published.recommendation_ref
        assert (await PublicationService(session).get_recommendation(published.recommendation_ref)).status == RecommendationStatus.PUBLISHED
        task = await admit(session, base=base(), input_key=str(uuid4()),
            binding=ContextBinding(context_refs=(), base_refs=(), as_of=datetime.now(UTC)),
            grant=ExecutionGrant(mode="interactive", source="authenticated_message", prompt="Manual question", scope_refs=(ref,)))
        assert task.id
        restored = await execute(session, CaseAction(case_ref=ref.model_copy(update={"revision": changed.revision}), action="resume" if action == "pause" else "unmute", reason="Resume"))
        assert not restored.background_paused and not restored.muted


@pytest.mark.asyncio
async def test_archive_outer_rollback_restores_opinion_case_and_receipt(controls_db):
    candidate, _, _ = await fixture()
    published = await publish(candidate)
    ref = await case_ref(candidate)
    action = CaseAction(case_ref=ref, action="archive", reason="Archive")
    body = request(action)
    async with session_scope() as session:
        await UserControlService(session).execute(body, authenticated_request(action))
        await session.rollback()
    async with session_scope() as session:
        current = await InvestorService(session).get_case("local_owner", ref.id)
        assert current.revision == ref.revision and current.current_recommendation_ref == published.recommendation_ref
        assert (await PublicationService(session).get_recommendation(published.recommendation_ref)).status == RecommendationStatus.PUBLISHED
        assert not await UserControlService(session).list_objects("decision")
        assert await session.scalar(select(func.count()).select_from(UserObject)) == 0
        assert (await UserControlService(session).execute(body, authenticated_request(action))).status == CaseStatus.ARCHIVED


@pytest.mark.asyncio
async def test_concurrent_actions_exact_replay_and_case_cas(controls_db):
    candidate, _, _ = await fixture()
    ref = await case_ref(candidate)
    body = request(CaseAction(case_ref=ref, action="pause", reason="Pause"))
    async def operation(req):
        async with session_scope() as session:
            return await UserControlService(session).execute(req, authenticated_request(req.parameters))
    a, b = await asyncio.gather(operation(body), operation(body))
    assert a == b and a.revision == ref.revision + 1
    with pytest.raises(InvestorError, match="revision changed"):
        await operation(request(CaseAction(case_ref=ref, action="mute", reason="Stale CAS")))


@pytest.mark.asyncio
async def test_exact_user_authority_proposals_consumption_params_expiry_and_rollback(controls_db, monkeypatch):
    candidate, _, _ = await fixture()
    ref = await case_ref(candidate)
    action = CaseAction(case_ref=ref, action="pause", reason="Suggested pause")
    with pytest.raises(InvestorError, match="Authenticated"):
        UserAuthority("actor=user", "model", action)
    async with session_scope() as session:
        svc = UserControlService(session)
        with pytest.raises(InvestorError, match="Exact authenticated"):
            await svc.execute(request(action), {"actor": "user"})
        proposal = await svc.prepare(PrepareRequest(parameters=action, idempotency_key=uuid4(), expires_at=datetime.now(UTC) + timedelta(hours=1)))
        assert not (await svc.get_case("local_owner", ref.id)).background_paused
        resolve = ResolveRequest(parameters=action, mutation=Mutation(expected_revision=1, idempotency_key=uuid4()), approve=True, reason="Approve")
        different = action.model_copy(update={"action": "mute"})
        with pytest.raises(InvestorError, match="exact prepared"):
            await svc.resolve(proposal.id, resolve.model_copy(update={"parameters": different}), authenticated_request(different))
        done = await svc.resolve(proposal.id, resolve, authenticated_request(action))
        assert done.status == "approved" and done.authorization_ref.kind == "instruction"
        assert await svc.resolve(proposal.id, resolve, authenticated_request(action)) == done
        with pytest.raises(InvestorError):
            await svc.resolve(proposal.id, resolve.model_copy(update={"mutation": Mutation(expected_revision=2, idempotency_key=uuid4())}), authenticated_request(action))
        await session.rollback()
    async with session_scope() as session:
        assert (await UserControlService(session).get_case("local_owner", ref.id)).revision == ref.revision
        assert not await UserControlService(session).list_objects("proposal")
        # Persist a proposal, then simulate UTC passage with the injected clock
        # instead of a real-time sleep (repo convention, see the memory expiry test).
        proposal = await UserControlService(session).prepare(PrepareRequest(parameters=action, idempotency_key=uuid4(), expires_at=datetime.now(UTC) + timedelta(hours=1)))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(hours=2)
    monkeypatch.setattr("investment_agent.investor.controls.datetime", Clock)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="expired"):
            await UserControlService(session).resolve(proposal.id, resolve, authenticated_request(action))
        assert not (await InvestorService(session).get_case("local_owner", ref.id)).background_paused


@pytest.mark.asyncio
async def test_mandate_proposal_user_approval_preserves_trusted_origin(controls_db):
    await fixture()
    async with session_scope() as session:
        current = await InvestorService(session).get_mandate("local_owner")
        candidate = current.model_copy(update={"revision": current.revision + 1, "goals": ("New explicit goal",), "actor": "model", "origin_ref": ObjectRef(kind="task", id=uuid4(), revision=1)})
        parameters = MandateAction(candidate=candidate, expected_revision=current.revision)
        svc = UserControlService(session)
        proposal = await svc.prepare(PrepareRequest(parameters=parameters, idempotency_key=uuid4(), expires_at=datetime.now(UTC) + timedelta(hours=1)))
        assert (await svc.get_mandate("local_owner")).revision == current.revision
        result = await svc.resolve(proposal.id, ResolveRequest(parameters=parameters, mutation=Mutation(expected_revision=1, idempotency_key=uuid4()), approve=True, reason="My goal"), authenticated_request(parameters))
        mandate = await svc.get_mandate("local_owner")
        assert mandate.origin_ref == result.authorization_ref and mandate.actor == "user"


async def conversation(session):
    svc = ConversationService(session, base())
    chat = await svc.create(ConversationCreate(title="Preference source"))
    accepted = await svc.submit(chat.id, MessageInput(client_message_id=uuid4(), text="I prefer durable cash and low debt", context_refs=()))
    source = ObjectRef(kind="message", id=accepted.message.id, revision=1)
    assistant = accepted.message.model_copy(update={"id": uuid4(), "sequence": 2, "role": "assistant", "text": "Summary: user prefers durable cash", "client_message_id": None})
    session.add(MessageRow(id=str(assistant.id), conversation_id=str(chat.id), client_message_id=None,
        completion_key=None, input_hash="derived", sequence=2, payload=assistant.model_dump(mode="json")))
    await session.flush()
    return chat, source, assistant


@pytest.mark.asyncio
async def test_revocation_filters_source_summary_sibling_and_prevents_reextraction(controls_db):
    async with session_scope() as session:
        chat, source, assistant = await conversation(session)
        svc = UserControlService(session)
        action = MemoryAction(kind="explicit_preference", text="Durable cash", source_refs=(source,))
        authority = await authenticated_message(session, source, action)
        saved = await svc.execute(request(action), authority)
        sibling = await execute(session, MemoryAction(kind="explicit_preference", text="Low debt", source_refs=(source,)))
        before = await svc.context(conversation_id=chat.id)
        assert {item.id for item in before.memories} == {saved.id, sibling.id}
        revoke = RevokeAction(memory_ref=ObjectRef(kind="memory", id=saved.id, revision=1), reason="Forget cash preference")
        revoked = await execute(session, revoke)
        after = await svc.context(conversation_id=chat.id, private_history_revision=before.revision, private_history_stamp=before.stamp)
        assert revoked.status == "revoked" and revoked.revoked_at
        assert not after.memories and not after.messages and not after.private_history_allowed
        assert after.revision == before.revision + 1
        original = await svc._object(ObjectRef(kind="memory", id=saved.id, revision=1), "memory")
        assert original["status"] == "active"
        with pytest.raises(InvestorError, match="Revoked source"):
            await svc.execute(request(action), authenticated_request(action))
        with pytest.raises(InvestorError, match="Old revoked"):
            fresh = MemoryAction(kind="explicit_preference", text="Cash preference")
            await svc.execute(request(fresh), await authenticated_message(session, source, fresh))
        with pytest.raises(InvestorError, match="Model text"):
            await authenticated_message(session, ObjectRef(kind="message", id=assistant.id, revision=1), action)
        # A fresh explicit authenticated request may restore a preference.
        restored = await execute(session, MemoryAction(kind="explicit_preference", text="Durable cash again"))
        assert restored.id != saved.id and (await svc.context()).memories == (restored,)


@pytest.mark.asyncio
async def test_revoke_rolls_back_tombstone_revision_and_filters(controls_db):
    async with session_scope() as session:
        chat, source, _ = await conversation(session)
        saved = await execute(session, MemoryAction(kind="explicit_preference", text="Cash", source_refs=(source,)))
        revision = await memory_revision(session)
    async with session_scope() as session:
        await execute(session, RevokeAction(memory_ref=ObjectRef(kind="memory", id=saved.id, revision=1), reason="Forget"))
        await session.rollback()
    async with session_scope() as session:
        context = await UserControlService(session).context(conversation_id=chat.id)
        assert context.revision == revision and len(context.messages) == 2 and context.memories == (saved,)
        assert await session.scalar(select(func.count()).select_from(UserObjectRevision).where(UserObjectRevision.id == str(saved.id))) == 1


async def running(session, *, tools=(), scope=()):
    task = await admit(session, base=base(), input_key=str(uuid4()), binding=ContextBinding(context_refs=(), base_refs=(), as_of=datetime.now(UTC)),
        grant=ExecutionGrant(mode="interactive", source="authenticated_message", prompt="Research", scope_refs=scope, tool_names=tools, memory_revision=999))
    run = await PipelineRunRepository(session).claim_next(worker_id=str(task.id), kinds=["agent_turn"])
    return task, await TaskService(session).claim(task.id, RunLease(run.id, str(task.id), run.attempt_count))


@pytest.mark.asyncio
async def test_revision_and_natural_expiry_stop_next_execution_boundary(controls_db, monkeypatch):
    expiry = datetime.now(UTC) + timedelta(seconds=5)
    async with session_scope() as session:
        saved = await execute(session, MemoryAction(kind="unverified_observation", text="Temporary observation", valid_until=expiry))
        task, claim = await running(session)
        grant = (await check_boundary(session, claim, base()))[0]
        assert grant.memory_revision == 1 and grant.memory_valid_until == expiry
        context = await UserControlService(session).context()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expiry
    monkeypatch.setattr("investment_agent.agent.execution.datetime", Clock)
    monkeypatch.setattr("investment_agent.investor.controls.datetime", Clock)
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="memory_context_expired"):
            await check_boundary(session, claim, base())
        expired = await UserControlService(session).context(private_history_stamp=context.stamp)
        assert not expired.memories and not expired.private_history_allowed
        assert expired.revision == context.revision  # no scheduler/event required
        # A new task gets a fresh context without the expired observation.
        _, fresh = await running(session)
        assert (await check_boundary(session, fresh, base()))[0].memory_valid_until is None
        await execute(session, RevokeAction(memory_ref=ObjectRef(kind="memory", id=saved.id, revision=1), reason="Forget"))
        with pytest.raises(ExecutionDeniedError, match="memory_context_changed"):
            await check_boundary(session, fresh, base())
        assert (await session.get(ExecutionGrantRow, str(task.id))).payload["memory_revision"] == 1


@pytest.mark.asyncio
async def test_task_observations_no_approval_no_preference_escalation_and_expiry(controls_db, monkeypatch):
    candidate, evidence, subject = await fixture()
    expiry = datetime.now(UTC) + timedelta(seconds=5)
    async with session_scope() as session:
        _, claim = await running(session, tools=("save_observation",), scope=(subject,))
        parameters = MemoryAction(kind="unverified_observation", text="A sourced tentative observation", scope_refs=(subject,), source_refs=(evidence,), valid_until=expiry)
        key = uuid4()
        svc = UserControlService(session)
        value = await svc.save_observation(claim, parameters, key, base=base())
        assert value.actor == "agent" and value.origin_ref.kind == "task"
        assert await svc.save_observation(claim, parameters, key, base=base()) == value
        assert (await svc.context(scope_refs=(subject,))).memories == (value,)
        with pytest.raises(InvestorError, match="unverified"):
            await svc.save_observation(claim, parameters.model_copy(update={"kind": "explicit_preference"}), uuid4(), base=base())
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expiry
    monkeypatch.setattr("investment_agent.agent.execution.datetime", Clock)
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="memory_context_expired"):
            await check_boundary(session, claim, base())


def test_api_auth_csrf_exact_wire_and_direct_execution(api_client):
    action = MemoryAction(kind="explicit_preference", text="User explicit preference")
    body = request(action).model_dump(mode="json")
    assert api_client.post("/investor/memories", json=body).status_code == 401
    headers = {"X-API-Key": "secret"}
    result = api_client.post("/investor/memories", json=body, headers=headers)
    assert result.status_code == 201
    assert api_client.post("/investor/memories", json=body, headers=headers).json() == result.json()
    assert api_client.post("/investor/actions", json=body | {"actor": "user"}, headers=headers).status_code == 422
    saved = result.json()
    revoke = request(RevokeAction(memory_ref=ObjectRef(kind="memory", id=UUID(saved["id"]), revision=1), reason="Forget"))
    assert api_client.post(f'/investor/memories/{saved["id"]}/revoke', json=revoke.model_dump(mode="json"), headers=headers).json()["status"] == "revoked"
    assert api_client.get("/investor/memories", headers=headers).json()[0]["status"] == "revoked"
    assert api_client.get("/investor/memories/context", headers=headers).status_code == 404
    assert api_client.get("/investor/memories/checkpoint", headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_expiry_rebuild_excludes_original_and_all_derived_summary(controls_db, monkeypatch):
    expiry = datetime.now(UTC) + timedelta(seconds=5)
    async with session_scope() as session:
        chat, source, _ = await conversation(session)
        svc = UserControlService(session)
        observation = await execute(session, MemoryAction(kind="unverified_observation", text="Temporary user observation", source_refs=(source,), valid_until=expiry))
        old = await svc.context(conversation_id=chat.id)
        assert len(old.messages) == 2
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expiry
    monkeypatch.setattr("investment_agent.investor.controls.datetime", Clock)
    async with session_scope() as session:
        rebuilt = await UserControlService(session).context(conversation_id=chat.id, private_history_stamp=old.stamp)
        assert not rebuilt.messages and not rebuilt.memories and not rebuilt.private_history_allowed
        with pytest.raises(InvestorError, match="Revoked source"):
            await execute(session, MemoryAction(kind="explicit_preference", text="Should not revive", source_refs=(source,)))
        view = await UserControlService(session).get_memories(status="expired")
        assert len(view) == 1 and view[0].id == observation.id and view[0].status == "expired"
        assert not await UserControlService(session).get_memories(status="active")
        assert (await UserControlService(session)._object(ObjectRef(kind="memory", id=observation.id, revision=1), "memory"))["status"] == "active"
        assert observation.valid_until == expiry


@pytest.mark.asyncio
async def test_model_can_prepare_exact_proposal_but_cannot_self_approve(controls_db):
    candidate, _, subject = await fixture()
    action = CaseAction(case_ref=candidate.thesis.case_ref, action="pause", reason="Model suggestion")
    async with session_scope() as session:
        _, claim = await running(session, tools=("prepare_user_operation",), scope=(subject,))
        svc = UserControlService(session)
        proposal = await svc.prepare(PrepareRequest(parameters=action, idempotency_key=uuid4(), expires_at=datetime.now(UTC) + timedelta(hours=1)), claim=claim, base=base())
        assert proposal.actor == "agent" and proposal.source == "trusted_task" and proposal.origin_ref.id == claim.task_id
        with pytest.raises(InvestorError, match="Exact authenticated"):
            await svc.resolve(proposal.id, ResolveRequest(parameters=action, mutation=Mutation(expected_revision=1, idempotency_key=uuid4()), approve=True, reason="actor=user"), claim)
        assert not (await svc.get_case("local_owner", action.case_ref.id)).background_paused
        assert not (await svc._object(ObjectRef(kind="proposal", id=proposal.id, revision=1), "proposal"))["authorization_ref"]


@pytest.mark.asyncio
async def test_user_pause_live_background_only_and_mute_delivery_only(controls_db):
    from investment_agent.investor.common import Quantity
    candidate, _, subject = await fixture()
    enabled = base().model_copy(update={"investor_agent_background_enabled": True})
    async with session_scope() as session:
        svc = UserControlService(session)
        mandate = await svc.get_mandate("local_owner")
        budget = Quantity(value="5", unit="USD")
        authorization = mandate.authorization.model_copy(update={"background_enabled": True, "exploration_scope_refs": (subject,), "per_task_budget": budget, "daily_budget": budget})
        revised = await execute(session, MandateAction(candidate=mandate.model_copy(update={"revision": 2, "authorization": authorization}), expected_revision=1))
        tasks = []
        for mode, source in (("background", "authorized_background"), ("interactive", "authenticated_message")):
            task = await admit(session, base=enabled, input_key=str(uuid4()), binding=ContextBinding(context_refs=(), base_refs=(), as_of=datetime.now(UTC)),
                grant=ExecutionGrant(mode=mode, source=source, prompt="Research", mandate_id=revised.id, scope_refs=(subject,)))
            run = await PipelineRunRepository(session).claim_next(worker_id=str(task.id), kinds=["agent_turn"])
            tasks.append(await TaskService(session).claim(task.id, RunLease(run.id, str(task.id), run.attempt_count)))
        paused = await execute(session, CaseAction(case_ref=candidate.thesis.case_ref, action="pause", reason="Pause background"))
        with pytest.raises(ExecutionDeniedError, match="background_paused"):
            await check_boundary(session, tasks[0], enabled)
        await check_boundary(session, tasks[1], enabled)
        assert await svc.notifications_allowed(paused.id)
        muted = await execute(session, CaseAction(case_ref=ObjectRef(kind="case", id=paused.id, revision=paused.revision), action="mute", reason="Mute notification"))
        assert not await svc.notifications_allowed(paused.id)
        await check_boundary(session, tasks[1], enabled)
        assert muted.background_paused


@pytest.mark.asyncio
async def test_inflight_memory_revocation_settles_model_cost_blocks_tools_and_publish(controls_db):
    import httpx

    from investment_agent.agent.budget import BudgetService
    from investment_agent.agent.tools import ToolRegistration
    from investment_agent.investor.enums import TaskStatus
    from investment_agent.storage.models.agent import AgentModelRun, BudgetEntry
    from tests.test_agent_execution import (
        Value,
        admitted,
        grant,
        http_result,
        no_scope,
        priced,
        run_worker,
    )
    configured = priced()
    async with session_scope() as session:
        saved = await execute(session, MemoryAction(kind="explicit_preference", text="Test preference"))
    task = await admitted(configured, grant(tool_names=("write",)), claim=False)
    started, released = asyncio.Event(), asyncio.Event()
    calls = []
    async def write(session, operation, business):
        calls.append(operation)
        return {"value": business.value}
    async def respond(request):
        started.set()
        await released.wait()
        return http_result(tools=True)
    work = asyncio.create_task(run_worker(configured, tools=(ToolRegistration("write", Value, no_scope, database=write),), transport=httpx.MockTransport(respond)))
    await asyncio.wait_for(started.wait(), 5)
    async with session_scope() as session:
        await execute(session, RevokeAction(memory_ref=ObjectRef(kind="memory", id=saved.id, revision=1), reason="Forget during model call"))
    released.set()
    await work
    async with session_scope() as session:
        final = await TaskService(session).get_task(task.id)
        assert final.status == TaskStatus.FAILED and final.usage.requests == 1 and final.usage.input_tokens == 20
        entry = await session.scalar(select(BudgetEntry))
        assert entry.state == "settled" and entry.cost_usd > 0
        audit = await session.scalar(select(AgentModelRun))
        assert audit.usage == {"input_tokens": 20, "output_tokens": 4}
        assert (await BudgetService(session, configured).usage(task.id)).cost.value > 0
        assert not calls


def test_user_controls_cookie_csrf_and_commit_failure_before_success(api_client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from investment_agent.web.session import SESSION_COOKIE_NAME, create_session_token
    action = MemoryAction(kind="explicit_preference", text="Cookie user preference")
    body = request(action).model_dump(mode="json")
    api_client.cookies.set(SESSION_COOKIE_NAME, create_session_token(api_key="secret", ttl_minutes=15))
    assert api_client.post("/investor/memories", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert api_client.post("/investor/memories", json=body, headers={"Origin": "http://testserver"}).status_code == 201
    before = api_client.get("/investor/memories").json()
    async def fail_commit(session):
        raise RuntimeError("Injected commit failure")
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="Injected commit failure"):
            api_client.post("/investor/memories", json=request(action).model_dump(mode="json"), headers={"Origin": "http://testserver"})
    assert api_client.get("/investor/memories").json() == before


@pytest.mark.asyncio
async def test_list_pagination_scope_status_and_full_trusted_context(controls_db):
    candidate, _, subject = await fixture()
    async with session_scope() as session:
        svc = UserControlService(session)
        values = [await execute(session, MemoryAction(kind="explicit_preference", text=f"Preference {i}", scope_refs=(subject,))) for i in range(101)]
        assert len((await svc.context(scope_refs=(subject,))).memories) == 101
        first = await svc.list_objects("memory", limit=100)
        second = await svc.list_objects("memory", after=UUID(first[-1]["id"]), limit=100)
        assert len(first) == 100 and len(second) == 1
        assert {value["id"] for value in (*first, *second)} == {str(value.id) for value in values}
        assert not await svc.get_memories(scope_refs=(subject.model_copy(update={"id": 999}),))
        await execute(session, RevokeAction(memory_ref=ObjectRef(kind="memory", id=values[-1].id, revision=1), reason="Forget one"))
        revoked = await svc.get_memories(status="revoked", limit=1)
        assert len(revoked) == 1 and revoked[0].id == values[-1].id
        assert len(await svc.get_memories(active_only=True)) == 100
        decision = await execute(session, DecisionAction(case_ref=candidate.thesis.case_ref, decision="defer", user_text="Wait"))
        assert (await svc.list_objects("decision", case_id=candidate.thesis.case_ref.id))[0]["id"] == str(decision.id)
        assert not await svc.list_objects("decision", case_id=uuid4())


def test_proposal_current_default_history_and_read_pagination(api_client):
    headers = {"X-API-Key": "secret"}
    parameters = MemoryAction(kind="explicit_preference", text="A proposal preference")
    prepared = api_client.post("/investor/proposals", headers=headers, json=PrepareRequest(parameters=parameters,
        idempotency_key=uuid4(), expires_at=datetime.now(UTC) + timedelta(hours=1)).model_dump(mode="json"))
    assert prepared.status_code == 201
    identifier = prepared.json()["id"]
    resolve = ResolveRequest(parameters=parameters, mutation=Mutation(expected_revision=1, idempotency_key=uuid4()), approve=True, reason="My instruction")
    assert api_client.post(f"/investor/proposals/{identifier}/resolve", headers=headers, json=resolve.model_dump(mode="json")).json()["status"] == "approved"
    assert api_client.get(f"/investor/proposals/{identifier}", headers=headers).json()["status"] == "approved"
    assert api_client.get(f"/investor/proposals/{identifier}?revision=1", headers=headers).json()["status"] == "pending"
    assert api_client.get("/investor/proposals?status=pending", headers=headers).json() == []
    assert api_client.get("/investor/proposals?status=approved", headers=headers).json()[0]["id"] == identifier
    assert api_client.get(f"/investor/proposals?after={identifier}", headers=headers).json() == []


@pytest.mark.asyncio
async def test_concurrent_proposal_single_consumption_and_revoke_cas(controls_db):
    parameters = MemoryAction(kind="explicit_preference", text="Concurrent approval")
    async with session_scope() as session:
        proposal = await UserControlService(session).prepare(PrepareRequest(parameters=parameters, idempotency_key=uuid4(), expires_at=datetime.now(UTC) + timedelta(hours=1)))
    body = ResolveRequest(parameters=parameters, mutation=Mutation(expected_revision=1, idempotency_key=uuid4()), approve=True, reason="Approve once")
    async def resolve():
        async with session_scope() as session:
            return await UserControlService(session).resolve(proposal.id, body, authenticated_request(parameters))
    a, b = await asyncio.gather(resolve(), resolve())
    assert a == b and a.revision == 2 and a.resolution_reason == "Approve once"
    async with session_scope() as session:
        values = await UserControlService(session).get_memories()
        assert len(values) == 1
    memory = values[0]
    parameters = RevokeAction(memory_ref=ObjectRef(kind="memory", id=memory.id, revision=1), reason="Revoke once")
    first = request(parameters)
    async def revoke(body):
        try:
            async with session_scope() as session:
                return await UserControlService(session).execute(body, authenticated_request(parameters))
        except InvestorError as exc:
            return exc.detail.code
    results = await asyncio.gather(revoke(first), revoke(request(parameters)))
    assert sum(isinstance(value, str) and value == "revision_conflict" for value in results) == 1
    async with session_scope() as session:
        assert (await UserControlService(session).get_memories())[0].revision == 2
        assert await memory_revision(session) == 2

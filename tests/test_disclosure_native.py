"""Actual native FunctionModel + durable external action path, offline source bytes."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select

from investment_agent.agent.disclosure_tools import registrations
from investment_agent.agent.execution import ExecutionDeniedError
from investment_agent.agent.tasks import TaskService
from investment_agent.agent.tools import ExecutionContext, TrustedTools
from investment_agent.evidence.disclosures import DisclosurePermission, FetchRequest
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.disclosure import DisclosureBlob, DisclosureOriginal
from tests.support.disclosure import NOW, FixtureHTTP, discovery, seed
from tests.support.execution import (
    admitted,
    execution_db,
    grant,
    run_worker,
    settings,
)

disclosure_db = execution_db
pytestmark = pytest.mark.usefixtures("disclosure_db")


async def setup(base, *, claim=False, missing_permission=False, **permission_updates):
    async with session_scope() as session:
        listing_id = await seed(session)
    permission = DisclosurePermission(listing_id=listing_id, cutoff=NOW).model_copy(
        update=permission_updates
    )
    auth = grant(
        tool_names=("discover_disclosures", "fetch_disclosure_original"),
        scope_refs=(ObjectRef(kind="listing", id=listing_id, revision=1),),
        disclosure_permissions=() if missing_permission else (permission,),
    )
    task = await admitted(base, auth, claim=claim)
    return listing_id, task


async def test_native_function_model_official_discover_and_fetch():
    base = settings(disclosure_originals_enabled=True)
    listing_id, task = await setup(base)
    http, rounds = FixtureHTTP(), []

    def respond(messages, info):
        rounds.append(messages)
        if len(rounds) == 1:
            assert {t.name for t in info.function_tools} == {
                "discover_disclosures",
                "fetch_disclosure_original",
            }
            part = ToolCallPart(
                "discover_disclosures", {"business": discovery(listing_id).model_dump(mode="json")}
            )
        elif len(rounds) == 2:
            returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][
                -1
            ].content
            assert (
                returned["untrusted_source_data"]
                and returned["items"][0]["verified_correction_refs"] == []
            )
            request = FetchRequest(
                listing_id=listing_id, item_id=returned["items"][0]["id"], as_of=NOW
            )
            part = ToolCallPart(
                "fetch_disclosure_original", {"business": request.model_dump(mode="json")}
            )
        else:
            part = TextPart(
                '{"summary":"Unparsed official original acquired; verification pending."}'
            )
        return ModelResponse([part], usage=RequestUsage(input_tokens=10, output_tokens=5))

    await run_worker(base, FunctionModel(respond), tools=registrations(test_http=http))
    async with session_scope() as session:
        persisted = await TaskService(session).get_task(task.id)
        from investment_agent.storage.models.agent import AgentModelRun

        audits = (await session.scalars(select(AgentModelRun))).all()
        assert persisted.status.value == "partial", (
            persisted.model_dump(mode="json"),
            await TaskService(session).get_checkpoint(task.id),
            [a.error_code for a in audits],
        )
        actions = (
            await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id)))
        ).all()
        assert len(actions) == 2 and all(
            a.effect == "external" and a.state == "committed" for a in actions
        )
        assert await session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 1
        assert await session.scalar(select(func.count()).select_from(DisclosureBlob)) == 1
    assert len(http.calls) == 3


@pytest.mark.parametrize("mode", ["disabled", "missing_exact", "size", "cutoff", "count"])
async def test_native_exact_permission_enforced_before_http(mode):
    base = settings(disclosure_originals_enabled=mode != "disabled")
    listing_id, (task, claim) = await setup(
        base,
        claim=True,
        missing_permission=mode == "missing_exact",
        max_discoveries=0 if mode == "count" else 1,
        max_bytes_per_response=1024 if mode == "size" else 16777216,
    )
    http = FixtureHTTP()
    tools = TrustedTools(
        ExecutionContext(claim, session_scope, base), registrations(test_http=http)
    )
    parameters = discovery(listing_id)
    if mode == "cutoff":
        parameters = parameters.model_copy(
            update={"as_of": NOW.replace(year=2025), "start": NOW.replace(year=2025)}
        )
    with pytest.raises(ExecutionDeniedError):
        await tools.call(tools.registrations["discover_disclosures"], parameters)
    assert not http.calls


async def test_native_external_unknown_recovery_and_replay_do_not_refetch():
    base = settings(disclosure_originals_enabled=True)
    listing_id, (task, claim) = await setup(base, claim=True)
    http = FixtureHTTP()
    context = ExecutionContext(claim, session_scope, base)
    specs = registrations(test_http=http)
    tools = TrustedTools(context, specs)
    spec = tools.registrations["discover_disclosures"]
    original_send = spec.external.send

    async def lost_response(operation, parameters):
        await original_send(operation, parameters)
        raise RuntimeError("fixture response lost after durable original acquisition receipt")

    spec.external.send = lost_response
    with pytest.raises(RuntimeError):
        await tools.call(spec, discovery(listing_id))
    async with session_scope() as session:
        actions = (
            await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id)))
        ).all()
        assert actions[0].state == "uncertain"
    resumed = TrustedTools(context, specs)
    await resumed.recover()
    recovered = await resumed.call(
        resumed.registrations["discover_disclosures"], discovery(listing_id)
    )
    assert recovered["status"] == "ok" and len(http.calls) == 2
    with pytest.raises(ExecutionDeniedError, match="count_exhausted"):
        await resumed.call(
            resumed.registrations["discover_disclosures"], discovery(listing_id, limit=1)
        )
    assert len(http.calls) == 2


async def test_independent_sessions_concurrent_duplicate_original_and_restart():
    import asyncio

    from investment_agent.evidence.disclosures import DisclosureService

    async with session_scope() as session:
        listing_id = await seed(session)
        http = FixtureHTTP()
        result = await DisclosureService.for_tests(session, http).execute(
            uuid4(), discovery(listing_id)
        )
    request = FetchRequest(listing_id=listing_id, item_id=result["items"][0]["id"], as_of=NOW)

    async def acquire():
        async with session_scope() as session:
            return await DisclosureService.for_tests(session, http).execute(uuid4(), request)

    first, second = await asyncio.gather(acquire(), acquire())
    assert first["original"]["id"] == second["original"]["id"]
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 1
        original, body = await DisclosureService(session).read(
            UUID(first["original"]["id"]), datetime.now(UTC)
        )
        assert body == http.body and original["content_hash"] == first["original"]["content_hash"]


@pytest.mark.parametrize("revocation", ["cancel", "lease", "disable"])
async def test_stale_network_send_rolls_back_all_original_material(revocation):
    import asyncio

    from sqlalchemy import update

    from investment_agent.agent.tasks import TaskBoundaryError
    from investment_agent.storage.models.analysis import PipelineRun
    from investment_agent.storage.models.disclosure import DisclosureReceipt
    from investment_agent.storage.models.evidence import SavedEvidence

    base = settings(disclosure_originals_enabled=True)
    listing_id, (task, claim) = await setup(base, claim=True)
    from investment_agent.settings.loader import ensure_imported

    async with session_scope() as session:
        await ensure_imported(session, settings=base)
    http = FixtureHTTP()
    tools = TrustedTools(
        ExecutionContext(claim, session_scope, base), registrations(test_http=http)
    )
    discovered = await tools.call(
        tools.registrations["discover_disclosures"], discovery(listing_id)
    )
    entered, released = asyncio.Event(), asyncio.Event()
    original_request = http.request

    async def slow_request(url, purpose, **kwargs):
        if purpose == "original":
            entered.set()
            await released.wait()
        return await original_request(url, purpose, **kwargs)

    http.request = slow_request
    request = FetchRequest(listing_id=listing_id, item_id=discovered["items"][0]["id"], as_of=NOW)
    running = asyncio.create_task(
        tools.call(tools.registrations["fetch_disclosure_original"], request)
    )
    await asyncio.wait_for(entered.wait(), 1)
    if revocation == "disable":
        from investment_agent.settings.store import SettingsStore

        async with session_scope() as session:
            await SettingsStore(session).apply(
                {"disclosure_originals_enabled": False},
                actor="user",
                note="fixture disable during source read",
            )
    else:
        # This independent writer must remain responsive while HTTP is paused.
        async with session_scope() as session:
            if revocation == "cancel":
                await TaskService(session).cancel(task.id)
            else:
                await session.execute(
                    update(PipelineRun)
                    .where(PipelineRun.id == claim.lease.run_id)
                    .values(attempt_count=claim.lease.attempt + 1, worker_id="replacement")
                )
    released.set()
    with pytest.raises((TaskBoundaryError, ExecutionDeniedError, RuntimeError)):
        await running
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 0
        assert await session.scalar(select(func.count()).select_from(DisclosureBlob)) == 0
        assert await session.scalar(select(func.count()).select_from(SavedEvidence)) == 0
        actions = (
            await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task.id)))
        ).all()
        fetch_action = next(
            a for a in actions if a.parameters["tool"] == "fetch_disclosure_original"
        )
        assert await session.get(DisclosureReceipt, fetch_action.operation_id) is None
    external = tools.registrations["fetch_disclosure_original"].external
    outcome, result = await external.reconcile(UUID(fetch_action.operation_id), request)
    assert outcome == "not_applied" and result is None


async def test_concurrent_same_operation_changed_remote_bytes_replays_one_result():
    import asyncio

    from investment_agent.evidence.disclosure_http import SourceResponse
    from investment_agent.evidence.disclosures import DisclosureService

    async with session_scope() as session:
        listing_id = await seed(session)
        http = FixtureHTTP()
        discovered = await DisclosureService.for_tests(session, http).execute(
            uuid4(), discovery(listing_id)
        )
    request = FetchRequest(listing_id=listing_id, item_id=discovered["items"][0]["id"], as_of=NOW)
    original_request, rounds = http.request, []

    async def changed_source(url, purpose, **kwargs):
        response = await original_request(url, purpose, **kwargs)
        rounds.append(1)
        return SourceResponse(
            response.status,
            response.headers,
            b"%PDF-1.7\n" + str(len(rounds)).encode() + b"\n%%EOF",
            response.url,
        )

    http.request = changed_source
    operation = uuid4()

    async def acquire():
        async with session_scope() as session:
            return await DisclosureService.for_tests(session, http).execute(operation, request)

    first, second = await asyncio.gather(acquire(), acquire())
    assert first == second
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 1
        assert await session.scalar(select(func.count()).select_from(DisclosureBlob)) == 1

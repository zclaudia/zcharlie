"""Short final fencing and atomic immutable parser receipts in file SQLite."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from investment_agent.agent.document_tools import registrations
from investment_agent.agent.execution import ExecutionDeniedError
from investment_agent.agent.tasks import TaskBoundaryError, TaskService
from investment_agent.agent.tools import ExecutionContext, TrustedTools
from investment_agent.evidence.disclosures import DisclosurePermission
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.evidence import SavedEvidence
from tests.support.execution import admitted, grant, settings
from tests.support.execution import execution_db as execution_db
from tests.support.originals import html, original, snapshot

pytestmark = pytest.mark.usefixtures("execution_db")


async def setup(**overrides):
    base = settings(disclosure_originals_enabled=True)
    async with session_scope() as session:
        payload, _, request = await original(
            session, body=html().replace(b"</body>", b"<p>" + b"x" * 1500 + b"</p></body>")
        )
        await snapshot(session, request.listing_id)
    permission = DisclosurePermission(
        listing_id=request.listing_id, cutoff=datetime.now(UTC), max_parses=1
    ).model_copy(update=overrides)
    auth = grant(
        tool_names=("parse_disclosure_original", "read_disclosure_original"),
        scope_refs=(ObjectRef(kind="listing", id=request.listing_id, revision=1),),
        disclosure_permissions=(permission,),
    )
    task, claim = await admitted(base, auth)
    request = request.model_copy(update={"as_of": permission.cutoff})
    context = ExecutionContext(claim, session_scope, base)
    return base, task, context, request


async def test_exact_native_parser_receipt_replay_and_later_readback_count():
    _, task, context, request = await setup()
    tools = TrustedTools(context, registrations())
    spec = tools.registrations["parse_disclosure_original"]
    first = await tools.call(spec, request)
    assert first["facts"][0]["verification"] == "verified"
    assert await tools.call(spec, request) == first
    with pytest.raises(ExecutionDeniedError, match="count"):
        await tools.call(spec, request.model_copy(update={"offset": 1}))
    with pytest.raises(ExecutionDeniedError, match="count"):
        await tools.call(tools.registrations["read_disclosure_original"], request)
    async with session_scope() as session:
        action = await session.scalar(select(ToolAction).where(ToolAction.task_id == str(task.id)))
        assert (
            action.state == "committed"
            and len((await TaskService(session).get_task(task.id)).artifact_refs) == 1
        )
        assert await session.scalar(select(func.count()).select_from(SavedEvidence)) == 3
        assert (await spec.external.reconcile(UUID(action.operation_id), request))[0] == "committed"


@pytest.mark.parametrize("mode", ["cutoff", "count", "size"])
async def test_native_parser_exact_cutoff_count_size_permission(mode):
    overrides = (
        {"max_parses": 0}
        if mode == "count"
        else {"max_bytes_per_response": 1024}
        if mode == "size"
        else {}
    )
    _, _, context, request = await setup(**overrides)
    if mode == "cutoff":
        request = request.model_copy(update={"as_of": request.as_of - timedelta(seconds=1)})
    tools = TrustedTools(context, registrations())
    with pytest.raises((ExecutionDeniedError, RuntimeError)):
        await tools.call(tools.registrations["parse_disclosure_original"], request)


@pytest.mark.parametrize("revocation", ["cancel", "disable"])
async def test_actual_parse_releases_writer_and_post_parse_rechecks_atomically(
    monkeypatch, revocation
):
    import investment_agent.agent.document_tools as module
    from investment_agent.settings.loader import ensure_imported
    from investment_agent.settings.store import SettingsStore

    base, task, context, request = await setup()
    async with session_scope() as session:
        await ensure_imported(session, settings=base)
    started, released = asyncio.Event(), asyncio.Event()
    real = module.parse_staged

    async def paused(body, mime, **window):
        # Invoke the production subprocess, not a fake text result.
        parsed = await real(body, mime, **window)
        started.set()
        await released.wait()
        return parsed

    monkeypatch.setattr(module, "parse_staged", paused)
    tools = TrustedTools(context, registrations())
    running = asyncio.create_task(
        tools.call(tools.registrations["parse_disclosure_original"], request)
    )
    await asyncio.wait_for(started.wait(), 3)
    async with session_scope() as session:
        if revocation == "cancel":
            await TaskService(session).cancel(task.id)
        else:
            await SettingsStore(session).apply(
                {"disclosure_originals_enabled": False},
                actor="user",
                note="fixture parser revocation",
            )
    released.set()
    with pytest.raises((TaskBoundaryError, ExecutionDeniedError, RuntimeError)):
        await running
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(SavedEvidence)) == 1
        action = await session.scalar(select(ToolAction).where(ToolAction.task_id == str(task.id)))
        assert action.result is None

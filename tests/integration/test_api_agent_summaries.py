"""AIB1 conversation summaries: activity ordering, attention, cursor and rename."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from investment_agent.agent.contracts import WaitRequirement
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.investor.enums import StopReason
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask, ConversationRow
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease

KEY = {"X-API-Key": "secret"}


def create(client, *, title=None, context_refs=()):
    response = client.post("/agent/conversations", headers=KEY,
        json={"title": title, "context_refs": [ref.model_dump(mode="json") for ref in context_refs]})
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, conversation_id, *, text="Question", context_refs=()):
    body = {"client_message_id": str(uuid4()), "text": text,
        "context_refs": [ref.model_dump(mode="json") for ref in context_refs]}
    url = f"/agent/conversations/{conversation_id}/messages"
    response = client.post(url, headers=KEY, json=body)
    assert response.status_code == 202, response.text
    return response.json()


async def seed_listings():
    from investment_agent.investor.common import ObjectRef
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="公司甲")
        session.add(company)
        await session.flush()
        good = Listing(company_id=company.id, market_code="SSE", ticker="600001", is_primary=True)
        odd = Listing(company_id=company.id, market_code="SSE", ticker="ABC", is_primary=True)
        session.add_all([good, odd])
        await session.flush()
        return (ObjectRef(kind="listing", id=good.id, revision=1),
            ObjectRef(kind="listing", id=odd.id, revision=1))


async def claim_task(task_id):
    async with session_scope() as session:
        row = await session.get(AgentTask, str(task_id))
        run = await PipelineRunRepository(session).claim_next(worker_id="fixture", run_id=row.run_id)
        return await TaskService(session).claim(UUID(task_id), RunLease(run.id, "fixture", run.attempt_count))


async def answer(execution, text="已核实的回答"):
    from investment_agent.agent.conversations import ConversationService
    async with session_scope() as session:
        await ConversationService(session, Settings()).save_assistant(execution, text=text)
        await TaskService(session).checkpoint(execution, checkpoint={}, terminal="succeeded",
            stop_reason=StopReason.ANSWERED)


def summaries(client, **params):
    response = client.get("/agent/conversation-summaries", headers=KEY, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_activity_ordering_attention_and_identity(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    good, odd = await seed_listings()
    first = create(api_client, title="公司甲")
    receipt = submit(api_client, first["id"], context_refs=(good,))
    execution = await claim_task(receipt["task"]["id"])
    await answer(execution)
    second = create(api_client)
    submit(api_client, second["id"], context_refs=(good,))
    page = summaries(api_client)
    assert [item["id"] for item in page["items"]] == [second["id"], first["id"]]
    assert page["next_cursor"] is None
    active, answered = page["items"]
    assert active["title"] is None and active["title_revision"] == 1
    assert active["message_count"] == 1 and active["attention"] == "running"
    assert active["latest_task"]["status"] == "queued" and active["latest_task"]["cancel_requested"] is False
    assert active["subjects"] == [{"ref": good.model_dump(mode="json"), "status": "available",
        "name": "公司甲", "ticker": "600001.SH"}]
    assert answered["message_count"] == 2 and answered["attention"] == "none"
    assert answered["latest_task"]["status"] == "succeeded"
    assert answered["latest_task"]["stop_reason"] == "answered"
    assert answered["created_at"] <= answered["last_activity_at"]

    # A running cancellation keeps the summary active until the terminal state.
    running = await claim_task(active["latest_task"]["id"])
    cancelled = api_client.post(f"/agent/tasks/{active['latest_task']['id']}/cancel", headers=KEY,
        json={"idempotency_key": str(uuid4())}).json()
    assert cancelled["status"] == "running" and cancelled["cancel_requested"] is True
    page = summaries(api_client)
    assert [item["id"] for item in page["items"]] == [second["id"], first["id"]]
    assert page["items"][0]["attention"] == "running"
    assert page["items"][0]["latest_task"]["cancel_requested"] is True
    async with session_scope() as session:
        await TaskService(session).ack_cancel(running)
    page = summaries(api_client)
    assert page["items"][0]["attention"] == "none"
    assert page["items"][0]["latest_task"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_attention_filter_priority_and_latest_task(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    good, _ = await seed_listings()

    async def wait_user(task_id):
        execution = await claim_task(task_id)
        async with session_scope() as session:
            await TaskService(session).checkpoint(execution, checkpoint={},
                waiting=WaitRequirement(required_input="需要补充投资期限",
                    dependency_ids=(uuid4(),), deadline=datetime.now(UTC) + timedelta(minutes=5)),
                wait_status="waiting_user")

    mixed = create(api_client)
    running_receipt = submit(api_client, mixed["id"])
    await claim_task(running_receipt["task"]["id"])
    submit(api_client, mixed["id"], text="第二个任务")
    needs = create(api_client)
    waiting_receipt = submit(api_client, needs["id"])
    await wait_user(waiting_receipt["task"]["id"])
    # Latest terminal task must not mask the older still-running one.
    masked = create(api_client)
    old_running = submit(api_client, masked["id"])
    await claim_task(old_running["task"]["id"])
    newer = submit(api_client, masked["id"], text="新任务")
    api_client.post(f"/agent/tasks/{newer['task']['id']}/cancel", headers=KEY,
        json={"idempotency_key": str(uuid4())})
    idle = create(api_client)

    grouped = summaries(api_client, limit=100)
    by_id = {item["id"]: item for item in grouped["items"]}
    assert by_id[mixed["id"]]["attention"] == "running"
    assert by_id[needs["id"]]["attention"] == "needs_you"
    assert by_id[masked["id"]]["attention"] == "running"
    assert by_id[masked["id"]]["latest_task"]["status"] == "cancelled"
    assert by_id[idle["id"]]["attention"] == "none" and by_id[idle["id"]]["latest_task"] is None
    assert by_id[idle["id"]]["message_count"] == 0

    assert [item["id"] for item in summaries(api_client, attention="needs_you")["items"]] == [needs["id"]]
    running_items = summaries(api_client, attention="running")["items"]
    assert {item["id"] for item in running_items} == {mixed["id"], masked["id"]}
    assert {item["id"] for item in summaries(api_client, attention="none")["items"]} == {idle["id"]}
    # Filtering happens before pagination: one needs_you page hides running ones.
    assert [item["id"] for item in summaries(api_client, limit=1, attention="needs_you")["items"]] == [needs["id"]]


@pytest.mark.asyncio
async def test_equal_time_pagination_and_cursor_validation(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    created = [create(api_client)["id"] for _ in range(4)]
    for conversation_id in created:
        submit(api_client, conversation_id)
    async with session_scope() as session:
        await session.execute(update(ConversationRow).values(last_activity_at=datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)))
    page = summaries(api_client, limit=2)
    assert [item["last_activity_at"] for item in page["items"]] == ["2026-03-01T08:00:00Z"] * 2
    assert page["next_cursor"] is not None
    middle = summaries(api_client, limit=1, before=page["next_cursor"])
    assert len(middle["items"]) == 1 and middle["next_cursor"] is not None
    last = summaries(api_client, limit=2, before=middle["next_cursor"])
    assert len(last["items"]) == 1 and last["next_cursor"] is None
    # Every conversation holds a queued task, so the none-group page is empty.
    empty = summaries(api_client, limit=2, attention="none")
    assert empty["items"] == [] and empty["next_cursor"] is None
    seen = [item["id"] for item in (*page["items"], *middle["items"], *last["items"])]
    assert sorted(seen) == sorted(created) and len(set(seen)) == 4

    assert api_client.get("/agent/conversation-summaries", headers=KEY,
        params={"before": "not-a-cursor"}).status_code == 422
    mismatch = summaries(api_client, limit=1, attention="running")["next_cursor"]
    assert api_client.get("/agent/conversation-summaries", headers=KEY,
        params={"before": mismatch}).status_code == 422
    assert api_client.get("/agent/conversation-summaries", headers=KEY,
        params={"before": mismatch, "attention": "running"}).status_code == 200


@pytest.mark.asyncio
async def test_rename_revision_conflict_and_history(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    good, _ = await seed_listings()
    conversation = create(api_client)
    submit(api_client, conversation["id"], context_refs=(good,))
    url = f"/agent/conversations/{conversation['id']}"

    def patch(title, revision):
        return api_client.patch(url, headers=KEY, json={"title": title, "expected_title_revision": revision})

    renamed = patch("  新标题 ", 1)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {"id": conversation["id"], "title": "新标题", "title_revision": 2}
    unchanged = patch("新标题", 2)
    assert unchanged.json()["title_revision"] == 2
    assert patch("其他标题", 1).status_code == 409
    assert api_client.patch(url, headers=KEY,
        json={"title": "其他标题", "expected_title_revision": 1}).json()["detail"]["code"] == "conversation_title_conflict"
    assert patch("  ", 2).status_code == 422
    assert patch("字" * 257, 2).status_code == 422
    combined = patch("e\u0301x", 2)
    assert combined.json()["title"] == "éx" and combined.json()["title_revision"] == 3
    cleared = patch(None, 3)
    assert cleared.json() == {"id": conversation["id"], "title": None, "title_revision": 4}
    assert api_client.patch(f"/agent/conversations/{uuid4()}", headers=KEY,
        json={"title": "任意", "expected_title_revision": 1}).status_code == 404

    # Renaming never moves the conversation revision inside saved messages.
    messages = api_client.get(f"{url}/messages", headers=KEY).json()
    assert messages[0]["conversation_ref"]["revision"] == 1
    listed = api_client.get("/agent/conversations", headers=KEY).json()
    assert listed[0]["revision"] == 1 and listed[0]["title"] is None
    before = summaries(api_client)["items"]
    assert patch("排序不变", 4).status_code == 200
    after = summaries(api_client)["items"]
    assert [item["id"] for item in after] == [item["id"] for item in before]
    assert after[0]["title"] == "排序不变" and after[0]["title_revision"] == 5


@pytest.mark.asyncio
async def test_subjects_follow_latest_user_message(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    good, odd = await seed_listings()
    conversation = create(api_client, context_refs=(good,))
    submit(api_client, conversation["id"], context_refs=(odd,))
    page = summaries(api_client)
    assert page["items"][0]["subjects"] == [{"ref": odd.model_dump(mode="json"), "status": "unavailable",
        "name": None, "ticker": None}]
    fresh = create(api_client, context_refs=(good,))
    assert summaries(api_client)["items"][0]["id"] == fresh["id"]
    assert summaries(api_client)["items"][0]["subjects"][0]["status"] == "available"


@pytest.mark.asyncio
async def test_archive_restore_and_409_contracts(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    conversation = create(api_client, title="归档")
    url = f"/agent/conversations/{conversation['id']}"
    receipt = submit(api_client, conversation["id"])
    busy = api_client.post(f"{url}/archive", headers=KEY)
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "conversation_active"
    execution = await claim_task(receipt["task"]["id"])
    await answer(execution)

    archived = api_client.post(f"{url}/archive", headers=KEY)
    assert archived.status_code == 200 and archived.json()["archived_at"] is not None
    assert summaries(api_client)["items"] == []
    assert [item["id"] for item in summaries(api_client, archived="true")["items"]] == [conversation["id"]]
    assert api_client.get("/agent/conversations", headers=KEY).json() == []
    blocked = api_client.post(f"{url}/messages", headers=KEY,
        json={"client_message_id": str(uuid4()), "text": "再问一个", "context_refs": []})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "conversation_archived"
    assert api_client.get(f"{url}/messages", headers=KEY).status_code == 200  # still readable

    restored = api_client.post(f"{url}/unarchive", headers=KEY)
    assert restored.status_code == 200 and restored.json()["archived_at"] is None
    assert [item["id"] for item in summaries(api_client)["items"]] == [conversation["id"]]
    assert api_client.post(f"/agent/conversations/{uuid4()}/archive", headers=KEY).status_code == 404

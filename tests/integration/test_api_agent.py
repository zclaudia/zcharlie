"""Authenticated AG09 HTTP acceptance and actual ASGI stream disconnect."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from investment_agent.agent.tasks import TaskService
from investment_agent.storage.database import get_engine, session_scope
from investment_agent.storage.models.agent import AgentTask, PublicTaskEvent

KEY = {"X-API-Key": "secret"}


def accepted(client):
    conversation = client.post("/agent/conversations", headers=KEY, json={"title": "Fixture", "context_refs": []})
    assert conversation.status_code == 201
    body = {"client_message_id": str(uuid4()), "text": "Question", "context_refs": []}
    url = f"/agent/conversations/{conversation.json()['id']}/messages"
    response = client.post(url, headers=KEY, json=body)
    assert response.status_code == 202, response.text
    return conversation.json(), response.json(), body, url


@pytest.mark.asyncio
async def test_api_auth_csrf_receipts_queries_and_extra_permissions(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    assert api_client.post("/agent/conversations", json={}).status_code == 401
    conversation, receipt, body, url = accepted(api_client)
    assert api_client.post(url, headers=KEY, json=body).json()["task"]["id"] == receipt["task"]["id"]
    assert api_client.post(url, headers=KEY, json={**body, "text": "Different"}).status_code == 409
    for extra in ({"actor": "user"}, {"grant": {}}, {"tool_names": ["request_data_refresh"]}, {"as_of": "2020-01-01"}):
        assert api_client.post(url, headers=KEY, json={**body, **extra}).status_code == 422
    assert api_client.get("/agent/conversations", headers=KEY).json()[0]["id"] == conversation["id"]
    assert api_client.get(url, headers=KEY).json()[0]["id"] == receipt["message"]["id"]
    task_url = f"/agent/tasks/{receipt['task']['id']}"
    assert api_client.get(task_url).status_code == 401
    assert api_client.get(task_url, headers=KEY).json()["message_ref"]["id"] == receipt["message"]["id"]
    assert api_client.get(task_url + "/snapshot", headers=KEY).json()["messages"][0]["text"] == "Question"
    assert api_client.post(task_url + "/cancel", headers=KEY, json={"idempotency_key": str(uuid4())}).json()["status"] == "cancelled"
    assert api_client.post("/app/login", data={"api_key": "secret", "next": "/app"}).status_code == 200
    assert api_client.post("/agent/conversations", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert api_client.post("/agent/conversations", json={}).status_code == 403
    assert api_client.post("/agent/conversations", json={}, headers={"X-Requested-With": "fetch"}).status_code == 201
    assert api_client.get(task_url).status_code == 200


@pytest.mark.asyncio
async def test_sse_reconnect_header_priority_poll_fallback_and_retention(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    _, receipt, _, _ = accepted(api_client)
    task_id = UUID(receipt["task"]["id"])
    async with session_scope() as session:
        svc = TaskService(session)
        first = (await svc.get_events(task_id))[0].event_id
        await svc.append_event(task_id, None, "progress", {"summary": "Only public progress"})
        await svc.cancel(task_id)
    url = receipt["events_url"]
    polling = api_client.get(url, headers={**KEY, "Last-Event-ID": str(first)}, params={"transport": "poll"})
    assert polling.status_code == 200
    ids = [event["event_id"] for event in polling.json()["events"]]
    assert len(ids) == 2 and ids == sorted(ids) and ids[0] > first
    stream = api_client.get(url, headers={**KEY, "Last-Event-ID": str(first)}, params={"after_event_id": 0})
    assert stream.status_code == 200 and stream.headers["content-type"].startswith("text/event-stream")
    assert f"id: {first}\n" not in stream.text
    assert all(f"id: {event_id}\n" in stream.text for event_id in ids)
    assert "Only public progress" in stream.text
    for hidden in ("fixture-model-key", "private_history", "reasoning", "prompt", "raw_provider"):
        assert hidden not in stream.text
    assert api_client.get(url, headers={**KEY, "Last-Event-ID": "bad"}).status_code == 422
    async with session_scope() as session:
        await session.execute(update(PublicTaskEvent).values(occurred_at=datetime.now(UTC)-timedelta(days=31)))
        assert await TaskService(session).retain_events() == 3
    for transport in ("sse", "poll"):
        expired = api_client.get(url, headers={**KEY, "Last-Event-ID": str(ids[-1])}, params={"transport": transport})
        assert expired.status_code == 410
        detail = expired.json()["detail"]
        assert detail["code"] == "cursor_expired" and detail["reload_snapshot"] is True
        snapshot = api_client.get(detail["snapshot_url"], headers=KEY).json()
        assert snapshot["task"]["status"] == "cancelled" and snapshot["event_cursor"] == 0
    assert api_client.get(url, headers=KEY, params={"transport": "poll"}).json()["events"] == []


@pytest.mark.asyncio
async def test_actual_asgi_disconnect_releases_sessions_without_cancelling(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    _, receipt, _, _ = accepted(api_client)
    disconnected = asyncio.Event()
    frames = []
    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}
    async def send(frame):
        frames.append(frame)
        if frame["type"] == "http.response.body" and b"id:" in frame.get("body", b""):
            # This is a real response yield; no read connection is held.
            assert get_engine().sync_engine.pool.checkedout() == 0
            disconnected.set()
    url = receipt["events_url"]
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1", "method": "GET", "scheme": "http", "path": url,
        "raw_path": url.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"x-api-key", b"secret"), (b"host", b"testserver")],
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345)}
    await asyncio.wait_for(api_client.app(scope, receive, send), timeout=5)
    assert any(frame["type"] == "http.response.start" and frame["status"] == 200 for frame in frames)
    async with session_scope() as session:
        row = await session.scalar(select(AgentTask).where(AgentTask.id == receipt["task"]["id"]))
        assert row.status == "queued" and row.payload["cancel_requested"] is False
    from investment_agent.api.routes.agent import _active_streams
    assert not _active_streams


@pytest.mark.asyncio
async def test_out_of_range_and_future_cursors_fail_safely(api_client, monkeypatch):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    _, receipt, _, _ = accepted(api_client)
    for cursor in ("9999999999999999999", "9223372036854775808", "100000", "-1", ""):
        response = api_client.get(receipt["events_url"], headers={**KEY, "Last-Event-ID": cursor}, params={"transport": "poll"})
        assert response.status_code == 422
    assert api_client.get(receipt["events_url"], headers=KEY, params={"after_event_id": 2**63}).status_code == 422


@pytest.mark.asyncio
async def test_sse_limit_has_poll_fallback_and_no_unstarted_reservation_leak(api_client, monkeypatch):
    from investment_agent.api.routes.agent import MAX_STREAMS, _active_streams
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    _, receipt, _, _ = accepted(api_client)
    tokens = {uuid4() for _ in range(MAX_STREAMS)}
    _active_streams.update(tokens)
    try:
        assert api_client.get(receipt["events_url"], headers=KEY).status_code == 429
        assert api_client.get(receipt["events_url"], headers=KEY, params={"transport": "poll"}).status_code == 200
    finally:
        _active_streams.difference_update(tokens)
    async def disconnected():
        return {"type": "http.disconnect"}
    async def send(frame):
        await asyncio.sleep(0.01)  # header send can be interrupted before iteration
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1", "method": "GET", "scheme": "http", "path": receipt["events_url"],
        "raw_path": receipt["events_url"].encode(), "query_string": b"", "root_path": "",
        "headers": [(b"x-api-key", b"secret"), (b"host", b"testserver")],
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345)}
    await asyncio.wait_for(api_client.app(scope, disconnected, send), timeout=5)
    assert not _active_streams


@pytest.mark.asyncio
async def test_admission_commit_failure_cannot_send_accepted_response(api_client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from investment_agent.storage.models.agent import MessageRow
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    conversation = api_client.post("/agent/conversations", headers=KEY, json={}).json()
    original = AsyncSession.commit
    async def fail_commit(self):
        # Failure only on the real message acceptance transaction.
        if await self.scalar(select(MessageRow.id).limit(1)) is not None:
            raise RuntimeError("fixture commit failure")
        await original(self)
    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    sent = []
    async def receive():
        return {"type": "http.request", "body": ('{"client_message_id":"'+str(uuid4())+'","text":"Question","context_refs":[]}').encode(), "more_body": False}
    async def send(frame):
        sent.append(frame)
    url = f"/agent/conversations/{conversation['id']}/messages"
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1", "method": "POST", "scheme": "http", "path": url,
        "raw_path": url.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"x-api-key", b"secret"), (b"host", b"testserver"), (b"content-type", b"application/json")],
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345)}
    with pytest.raises(RuntimeError, match="fixture commit failure"):
        await api_client.app(scope, receive, send)
    starts = [frame for frame in sent if frame["type"] == "http.response.start"]
    assert starts and starts[0]["status"] == 500
    assert not any(frame["status"] == 202 for frame in starts)
    monkeypatch.setattr(AsyncSession, "commit", original)
    async with session_scope() as session:
        assert await session.scalar(select(MessageRow.id)) is None
        assert await session.scalar(select(AgentTask.id)) is None

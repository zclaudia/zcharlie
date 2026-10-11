"""AG25 authenticated API and safe original readback, isolated fixture bytes."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from investment_agent.evidence.disclosure_http import OfficialHTTPS
from investment_agent.storage.database import session_scope
from tests.test_disclosure_originals import NOW, FixtureHTTP, discovery, seed

KEY = {"X-API-Key": "secret"}


@pytest.mark.asyncio
async def test_api_auth_default_off_unsupported_readback_and_html_sandbox(api_client, monkeypatch):
    url = "/investor/disclosures"
    params = {"listing_id": 1, "as_of": NOW.isoformat()}
    assert api_client.get(url, params=params).status_code == 401
    assert api_client.get(url, headers=KEY, params=params).status_code == 200
    assert (
        api_client.post(
            url + "/discover",
            headers=KEY,
            json={"request": discovery().model_dump(mode="json"), "idempotency_key": str(uuid4())},
        ).status_code
        == 409
    )
    monkeypatch.setenv("DISCLOSURE_ORIGINALS_ENABLED", "true")
    http = FixtureHTTP()
    http.items[0]["adjunctUrl"] = "finalpage/2026-10-09/12345.html"
    http.body, http.mime = b'<!doctype html><script>fetch("/admin")</script>', "text/html"

    async def fixture_request(self, *args, **kwargs):
        return await http.request(*args, **kwargs)

    monkeypatch.setattr(OfficialHTTPS, "request", fixture_request)
    async with session_scope() as session:
        listing_id = await seed(session)
    body = {
        "request": discovery(listing_id).model_dump(mode="json"),
        "idempotency_key": str(uuid4()),
    }
    assert (
        api_client.post(
            url + "/discover", headers=KEY, json={**body, "url": "https://private.invalid"}
        ).status_code
        == 422
    )
    unsupported = {**body, "request": {**body["request"], "source": "arbitrary"}}
    assert api_client.post(url + "/discover", headers=KEY, json=unsupported).status_code == 422
    result = api_client.post(url + "/discover", headers=KEY, json=body)
    assert result.status_code == 200 and result.json()["status"] == "ok", result.text
    calls = len(http.calls)
    assert api_client.post(url + "/discover", headers=KEY, json=body).json() == result.json()
    assert len(http.calls) == calls
    fetched = api_client.post(
        url + "/fetch",
        headers=KEY,
        json={
            "request": {
                "listing_id": listing_id,
                "item_id": result.json()["items"][0]["id"],
                "as_of": NOW.isoformat(),
            },
            "idempotency_key": str(uuid4()),
        },
    )
    assert fetched.status_code == 200 and fetched.json()["status"] == "ok", fetched.text
    original = fetched.json()["original"]
    target = url + "/originals/" + original["id"] + "/download"
    assert (
        api_client.get(target, params={"as_of": datetime.now(UTC).isoformat()}).status_code == 401
    )
    assert api_client.get(target, headers=KEY, params={"as_of": NOW.isoformat()}).status_code == 404
    download = api_client.get(target, headers=KEY, params={"as_of": datetime.now(UTC).isoformat()})
    assert download.content == http.body
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"
    historic = api_client.get(
        target, headers=KEY, params={"as_of": NOW.isoformat(), "historical_reconstruction": True}
    )
    assert (
        historic.content == http.body and historic.headers["x-historical-reconstruction"] == "true"
    )
    listing = api_client.get(
        url, headers=KEY, params={"listing_id": listing_id, "as_of": datetime.now(UTC).isoformat()}
    )
    assert len(listing.json()["items"]) == 1 and len(listing.json()["originals"]) == 1

    monkeypatch.setenv("DISCLOSURE_ORIGINALS_ENABLED", "false")
    calls = len(http.calls)
    assert (
        api_client.get(target, headers=KEY, params={"as_of": datetime.now(UTC).isoformat()}).content
        == http.body
    )
    assert api_client.get(
        url, headers=KEY, params={"listing_id": listing_id, "as_of": datetime.now(UTC).isoformat()}
    ).json()["originals"]
    assert api_client.post(url + "/discover", headers=KEY, json=body).json() == result.json()
    assert (
        api_client.post(
            url + "/discover", headers=KEY, json={**body, "idempotency_key": str(uuid4())}
        ).status_code
        == 409
    )
    assert (
        api_client.post(
            url + "/discover",
            headers=KEY,
            json={**body, "request": {**body["request"], "limit": 1}},
        ).status_code
        == 409
    )
    assert len(http.calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", [True, False])
async def test_authenticated_message_opt_in_queue_to_native_framework(
    api_client, monkeypatch, historical
):
    import json
    from uuid import UUID

    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage
    from sqlalchemy import select

    from investment_agent.agent.disclosure_tools import registrations as source_registrations
    from investment_agent.agent.executor import AgentExecutor
    from investment_agent.agent.job import AgentJobHandler
    from investment_agent.agent.research import (
        ResearchOutput,
        ResearchPolicy,
        ResearchProgress,
        registrations,
    )
    from investment_agent.agent.tasks import TaskService
    from investment_agent.evidence.disclosures import FetchRequest
    from investment_agent.investor.common import ObjectRef
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.storage.models.agent import ExecutionGrantRow, ToolAction
    from tests.test_agent_execution import settings

    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    monkeypatch.setenv("DISCLOSURE_ORIGINALS_ENABLED", "true")
    monkeypatch.setenv("AGENT_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("AGENT_API_KEY", "fixture-secret")
    monkeypatch.setenv("AGENT_MODEL", "fixture-model")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "100")
    async with session_scope() as session:
        listing_id = await seed(session)
    ref = ObjectRef(kind="listing", id=listing_id, revision=1).model_dump(mode="json")
    conversation = api_client.post(
        "/agent/conversations", headers=KEY, json={"context_refs": [ref]}
    ).json()
    url = f"/agent/conversations/{conversation['id']}/messages"
    body = {
        "client_message_id": str(uuid4()),
        "text": "Acquire this exact official original; leave facts unverified.",
        "context_refs": [ref],
        "research_kind": "local_check",
        "research_at": NOW.isoformat(),
        "disclosure_access": [
            {
                "listing_id": listing_id,
                "source": "cninfo.v1",
                "max_discoveries": 1,
                "max_fetches": 1,
            }
        ],
    }
    if not historical:
        body.pop("research_at")
    invalid = {**body, "client_message_id": str(uuid4()), "context_refs": []}
    assert api_client.post(url, headers=KEY, json=invalid).status_code == 422
    unknown = {
        **body,
        "client_message_id": str(uuid4()),
        "disclosure_access": [{"listing_id": listing_id, "source": "arbitrary"}],
    }
    assert api_client.post(url, headers=KEY, json=unknown).status_code == 422
    accepted = api_client.post(url, headers=KEY, json=body)
    assert accepted.status_code == 202, accepted.text
    task_id = UUID(accepted.json()["task"]["id"])
    monkeypatch.setenv("DISCLOSURE_ORIGINALS_ENABLED", "false")
    assert api_client.post(url, headers=KEY, json=body).json()["task"]["id"] == str(task_id)
    assert (
        api_client.post(
            url, headers=KEY, json={**body, "client_message_id": str(uuid4())}
        ).status_code
        == 422
    )
    assert (
        api_client.post(
            url,
            headers=KEY,
            json={**body, "disclosure_access": [{"listing_id": listing_id, "max_fetches": 2}]},
        ).status_code
        == 409
    )
    monkeypatch.setenv("DISCLOSURE_ORIGINALS_ENABLED", "true")
    async with session_scope() as session:
        grant = await session.get(ExecutionGrantRow, str(task_id))
        cutoff = datetime.fromisoformat(grant.payload["disclosure_permissions"][0]["cutoff"])
        assert cutoff == datetime.fromisoformat(accepted.json()["task"]["binding"]["as_of"])
        assert grant.payload["current_research"] is not historical
    http, calls = FixtureHTTP(), []

    def model(messages, info):
        calls.append(messages)
        if len(calls) == 1:
            assert "disclosure_permissions" in str(messages)
            part = ToolCallPart(
                "discover_disclosures",
                {
                    "business": discovery(listing_id)
                    .model_copy(update={"as_of": cutoff})
                    .model_dump(mode="json")
                },
            )
        elif len(calls) == 2:
            returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][
                -1
            ].content
            part = ToolCallPart(
                "fetch_disclosure_original",
                {
                    "business": FetchRequest(
                        listing_id=listing_id, item_id=returned["items"][0]["id"], as_of=cutoff
                    ).model_dump(mode="json")
                },
            )
        else:
            part = TextPart(
                ResearchOutput(
                    progress=ResearchProgress(
                        kind="local_check",
                        question=body["text"],
                        gaps=(
                            "Original acquired; parser and financial verification remain pending.",
                        ),
                        stopped_because="unparsed_original",
                        answered=False,
                    )
                ).model_dump_json()
            )
        return ModelResponse([part], usage=RequestUsage(input_tokens=20, output_tokens=10))

    selected = settings(disclosure_originals_enabled=True, api_key="secret")
    tools = tuple(
        t
        for t in registrations()
        if t.name not in {"discover_disclosures", "fetch_disclosure_original"}
    ) + source_registrations(test_http=http)
    executor = AgentExecutor(
        selected,
        model=FunctionModel(model),
        registrations=tools,
        specification=ResearchPolicy(selected).specification(),
    )
    worker = ResearchWorker(
        settings=selected,
        worker_id="api-disclosure-fixture",
        handlers=[AgentJobHandler("agent_turn", executor)],
    )
    assert await worker.run_once() is not None
    async with session_scope() as session:
        task = await TaskService(session).get_task(task_id)
        assert task.status.value == "partial", task.model_dump(mode="json")
        actions = (
            await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task_id)))
        ).all()
        assert all(a.state == "committed" for a in actions)
        assert (
            len(
                [
                    a
                    for a in actions
                    if a.parameters["tool"] in {"discover_disclosures", "fetch_disclosure_original"}
                ]
            )
            == 2
        )
        assert "fixture-original" not in json.dumps([a.result for a in actions])
        assert task.artifact_refs
    assert "fixture-original" not in str(calls)
    assert len(http.calls) == 3

    from investment_agent.agent.observation import effective_cutoff
    from investment_agent.evidence.store import EvidenceService

    async with session_scope() as session:
        fetch_action = next(
            a for a in actions if a.parameters["tool"] == "fetch_disclosure_original"
        )
        availability = fetch_action.result["evidence_availability"]
        assert availability["status"] == "missing" and availability["requires_new_message"]
        assert availability["reason"] == "acquired_after_requested_cutoff"
        assert datetime.fromisoformat(availability["readable_after"]) > cutoff
        assert await effective_cutoff(session, task_id) == cutoff
        evidence_ref = ObjectRef.model_validate(fetch_action.result["original"]["evidence_ref"])
        assert (
            await EvidenceService(session).get_evidence("local_owner", evidence_ref, cutoff)
        ).status == "missing"
    # A later authenticated message reuses the saved candidate without granting
    # another network acquisition or changing the old task's historical window.
    later = api_client.post(
        url,
        headers=KEY,
        json={
            "client_message_id": str(uuid4()),
            "text": "Read the saved candidate and identify the parsing gap.",
            "context_refs": [evidence_ref.model_dump(mode="json")],
            "research_kind": "local_check",
        },
    )
    assert later.status_code == 202, later.text
    later_id = UUID(later.json()["task"]["id"])
    later_cutoff = datetime.fromisoformat(later.json()["task"]["binding"]["as_of"])
    later_rounds = []

    def later_model(messages, info):
        later_rounds.append(messages)
        assert "discover_disclosures" not in {tool.name for tool in info.function_tools}
        if len(later_rounds) == 1:
            part = ToolCallPart(
                "get_evidence",
                {
                    "business": {
                        "ref": evidence_ref.model_dump(mode="json"),
                        "subject_ref": ref,
                        "as_of": later_cutoff.isoformat(),
                    }
                },
            )
        else:
            returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][
                -1
            ].content
            assert returned["status"] == "ok" and returned["data"]["verification"] == "candidate"
            part = TextPart(
                ResearchOutput(
                    progress=ResearchProgress(
                        kind="local_check",
                        question="Read saved candidate",
                        answered=False,
                        gaps=(
                            "Candidate is available now; parsing and verification remain pending.",
                        ),
                        stopped_because="unparsed_original",
                    )
                ).model_dump_json()
            )
        return ModelResponse([part], usage=RequestUsage(input_tokens=20, output_tokens=10))

    followup_executor = AgentExecutor(
        selected,
        model=FunctionModel(later_model),
        registrations=tools,
        specification=ResearchPolicy(selected).specification(),
    )
    followup_worker = ResearchWorker(
        settings=selected,
        worker_id="api-disclosure-followup",
        handlers=[AgentJobHandler("agent_turn", followup_executor)],
    )
    assert await followup_worker.run_once() is not None
    async with session_scope() as session:
        assert (await TaskService(session).get_task(later_id)).status.value == "partial"
        assert await effective_cutoff(session, task_id) == cutoff
    assert len(http.calls) == 3

"""Authenticated conversation → queue → ResearchPolicy → native actual parser."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from investment_agent.agent.executor import AgentExecutor
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.research import (
    ResearchOutput,
    ResearchPolicy,
    ResearchProgress,
    registrations,
)
from investment_agent.agent.tasks import TaskService
from investment_agent.evidence.documents import OriginalRequest
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from tests.support.execution import settings
from tests.support.originals import original, snapshot

KEY = {"X-API-Key": "secret"}


async def test_authenticated_saved_original_parse_readback_and_later_fact_visibility(
    api_client, monkeypatch
):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    monkeypatch.setenv("DISCLOSURE_ORIGINALS_ENABLED", "true")
    monkeypatch.setenv("AGENT_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("AGENT_API_KEY", "fixture-secret")
    monkeypatch.setenv("AGENT_MODEL", "fixture-model")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "100")
    async with session_scope() as session:
        payload, _, request = await original(session)
        await snapshot(session, request.listing_id)
    target = f"/investor/disclosures/originals/{request.original_id}/fragments"
    query = {"listing_id": request.listing_id, "as_of": datetime.now(UTC).isoformat()}
    assert api_client.get(target, params=query).status_code == 401
    response = api_client.get(target, headers=KEY, params=query)
    assert (
        response.status_code == 200
        and response.json()["candidate_facts"][0]["verification"] == "candidate"
    )
    assert (
        api_client.get(
            target, headers=KEY, params={**query, "listing_id": request.listing_id + 1}
        ).status_code
        == 404
    )
    ref = {"kind": "listing", "id": request.listing_id, "revision": 1}
    conversation = api_client.post(
        "/agent/conversations", headers=KEY, json={"context_refs": [ref]}
    ).json()
    body = {
        "client_message_id": str(uuid4()),
        "text": "Parse the saved consolidated statement; identify exact supported facts and remaining gaps.",
        "context_refs": [ref],
        "research_kind": "local_check",
        "disclosure_access": [
            {
                "listing_id": request.listing_id,
                "max_discoveries": 0,
                "max_fetches": 0,
                "max_parses": 1,
            }
        ],
    }
    accepted = api_client.post(
        f"/agent/conversations/{conversation['id']}/messages", headers=KEY, json=body
    )
    assert accepted.status_code == 202, accepted.text
    task_id = UUID(accepted.json()["task"]["id"])
    cutoff = datetime.fromisoformat(accepted.json()["task"]["binding"]["as_of"])
    rounds = []

    def model(messages, info):
        rounds.append(messages)
        if len(rounds) == 1:
            assert {"read_disclosure_original", "parse_disclosure_original"} <= {
                t.name for t in info.function_tools
            }
            schema = next(
                t.parameters_json_schema
                for t in info.function_tools
                if t.name == "parse_disclosure_original"
            )
            assert "verification" not in str(schema) and "url" not in str(schema)
            part = ToolCallPart(
                "parse_disclosure_original",
                {"business": request.model_copy(update={"as_of": cutoff}).model_dump(mode="json")},
            )
        else:
            returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][
                -1
            ].content
            assert returned["untrusted_source_data"] and returned["requires_new_message"]
            assert returned["facts"][0]["verification"] == "verified"
            assert "<script>" not in str(returned) and "fetch(" not in str(returned)
            part = TextPart(
                ResearchOutput(
                    progress=ResearchProgress(
                        kind="local_check",
                        question=body["text"],
                        answered=False,
                        gaps=(
                            "New deterministic parse is saved; a later message is required to consume it.",
                        ),
                        stopped_because="new_parse_after_cutoff",
                    )
                ).model_dump_json()
            )
        return ModelResponse([part], usage=RequestUsage(input_tokens=20, output_tokens=10))

    selected = settings(disclosure_originals_enabled=True, api_key="secret")
    executor = AgentExecutor(
        selected,
        model=FunctionModel(model),
        registrations=registrations(),
        specification=ResearchPolicy(selected).specification(),
    )
    worker = ResearchWorker(
        settings=selected,
        worker_id="original-parse-fixture",
        handlers=[AgentJobHandler("agent_turn", executor)],
    )
    assert await worker.run_once() is not None
    async with session_scope() as session:
        task = await TaskService(session).get_task(task_id)
        actions = (
            await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task_id)))
        ).all()
        parsed = next(a for a in actions if a.parameters.get("tool") == "parse_disclosure_original")
        assert parsed.state == "committed" and task.status.value == "partial"
        fact_ref = parsed.result["fact_refs"][0]
    endpoint = f"/investor/evidence/{fact_ref['id']}"
    assert (
        api_client.get(endpoint, headers=KEY, params={"as_of": cutoff.isoformat()}).status_code
        == 404
    )
    assert api_client.get(endpoint, headers=KEY).json()["data"]["verification"] == "verified"
    later = api_client.post(
        f"/agent/conversations/{conversation['id']}/messages",
        headers=KEY,
        json={
            "client_message_id": str(uuid4()),
            "text": "Read the now available exact saved fact and its origin.",
            "context_refs": [fact_ref],
            "research_kind": "local_check",
        },
    )
    assert later.status_code == 202, later.text
    later_cutoff = later.json()["task"]["binding"]["as_of"]
    later_rounds = []

    def read_model(messages, info):
        later_rounds.append(messages)
        assert "parse_disclosure_original" not in {t.name for t in info.function_tools}
        if len(later_rounds) == 1:
            part = ToolCallPart(
                "get_evidence",
                {"business": {"ref": fact_ref, "subject_ref": ref, "as_of": later_cutoff}},
            )
        else:
            returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][
                -1
            ].content
            assert returned["data"]["verification"] == "verified"
            assert (
                returned["data"]["document_provenance"]["original_hash"] == payload["content_hash"]
            )
            part = TextPart(
                ResearchOutput(
                    progress=ResearchProgress(
                        kind="local_check",
                        question="Read exact saved original fact",
                        answered=False,
                        gaps=("One annual fact is insufficient for a whole company view.",),
                        stopped_because="bounded_fact_only",
                    )
                ).model_dump_json()
            )
        return ModelResponse([part], usage=RequestUsage(input_tokens=20, output_tokens=10))

    reader = AgentExecutor(
        selected,
        model=FunctionModel(read_model),
        registrations=registrations(),
        specification=ResearchPolicy(selected).specification(),
    )
    read_worker = ResearchWorker(
        settings=selected,
        worker_id="original-read-fixture",
        handlers=[AgentJobHandler("agent_turn", reader)],
    )
    assert await read_worker.run_once() is not None and len(later_rounds) == 2
    async with session_scope() as session:
        from investment_agent.agent.observation import effective_cutoff

        assert await effective_cutoff(session, task_id) == cutoff
    monkeypatch.setenv("DISCLOSURE_ORIGINALS_ENABLED", "false")
    assert api_client.get(target, headers=KEY, params=query).status_code == 200
    assert api_client.get(endpoint, headers=KEY).status_code == 200
    assert api_client.get(target, headers=KEY, params={**query, "limit": 21}).status_code == 422
    assert OriginalRequest.model_json_schema()["additionalProperties"] is False

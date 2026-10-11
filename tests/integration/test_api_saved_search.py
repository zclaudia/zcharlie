"""Authenticated message → queue → native ResearchPolicy → FunctionModel search."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from investment_agent.agent.executor import AgentExecutor
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.research import (
    ResearchOutput,
    ResearchPolicy,
    ResearchProgress,
    registrations,
)
from investment_agent.agent.tasks import TaskService
from investment_agent.evidence.documents import parse_staged, save_candidates
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from tests.test_agent_execution import settings
from tests.test_original_parsing import original

KEY = {"X-API-Key": "secret"}


@pytest.mark.parametrize("forbidden", [False, True])
async def test_authenticated_selected_listing_search_native_without_original_write_grant(api_client, monkeypatch, forbidden):
    monkeypatch.setenv("INVESTOR_AGENT_ENABLED", "true")
    monkeypatch.setenv("AGENT_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("AGENT_API_KEY", "fixture-secret")
    monkeypatch.setenv("AGENT_MODEL", "fixture-model")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "100")
    async with session_scope() as session:
        payload, exact, request = await original(session)
        await save_candidates(session, payload, exact, await parse_staged(exact, "text/html"), request)
    query = {"listing_ids": [request.listing_id], "query": "净利润", "as_of": datetime.now(UTC).isoformat()}
    assert api_client.post('/investor/disclosures/search', json=query).status_code == 401
    response = api_client.post('/investor/disclosures/search', headers=KEY, json=query)
    assert response.status_code == 200 and response.json()['matches']
    ref = {"kind": "listing", "id": request.listing_id, "revision": 1}
    conversation = api_client.post('/agent/conversations', headers=KEY, json={"context_refs": [ref]}).json()
    accepted = api_client.post(f"/agent/conversations/{conversation['id']}/messages", headers=KEY, json={
        "client_message_id": str(uuid4()), "text": "搜索已保存净利润材料，保留来源线索和局限。",
        "context_refs": [ref], "research_kind": "local_check"})
    assert accepted.status_code == 202, accepted.text
    task_id = UUID(accepted.json()['task']['id'])
    query['as_of'] = accepted.json()['task']['binding']['as_of']
    if forbidden:
        query['listing_ids'] = [request.listing_id+1]
    rounds = []

    def model(messages, info):
        rounds.append(messages)
        assert 'search_saved_sources' in {t.name for t in info.function_tools}
        assert 'fetch_disclosure_original' not in {t.name for t in info.function_tools}
        if len(rounds) == 1:
            part = ToolCallPart('search_saved_sources', {"business": query})
        else:
            returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][-1].content
            assert returned['untrusted_source_data'] and returned['matches'][0]['discovery_only']
            assert returned['matches'][0]['original_hash'] == payload['content_hash']
            part = TextPart(ResearchOutput(progress=ResearchProgress(kind='local_check', question='搜索保存材料', answered=False,
                gaps=('搜索提供候选线索；需要核验才能支持关键主张。',), stopped_because='discovery_only')).model_dump_json())
        return ModelResponse([part], usage=RequestUsage(input_tokens=20, output_tokens=10))

    base = settings(api_key='secret')
    executor = AgentExecutor(base, model=FunctionModel(model), registrations=registrations(), specification=ResearchPolicy(base).specification())
    worker = ResearchWorker(settings=base, worker_id='saved-search-fixture', handlers=[AgentJobHandler('agent_turn', executor)])
    assert await worker.run_once() is not None
    async with session_scope() as session:
        task = await TaskService(session).get_task(task_id)
        assert task.status.value == ('failed' if forbidden else 'partial'), str(task.model_dump(mode='json'))
    assert len(rounds) == (1 if forbidden else 2)

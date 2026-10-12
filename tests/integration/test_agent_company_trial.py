"""Offline verification of the explicit trial runner; never real model acceptance."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from tools.agent_company_trial import run_trial

from investment_agent.agent.research import ResearchOutput
from investment_agent.investor.contracts import DIMENSIONS, DimensionAssessment
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.support.publication import publication_db as publication_db
from tests.support.research import full_progress


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_mode", [False, True])
async def test_trial_runner_three_explicit_company_tasks_cutoff_usage_unknown_cost(publication_db, tmp_path, monkeypatch, transport_mode):
    # Separate fixture identities: the real-company packet is never used by FunctionModel.
    cutoff = datetime.now(UTC) - timedelta(seconds=1)
    async with session_scope() as session:
        session.add(Market(code="SH", name="fixture", timezone="Asia/Shanghai", currency="CNY"))
        companies = []
        for index in range(3):
            company = Company(canonical_name=f"Fictional fixture {index}")
            session.add(company)
            await session.flush()
            listing = Listing(company_id=company.id, market_code="SH", ticker=f"60000{index}")
            session.add(listing)
            await session.flush()
            companies.append({"identity": {"id": listing.id, "canonical_name": company.canonical_name}, "cutoff": cutoff.isoformat()})
    # publication_db is a file; the runner intentionally opens its own engine.
    await publication_db.dispose()
    original = tmp_path / "publication.db"
    original.rename(tmp_path / "trial.db")
    (tmp_path / "ag20-isolated").touch()
    (tmp_path / "materials.json").write_text(json.dumps({"companies": companies, "material_set_hash": "fictional-offline-test"}))
    config = tmp_path / "explicit.json"
    config.write_text(json.dumps({"agent_base_url": "https://fixture.invalid/v1", "agent_model": "fixture"}))
    monkeypatch.setenv("AG20_AGENT_API_KEY", "inert")
    dimensions = tuple(DimensionAssessment(dimension=dimension, status="missing", applicable=True, reason="No verified fixture material", refs=()) for dimension in DIMENSIONS)
    progress = full_progress(dimensions, answered=False, gaps=("Official material unavailable",))
    model = FunctionModel(lambda messages, info: ModelResponse([TextPart(ResearchOutput(progress=progress).model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=5)))
    if transport_mode:
        import httpx
        def respond(request):
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": ResearchOutput(progress=progress).model_dump_json()}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 49900, "completion_tokens": 0, "total_tokens": 49900}})
        kwargs = {"_test_transport": httpx.MockTransport(respond)}
    else:
        kwargs = {"_test_model": model}
    await run_trial(tmp_path, config, **kwargs)
    report = json.loads((tmp_path / "actual-results.json").read_text())
    assert len(report["results"]) == 3 and report["state"] == "bounded trial completed"
    for item in report["results"]:
        assert datetime.fromisoformat(item["actual_binding_cutoff"]) == cutoff
        assert item["task"]["status"] == "partial"
        assert item["model_runs"][0]["usage"]["input_tokens"] == (49900 if transport_mode else 10)
        assert item["model_runs"][0]["http_attempts"] == (1 if transport_mode else None)
        assert item["money_cost_usd"] is None
        assert item["semantic_acceptance"] == "human pending"
    with pytest.raises(ValueError, match="refuse repeat"):
        await run_trial(tmp_path, config, **kwargs)
    # Simulate a new business day: the tool still subtracts lifetime prior spend.
    async with session_scope() as session:
        from sqlalchemy import update

        from investment_agent.storage.models.agent import BudgetEntry
        await session.execute(update(BudgetEntry).values(day="2020-01-01"))
    await run_trial(tmp_path, config, **kwargs, retry_existing=True)
    repaired = json.loads((tmp_path / "actual-results-repair-1.json").read_text())
    assert json.loads((tmp_path / "actual-results.json").read_text()) == report
    assert len(repaired["results"]) == 3
    for index, item in enumerate(repaired["results"]):
        assert item["previous_failed_task_id"] == report["results"][index]["task"]["id"]
        assert item["task"]["parent_task_id"] == item["previous_failed_task_id"]
        remaining = item["cumulative_remaining_before_attempt"]
        assert remaining["requests"] == 7 and remaining["tokens"] == (100 if transport_mode else 49985)
        assert remaining["total_used_requests"] == (3 if transport_mode else 3 + index)
        assert remaining["total_used_tokens"] == (149700 if transport_mode else 45 + index * 15)
    with pytest.raises(ValueError, match="refuse repeat"):
        await run_trial(tmp_path, config, **kwargs, retry_existing=True)

    if not transport_mode:
        def conclude(messages, info):
            assert not info.function_tools and 'recovered_evidence' in info.instructions
            final = progress.model_copy(update={'stopped_because': 'No verified material is available for a direction.'})
            return ModelResponse([TextPart(ResearchOutput(progress=final).model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=5))
        await run_trial(tmp_path, config, _test_model=FunctionModel(conclude), retry_existing=True, conclude_existing=True)
        closed = json.loads((tmp_path / 'actual-results-conclusion.json').read_text())
        for index, item in enumerate(closed['results']):
            assert item['task']['parent_task_id'] == repaired['results'][index]['task']['id']
            assert item['cumulative_remaining_before_attempt']['requests'] == 1
            assert item['cumulative_remaining_before_attempt']['tokens'] == 49970
            assert item['task']['usage']['requests'] == 1 and item['task']['usage']['tool_calls'] == 0
            assert item['model_runs'][0]['success']
        assert closed['limits']['tokens_per_company'] == 50000
        assert json.loads((tmp_path / 'actual-results.json').read_text()) == report
        assert json.loads((tmp_path / 'actual-results-repair-1.json').read_text()) == repaired
        with pytest.raises(ValueError, match='refuse repeat'):
            await run_trial(tmp_path, config, _test_model=FunctionModel(conclude), retry_existing=True, conclude_existing=True)

    if transport_mode:
        assert all(item["model_runs"][0]["http_attempts"] == 0 for item in repaired["results"])
        await run_trial(tmp_path, config, **kwargs, retry_existing=True, repair_after_unsent=True)
        final = json.loads((tmp_path / "actual-results-repair-2.json").read_text())
        assert json.loads((tmp_path / "actual-results-repair-1.json").read_text()) == repaired
        for index, item in enumerate(final["results"]):
            assert item["task"]["parent_task_id"] == repaired["results"][index]["task"]["id"]
            assert item["cumulative_remaining_before_attempt"]["tokens"] == 100
            assert item["cumulative_remaining_before_attempt"]["total_used_tokens"] == 149700
            assert item["model_runs"][0]["http_attempts"] == 0
        with pytest.raises(ValueError, match="refuse repeat"):
            await run_trial(tmp_path, config, **kwargs, retry_existing=True, repair_after_unsent=True)
    else:
        with pytest.raises(ValueError, match="not proven zero HTTP"):
            await run_trial(tmp_path, config, **kwargs, retry_existing=True, repair_after_unsent=True)

"""Real HTTP admission, persistent worker and read-only account-fit reads."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.suitability_contracts import AccountFitRequest, FitRequestItem
from investment_agent.portfolio.suitability_jobs import AccountFitHandler
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.portfolio import PortfolioAccount
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import make_inputs
from tests.support.account_fit_flow import persist_inputs

KEY = {"X-API-Key": "secret"}


@pytest.fixture
def flags(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT_FIT_ENABLED", "true")
    monkeypatch.setenv("CANDIDATE_RANKING_ENABLED", "true")


async def _prepare() -> AccountFitRequest:
    inputs = make_inputs(target_listing=1)
    async with session_scope() as session:
        await persist_inputs(session, inputs)
    return AccountFitRequest(operation_id=uuid4(), account_id=inputs.account_id, snapshot_ref=inputs.snapshot.reference, mandate_ref=ObjectRef(kind="mandate", id=inputs.mandate.id, revision=1), valuation_binding=inputs.valuation_binding, purpose_binding=inputs.purpose_binding, items=(FitRequestItem(listing_ref=inputs.listing_sources[0].listing_ref, action="open", trade_assumptions=assumptions()),))


async def _counts() -> dict[str, int]:
    async with session_scope() as session:
        return {table.name: int(await session.scalar(select(func.count()).select_from(table)) or 0) for table in Base.metadata.sorted_tables}


async def test_real_http_worker_unknown_and_readonly_reads(api_client, flags) -> None:
    request = await _prepare()
    before = await _counts()
    context = api_client.get("/portfolio/fit-context", params={"account_id": str(request.account_id), "listing_id": 1}, headers=KEY)
    assert context.status_code == 200, context.text
    assert context.json()["enabled"]["account_fit"] is True
    missing = api_client.get("/portfolio/fit-assessments/current", params={"account_id": str(request.account_id), "listing_id": 1, "action": "open", "purpose_binding_key": "a" * 64, "calculation_basis_key": "b" * 64}, headers=KEY)
    assert missing.status_code == 200 and missing.json()["status"] == "missing"
    assert await _counts() == before
    accepted = api_client.post("/portfolio/fit-assessments", json=request.model_dump(mode="json"), headers=KEY)
    assert accepted.status_code == 202, accepted.text
    receipt = accepted.json()
    replay = api_client.post("/portfolio/fit-assessments", json=request.model_dump(mode="json"), headers=KEY)
    assert replay.status_code == 202 and replay.json()["replayed"] is True
    assert replay.json()["run_ref"] == receipt["run_ref"]
    worker = ResearchWorker(settings=Settings(), worker_id="fit-integration", handlers=[AccountFitHandler(settings=Settings())])
    assert await worker.run_once(run_id=receipt["run_ref"]["id"]) == receipt["run_ref"]["id"]
    run = api_client.get(f'/investment-assessment-runs/{receipt["run_ref"]["id"]}', headers=KEY)
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "success", run.text
    assert "client_request" not in run.json() and "frozen_inputs" not in run.json()
    fit_id = run.json()["fit_refs"][0]["id"]
    before_reads = await _counts()
    exact = api_client.get(f"/portfolio/fit-assessments/{fit_id}", params={"account_id": str(request.account_id)}, headers=KEY)
    assert exact.status_code == 200, exact.text
    fit = exact.json()["payload"]
    assert fit["financial_status"] == "unknown"
    assert fit["ranking_bucket"] == "incomplete" and fit["research_status"] == "not_assessed"
    assert fit["supplemental_binding"] == "not_integrated"
    current = api_client.get("/portfolio/fit-assessments/current", params={"account_id": str(request.account_id), "listing_id": 1, "action": "open", "purpose_binding_key": fit["purpose_binding_key"], "calculation_basis_key": fit["calculation_basis_key"]}, headers=KEY)
    assert current.status_code == 200 and current.json()["payload"]["id"] == fit_id
    assert await _counts() == before_reads
    async with session_scope() as session:
        assert list(await session.scalars(select(PipelineRun.kind))) == ["account_fit_evaluate"]
        assert await session.scalar(select(func.count()).select_from(InvestmentAssessment)) == 1


async def test_http_auth_csrf_conflict_and_receipt_after_disable(api_client, flags, monkeypatch) -> None:
    request = await _prepare()
    body = request.model_dump(mode="json")
    assert api_client.get("/portfolio/fit-context", params={"account_id": str(request.account_id)}).status_code == 401
    api_client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert api_client.post("/portfolio/fit-assessments", json=body, headers={"Origin": "https://evil.invalid"}).status_code == 403
    accepted = api_client.post("/portfolio/fit-assessments", json=body, headers=KEY)
    assert accepted.status_code == 202, accepted.text
    changed = {**body, "items": [{**body["items"][0], "trade_assumptions": assumptions(fixed_fee="11").model_dump(mode="json")}]}
    assert api_client.post("/portfolio/fit-assessments", json=changed, headers=KEY).status_code == 409
    monkeypatch.setenv("ACCOUNT_FIT_ENABLED", "false")
    replay = api_client.post("/portfolio/fit-assessments", json=body, headers=KEY)
    assert replay.status_code == 202 and replay.json()["replayed"] is True
    fresh = {**body, "operation_id": str(uuid4())}
    assert api_client.post("/portfolio/fit-assessments", json=fresh, headers=KEY).status_code == 403
    worker = ResearchWorker(settings=Settings(), worker_id="fit-disabled", handlers=[AccountFitHandler(settings=Settings())])
    await worker.run_once(run_id=accepted.json()["run_ref"]["id"])
    status = api_client.get(f'/investment-assessment-runs/{accepted.json()["run_ref"]["id"]}', headers=KEY).json()
    assert status["status"] == "failed" and status["failure_code"] == "writes_disabled"
    assert status["fit_refs"] == []


async def test_snapshot_missing_valuation_missing_and_wrong_account(api_client, flags) -> None:
    request = await _prepare()
    async with session_scope() as session:
        account = await session.get(PortfolioAccount, str(request.account_id))
        account.current_snapshot_id = None
    context = api_client.get("/portfolio/fit-context", params={"account_id": str(request.account_id)}, headers=KEY)
    assert context.status_code == 200 and "snapshot_missing" in context.json()["gaps"]
    before = await _counts()
    rejected = api_client.post("/portfolio/fit-assessments", json=request.model_dump(mode="json"), headers=KEY)
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "snapshot_required"
    assert await _counts() == before
    wrong = api_client.get("/portfolio/fit-context", params={"account_id": str(uuid4())}, headers=KEY)
    assert wrong.status_code == 404
    async with session_scope() as session:
        account = await session.get(PortfolioAccount, str(request.account_id))
        account.current_snapshot_id = str(request.snapshot_ref.id)
        account.current_valuation_id = None
    context = api_client.get("/portfolio/fit-context", params={"account_id": str(request.account_id)}, headers=KEY).json()
    assert context["snapshot"] is not None and context["valuation_binding"]["state"] == "missing"
    body = request.model_dump(mode="json")
    body["valuation_binding"] = context["valuation_binding"]
    assert api_client.post("/portfolio/fit-assessments", json=body, headers=KEY).status_code == 202


async def test_historical_replay_is_bound_to_original_manifest(api_client, flags) -> None:
    request = await _prepare()
    accepted = api_client.post("/portfolio/fit-assessments", json=request.model_dump(mode="json"), headers=KEY).json()
    async with session_scope() as session:
        original_run = await session.get(PipelineRun, accepted["run_ref"]["id"])
        as_of = original_run.request_json["frozen_inputs"]["assessment_as_of"]
    body = request.model_dump(mode="json")
    body.update(operation_id=str(uuid4()), mode="historical_replay", historical_replay={"run_ref": accepted["run_ref"], "manifest_hash": accepted["source_manifest_hash"], "input_as_of": as_of})
    replay = api_client.post("/portfolio/fit-assessments", json=body, headers=KEY)
    assert replay.status_code == 202, replay.text
    body["operation_id"] = str(uuid4())
    body["historical_replay"]["manifest_hash"] = "0" * 64
    assert api_client.post("/portfolio/fit-assessments", json=body, headers=KEY).status_code == 422
    body["historical_replay"]["run_ref"]["id"] = 99999
    assert api_client.post("/portfolio/fit-assessments", json=body, headers=KEY).status_code == 422

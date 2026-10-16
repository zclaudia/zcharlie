"""Targeted P2 recovery regressions over the HTTP/SQLite boundary."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investor import InvestorMutation
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.supplemental_rules.canonical import mutation_request_hash
from investment_agent.supplemental_rules.contracts import EvaluationRequest, FrozenManifest
from investment_agent.supplemental_rules.service import manifest_digest

API_KEY = {"X-API-Key": "secret"}


def _admit_empty_run(api_client, monkeypatch) -> int:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    async def seed_listing() -> int:
        async with session_scope() as session:
            session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
            company = Company(canonical_name="P2 checkpoint review fixture")
            session.add(company)
            await session.flush()
            listing = Listing(company_id=company.id, market_code="SSE", ticker="600001")
            session.add(listing)
            await session.flush()
            return listing.id

    listing_id = asyncio.run(seed_listing())
    admitted = api_client.post(
        "/supplemental-rule-evaluations",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "listing_refs": [{"kind": "listing", "id": listing_id, "revision": 1}],
            "contexts": ["company_research"],
            "mode": "live",
        },
    )
    assert admitted.status_code == 202, admitted.text
    return admitted.json()["admission"]["run_ref"]["id"]


def test_run_view_rejects_forged_checkpoint_failure_code(api_client, monkeypatch) -> None:
    """A damaged checkpoint cannot invent a terminal failed item in a run view."""
    run_id = _admit_empty_run(api_client, monkeypatch)

    async def corrupt_checkpoint() -> None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            run.metadata_json = {
                "supplemental_checkpoint": {
                    "schema_version": 1,
                    "source_manifest_hash": run.request_json["source_manifest_hash"],
                    "updated_at": datetime.now(UTC).isoformat(),
                    "members": [
                        {
                            "ordinal": 0,
                            "state": "item_failed",
                            "application_ref": None,
                            "input_hash": None,
                            "error_code": "forged_failure",
                        }
                    ],
                }
            }

    asyncio.run(corrupt_checkpoint())
    response = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert response.status_code == 422, response.text
    assert "检查点无效" in response.text


def test_run_view_rejects_admission_receipt_field_mismatch(api_client, monkeypatch) -> None:
    run_id = _admit_empty_run(api_client, monkeypatch)

    async def corrupt_admission() -> None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            key = run.request_json["operation_id"]
            receipt = await session.get(InvestorMutation, ("local_owner", key))
            assert receipt is not None
            changed = deepcopy(receipt.result)
            changed["result"]["admission"]["operation_id"] = str(uuid4())
            await session.execute(
                update(InvestorMutation)
                .where(InvestorMutation.owner_id == "local_owner", InvestorMutation.key == key)
                .values(result=changed)
            )

    asyncio.run(corrupt_admission())
    response = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert response.status_code == 404, response.text

    async def corrupt_admission_time() -> None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            key = run.request_json["operation_id"]
            receipt = await session.get(InvestorMutation, ("local_owner", key))
            assert receipt is not None
            changed = deepcopy(receipt.result)
            changed["result"]["admission"]["operation_id"] = key
            changed["result"]["admission"]["accepted_at"] = "2000-01-01T00:00:00+00:00"
            await session.execute(
                update(InvestorMutation)
                .where(InvestorMutation.owner_id == "local_owner", InvestorMutation.key == key)
                .values(result=changed)
            )

    asyncio.run(corrupt_admission_time())
    time_mismatch = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert time_mismatch.status_code == 404, time_mismatch.text


def test_run_view_rejects_unbound_manifest_evaluated_at(api_client, monkeypatch) -> None:
    run_id = _admit_empty_run(api_client, monkeypatch)

    async def corrupt_evaluated_at() -> None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            payload = deepcopy(run.request_json)
            payload["source_manifest"]["evaluated_at"] = "2000-01-01T00:00:00+00:00"
            run.request_json = payload

    asyncio.run(corrupt_evaluated_at())
    response = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert response.status_code == 422, response.text


def test_run_view_rejects_rehashed_manifest_and_acceptance_tampering(api_client, monkeypatch) -> None:
    run_id = _admit_empty_run(api_client, monkeypatch)

    async def rewrite_self_hashed_payload() -> None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            payload = deepcopy(run.request_json)
            changed_time = datetime(2000, 1, 1, tzinfo=UTC)
            manifest = FrozenManifest.model_validate(payload["source_manifest"]).model_copy(
                update={"as_of": changed_time, "evaluated_at": changed_time}
            )
            digest = manifest_digest(manifest)
            manifest = manifest.model_copy(update={"input_hash": digest, "manifest_hash": digest})
            payload["accepted_at"] = changed_time.isoformat()
            payload["source_manifest"] = manifest.model_dump(mode="json")
            payload["source_manifest_hash"] = digest
            run.request_json = payload

    asyncio.run(rewrite_self_hashed_payload())
    response = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert response.status_code == 404, response.text


def test_run_view_rejects_rehashed_request_body_against_receipt(api_client, monkeypatch) -> None:
    run_id = _admit_empty_run(api_client, monkeypatch)

    async def rewrite_request_digest() -> None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            payload = deepcopy(run.request_json)
            parsed = EvaluationRequest.model_validate(payload["command_body"])
            command_body = parsed.model_copy(
                update={"contexts": frozenset((*parsed.contexts, "candidate"))}
            )
            payload["command_body"] = command_body.model_dump(mode="json")
            payload["request_hash"] = mutation_request_hash(
                payload["command"], None, command_body
            )
            run.request_json = payload

    asyncio.run(rewrite_request_digest())
    response = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert response.status_code == 404, response.text


def test_run_view_rejects_execution_purpose_envelope_mismatch(api_client, monkeypatch) -> None:
    run_id = _admit_empty_run(api_client, monkeypatch)

    async def rewrite_execution_envelope() -> None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            payload = deepcopy(run.request_json)
            payload["execution_envelope"]["evaluation_purpose"] = "issue_review"
            run.request_json = payload

    asyncio.run(rewrite_execution_envelope())
    response = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert response.status_code == 404, response.text

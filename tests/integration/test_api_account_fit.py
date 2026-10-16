"""Admission races, historical replay isolation and safety capacities on SQLite."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from investment_agent.candidates.lifecycle import CandidatePolicy
from investment_agent.candidates.ranking_contracts import RankingSourcePolicy
from investment_agent.candidates.ranking_sources import (
    freeze_candidate_sources,
    resolve_source_runs,
)
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.calculations import TrustedTradingRule
from investment_agent.portfolio.contracts import Account
from investment_agent.portfolio.suitability_contracts import (
    AccountFitRequest,
    FitRequestItem,
    FrozenAccountInputs,
    HistoricalReplayBinding,
)
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.portfolio.suitability_jobs import (
    AccountFitAdmission,
    AccountFitHandler,
    AdmissionReceipt,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.portfolio import PortfolioAccount, PortfolioSnapshot
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import AS_OF, make_inputs, make_trusted_rule
from tests.support.account_fit_flow import persist_inputs
from tests.unit._candidate_support import cand_db as candidate_db_fixture  # noqa: F401

KEY = {"X-API-Key": "secret"}


class _AvailableRules:
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime) -> TrustedTradingRule | None:
        return make_trusted_rule(listing_ref)


@pytest.fixture
def enabled_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT_FIT_ENABLED", "true")
    monkeypatch.setenv("CANDIDATE_RANKING_ENABLED", "false")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="secret",
        llm_tier3_api_key="test",
        data_dir=tmp_path,
        portfolio_ledger_enabled=True,
        account_fit_enabled=True,
        candidate_ranking_enabled=False,
    )


async def _request(
    *, with_rule: bool = False, items: tuple[FitRequestItem, ...] | None = None
) -> tuple[FrozenAccountInputs, AccountFitRequest]:
    inputs = make_inputs(target_listing=600000, rules=(600000,) if with_rule else ())
    async with session_scope() as session:
        await persist_inputs(session, inputs)
    item = FitRequestItem(
        listing_ref=ObjectRef(kind="listing", id=600000, revision=1),
        action="open",
        trade_assumptions=assumptions(),
    )
    return inputs, AccountFitRequest(
        operation_id=uuid4(),
        account_id=inputs.account_id,
        snapshot_ref=inputs.snapshot.reference,
        mandate_ref=ObjectRef(kind="mandate", id=inputs.mandate.id, revision=1),
        valuation_binding=inputs.valuation_binding,
        purpose_binding=inputs.purpose_binding,
        items=items or (item,),
    )


async def _accept(request: AccountFitRequest, tmp_path: Path) -> AdmissionReceipt:
    async with session_scope() as session:
        return await AccountFitAdmission(session, settings=_settings(tmp_path)).accept(request)


async def _run(run_id: int, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    worker = ResearchWorker(
        settings=settings,
        worker_id="account-fit-integration",
        handlers=[AccountFitHandler(settings=settings)],
    )
    await worker.run_once(run_id=run_id)


@pytest.mark.asyncio
async def test_pc04_concurrent_same_key_admission_has_one_receipt(
    api_client: Any, enabled_flags: None, tmp_path: Path
) -> None:
    _, request = await _request()

    first, second = await asyncio.gather(
        _accept(request, tmp_path),
        _accept(request, tmp_path),
    )

    assert first.run_ref == second.run_ref
    assert first.request_hash == second.request_hash
    assert first.replayed != second.replayed
    async with session_scope() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.kind == "account_fit_evaluate")
            )
            == 1
        )
    response = api_client.get(f"/investment-assessment-runs/{first.run_ref.id}", headers=KEY)
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_pc04_old_exact_snapshot_conflicts_after_previewed_head_advance(
    api_client: Any, enabled_flags: None, tmp_path: Path
) -> None:
    inputs, request = await _request()
    async with session_scope() as session:
        preview = await SuitabilityInputService(session).context(inputs.account_id)
    assert preview.snapshot is not None
    assert preview.snapshot.reference == request.snapshot_ref

    newer_id = uuid4()
    newer_snapshot = inputs.snapshot.model_copy(
        update={
            "id": newer_id,
            "revision": inputs.snapshot.revision + 1,
            "recorded_at": AS_OF + timedelta(seconds=1),
            "reference": ObjectRef(
                kind="portfolio_snapshot", id=newer_id, revision=inputs.snapshot.revision + 1
            ),
        }
    )
    newer_account = Account(
        id=inputs.account_id,
        name="Synthetic CNY account",
        currency="CNY",
        revision=newer_snapshot.revision,
        current_snapshot_id=newer_id,
    )
    async with session_scope() as session:
        session.add(
            PortfolioSnapshot(
                id=str(newer_id),
                account_id=str(inputs.account_id),
                revision=newer_snapshot.revision,
                payload=newer_snapshot.model_dump(mode="json"),
            )
        )
        account = await session.get(PortfolioAccount, str(inputs.account_id))
        assert account is not None
        account.revision = newer_account.revision
        account.current_snapshot_id = str(newer_id)
        account.payload = newer_account.model_dump(mode="json")

    with pytest.raises(InvestorError) as conflict:
        await _accept(request, tmp_path)
    assert conflict.value.detail.code == "revision_conflict"
    async with session_scope() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.kind == "account_fit_evaluate")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_pc11_historical_worker_does_not_enter_live_current_even_if_rule_appears(
    api_client: Any, enabled_flags: None, tmp_path: Path
) -> None:
    inputs, request = await _request(with_rule=False)
    original_response = api_client.post(
        "/portfolio/fit-assessments", json=request.model_dump(mode="json"), headers=KEY
    )
    assert original_response.status_code == 202, original_response.text
    original = cast(dict[str, Any], original_response.json())
    await _run(int(original["run_ref"]["id"]), tmp_path)

    async with session_scope() as session:
        original_run = await session.get(PipelineRun, int(original["run_ref"]["id"]))
        assert original_run is not None and original_run.status == "success"
        frozen = cast(dict[str, Any], original_run.request_json["frozen_inputs"])
        input_as_of = frozen["assessment_as_of"]
        assert frozen["listing_sources"][0]["trusted_rule"] is None
        original_fit = await session.scalar(select(InvestmentAssessment))
        assert original_fit is not None
        original_payload = original_fit.payload
        assert original_payload["financial_status"] == "unknown"

    # Model a provider becoming available after the frozen run. The current
    # input resolver can now return a rule, but historical replay must keep the
    # original absence as an unknown.
    async with session_scope() as session:
        available_now = await SuitabilityInputService(session, rules=_AvailableRules()).freeze(
            request, as_of=datetime.now(UTC)
        )
    assert available_now.listing_sources[0].trusted_rule is not None

    replay_request = request.model_copy(
        update={
            "operation_id": uuid4(),
            "mode": "historical_replay",
            "historical_replay": HistoricalReplayBinding(
                run_ref=ObjectRef.model_validate(original["run_ref"]),
                manifest_hash=original["source_manifest_hash"],
                input_as_of=datetime.fromisoformat(input_as_of),
            ),
        }
    )
    replay_response = api_client.post(
        "/portfolio/fit-assessments",
        json=replay_request.model_dump(mode="json"),
        headers=KEY,
    )
    assert replay_response.status_code == 202, replay_response.text
    replay = cast(dict[str, Any], replay_response.json())
    await _run(int(replay["run_ref"]["id"]), tmp_path)

    async with session_scope() as session:
        replay_run = await session.get(PipelineRun, int(replay["run_ref"]["id"]))
        assert replay_run is not None and replay_run.status == "success"
        replay_frozen = cast(dict[str, Any], replay_run.request_json["frozen_inputs"])
        assert replay_frozen["mode"] == "historical_replay"
        assert replay_frozen["listing_sources"][0]["trusted_rule"] is None
        replay_metadata = cast(dict[str, Any], replay_run.metadata_json)
        replay_fit = await session.get(
            InvestmentAssessment,
            (replay_metadata["completed"]["600000"]["fit_ref"]["id"], 1),
        )
        assert replay_fit is not None
        assert replay_fit.payload["financial_status"] == "unknown"
        assert replay_fit.payload["frozen_inputs"]["mode"] == "historical_replay"

    # The endpoint's identity filter must keep returning the live fit; replay
    # is audit-only even if an injected current resolver can now supply a rule.
    body = original_fit.payload
    current = api_client.get(
        "/portfolio/fit-assessments/current",
        params={
            "account_id": str(request.account_id),
            "listing_id": 600000,
            "action": "open",
            "purpose_binding_key": body["purpose_binding_key"],
            "calculation_basis_key": body["calculation_basis_key"],
        },
        headers=KEY,
    )
    assert current.status_code == 200, current.text
    assert current.json()["payload"]["id"] == body["id"]
    assert current.json()["payload"]["frozen_inputs"]["mode"] == "live"
    assert current.json()["payload"]["financial_status"] == "unknown"

    # Rejected bindings must not create receipts or runs.
    replay_body = replay_request.model_dump(mode="json")
    broken_bindings = (
        {**replay_body["historical_replay"], "manifest_hash": "0" * 64},
        {
            **replay_body["historical_replay"],
            "input_as_of": (datetime.fromisoformat(input_as_of) + timedelta(seconds=1)).isoformat(),
        },
        {
            **replay_body["historical_replay"],
            "run_ref": {"kind": "pipeline_run", "id": 999999, "revision": 1},
        },
    )
    for broken in broken_bindings:
        body = {**replay_body, "operation_id": str(uuid4()), "historical_replay": broken}
        response = api_client.post("/portfolio/fit-assessments", json=body, headers=KEY)
        assert response.status_code == 422, response.text

    async with session_scope() as session:
        authorized = await session.get(PipelineRun, int(original["run_ref"]["id"]))
        assert authorized is not None
        altered = dict(authorized.request_json)
        altered["owner_id"] = "another_owner"
        authorized.request_json = altered
    owner_mismatch = {
        **replay_body,
        "operation_id": str(uuid4()),
        "historical_replay": replay_body["historical_replay"],
    }
    response = api_client.post("/portfolio/fit-assessments", json=owner_mismatch, headers=KEY)
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_pc12_fit_capacity_rejects_501_holdings_and_dto_101_items(
    api_client: Any, enabled_flags: None
) -> None:
    holding_ids = tuple(range(610001, 610502))
    inputs = make_inputs(
        target_listing=610001,
        extra_listings=holding_ids[1:],
        cash="1000000",
        holdings=tuple((listing_id, "1", "100") for listing_id in holding_ids),
        rules=(),
    )
    async with session_scope() as session:
        await persist_inputs(session, inputs)
    request = AccountFitRequest(
        operation_id=uuid4(),
        account_id=inputs.account_id,
        snapshot_ref=inputs.snapshot.reference,
        mandate_ref=ObjectRef(kind="mandate", id=inputs.mandate.id, revision=1),
        valuation_binding=inputs.valuation_binding,
        purpose_binding=inputs.purpose_binding,
        items=(FitRequestItem(listing_ref=inputs.snapshot.holdings[0].listing_ref, action="hold"),),
    )
    oversized = api_client.post(
        "/portfolio/fit-assessments", json=request.model_dump(mode="json"), headers=KEY
    )
    assert oversized.status_code == 422, oversized.text
    assert oversized.json()["detail"]["code"] == "account_capacity_exceeded"

    too_many_items = tuple(
        FitRequestItem(
            listing_ref=ObjectRef(kind="listing", id=620000 + index, revision=1),
            action="open",
            trade_assumptions=assumptions(),
        )
        for index in range(101)
    )
    with pytest.raises(ValidationError):
        AccountFitRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "operation_id": str(uuid4()),
                "items": [item.model_dump(mode="json") for item in too_many_items],
            }
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("candidate_db_fixture")
async def test_pc12_2001_source_members_are_partial_at_the_real_cap() -> None:
    as_of = datetime.now(UTC)
    screening_date = as_of.date()
    total = 2001
    async with session_scope() as session:
        run = await PipelineRunRepository(session).create(
            kind="screen_run",
            triggered_by="synthetic_pc12_test",
            status="success",
            stage="done",
            request_json={"as_of": screening_date.isoformat()},
        )
        run.finished_at = as_of - timedelta(minutes=1)
        run.metadata_json = {
            "as_of": screening_date.isoformat(),
            "templates": {
                "pc12_cap_template@1": {
                    "counts": {
                        "hit": total,
                        "truncated": 0,
                        "miss": 0,
                        "skipped": 0,
                        "excluded": 0,
                    }
                }
            },
            "result_rows": total,
            "load_errors": {},
        }
        await session.flush()
        await session.execute(
            sqlite_insert(ScreenResultRecord),
            [
                {
                    "screen_run_id": run.id,
                    "as_of": screening_date,
                    "template_id": "pc12_cap_template",
                    "template_version": 1,
                    "listing_id": 700000 + index,
                    "ticker": f"{600000 + index:06d}.SH",
                    "outcome": "hit",
                    "hit": True,
                    "rank": index + 1,
                    "strength": 0.5,
                    "reasons": [{"factor": "synthetic", "actual": index}],
                    "context": {"fixture": "synthetic_pc12"},
                }
                for index in range(total)
            ],
        )
        source_runs, source_issues = await resolve_source_runs(
            session,
            as_of=as_of,
            policy=RankingSourcePolicy(template_ids=("pc12_cap_template",)),
        )
        manifest = await freeze_candidate_sources(
            session,
            as_of=as_of,
            source_runs=source_runs,
            source_issues=source_issues,
            candidate_policy=CandidatePolicy(expiry_days=14, dismiss_cooldown_days=30),
        )

    assert manifest.coverage == "partial"
    assert manifest.total_unique_count == 2001
    assert manifest.processed_count == 2000
    assert manifest.unprocessed_count == 1
    assert len(manifest.members) == 2000
    cap_exclusion = next(item for item in manifest.exclusions if item.kind == "source_cap")
    assert cap_exclusion.unprocessed_range_start == 2000
    assert cap_exclusion.unprocessed_range_end == 2000

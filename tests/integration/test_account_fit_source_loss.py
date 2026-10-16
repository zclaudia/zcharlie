"""Exact account-fit and ranking artifacts survive loss of current sources."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from investment_agent.candidates.ranking_contracts import (
    BatchTradeAssumptions,
    RankingRequest,
    RankingSourcePolicy,
)
from investment_agent.candidates.ranking_jobs import CandidateRankingHandler
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.suitability_contracts import AccountFitRequest, FitRequestItem
from investment_agent.portfolio.suitability_jobs import AccountFitHandler
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import make_inputs
from tests.support.account_fit_flow import persist_inputs

KEY = {"X-API-Key": "secret"}
LISTING_ID = 600000


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT_FIT_ENABLED", "true")
    monkeypatch.setenv("CANDIDATE_RANKING_ENABLED", "true")


def _settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_key="secret",
        llm_tier3_api_key="ci-inert-tier3",
        data_dir=root,
        portfolio_ledger_enabled=True,
        account_fit_enabled=True,
        candidate_ranking_enabled=True,
    )


async def _prepare_requests(root: Path) -> tuple[AccountFitRequest, RankingRequest]:
    inputs = make_inputs(
        target_listing=LISTING_ID,
        holdings=((LISTING_ID, "100", "1500"),),
        rules=(),
    )
    async with session_scope() as session:
        await persist_inputs(session, inputs)
        run = await PipelineRunRepository(session).create(
            kind="screen_run",
            triggered_by="source-loss-test",
            status="success",
            stage="done",
            request_json={"as_of": "2026-10-15"},
        )
        run.finished_at = datetime.now(UTC) - timedelta(seconds=1)
        run.metadata_json = {
            "as_of": "2026-10-15",
            "templates": {
                "rank_a@1": {
                    "counts": {
                        "hit": 1,
                        "truncated": 0,
                        "miss": 0,
                        "skipped": 0,
                        "excluded": 0,
                    }
                }
            },
            "result_rows": 1,
            "load_errors": {},
        }
        listing = await session.get(Listing, LISTING_ID)
        assert listing is not None
        session.add(
            ScreenResultRecord(
                screen_run_id=run.id,
                as_of=date(2026, 10, 15),
                template_id="rank_a",
                template_version=1,
                listing_id=LISTING_ID,
                ticker=listing.ticker,
                outcome="hit",
                hit=True,
                rank=1,
                strength=0.5,
                reasons=[{"factor": "synthetic"}],
                context={"fixture": "source-loss"},
            )
        )

    common = {
        "account_id": inputs.account_id,
        "snapshot_ref": inputs.snapshot.reference,
        "mandate_ref": ObjectRef(kind="mandate", id=inputs.mandate.id, revision=1),
        "valuation_binding": inputs.valuation_binding,
        "purpose_binding": inputs.purpose_binding,
    }
    fit = AccountFitRequest(
        operation_id=uuid4(),
        **common,
        items=(
            FitRequestItem(
                listing_ref=ObjectRef(kind="listing", id=LISTING_ID, revision=1),
                action="add",
                trade_assumptions=assumptions(),
            ),
        ),
    )
    ranking = RankingRequest(
        operation_id=uuid4(),
        **common,
        source_policy=RankingSourcePolicy(template_ids=("rank_a",)),
        trade_assumptions=BatchTradeAssumptions(
            fixed_fee=Decimal("10"),
            fee_rate=Decimal("0"),
            assumption_source="synthetic_user_confirmation",
            unknown_fields=(),
            price_policy="valuation_price_as_scenario",
            confirmed=True,
        ),
    )
    return fit, ranking


async def _run(kind: str, run_id: int, root: Path) -> None:
    settings = _settings(root)
    handler = (
        AccountFitHandler(settings=settings)
        if kind == "fit"
        else CandidateRankingHandler(settings=settings)
    )
    worker = ResearchWorker(
        settings=settings,
        worker_id=f"source-loss-{kind}",
        handlers=[handler],
    )
    await worker.run_once(run_id=run_id)


@pytest.mark.parametrize("kind", ["fit", "ranking"])
@pytest.mark.parametrize("mutation", ["market", "price"])
async def test_exact_artifact_remains_auditable_when_current_source_disappears(
    api_client: Any,
    enabled: None,
    tmp_path: Path,
    kind: str,
    mutation: str,
) -> None:
    fit_request, ranking_request = await _prepare_requests(tmp_path)
    if kind == "fit":
        request = fit_request
        path = "/portfolio/fit-assessments"
    else:
        request = ranking_request
        path = "/candidates/rankings"
    accepted = api_client.post(path, json=request.model_dump(mode="json"), headers=KEY)
    assert accepted.status_code == 202, accepted.text
    receipt = accepted.json()
    await _run(kind, int(receipt["run_ref"]["id"]), tmp_path)

    async with session_scope() as session:
        run = await session.get(PipelineRun, int(receipt["run_ref"]["id"]))
        assert run is not None and run.status == "success"
        if kind == "fit":
            artifact = await session.scalar(select(InvestmentAssessment))
            assert artifact is not None and artifact.kind == "account_fit"
        else:
            ranking_ref = run.metadata_json["ranking_ref"]["id"]
            artifact = await session.get(InvestmentAssessment, (ranking_ref, 1))
            assert artifact is not None and artifact.kind == "candidate_ranking"
        original_payload = artifact.payload

        if mutation == "market":
            listing = await session.get(Listing, LISTING_ID)
            assert listing is not None
            listing.market_code = "NASDAQ"
        else:
            price = await session.get(MarketSnapshot, LISTING_ID + 9000000)
            assert price is not None
            await session.delete(price)

    if kind == "fit":
        artifact_id = original_payload["id"]
        exact_url = f"/portfolio/fit-assessments/{artifact_id}"
        params = {"account_id": str(request.account_id)}
    else:
        artifact_id = original_payload["id"]
        exact_url = f"/candidates/rankings/{artifact_id}"
        params = {"account_id": str(request.account_id)}
    exact = api_client.get(exact_url, params=params, headers=KEY)
    assert exact.status_code == 200, exact.text
    result = exact.json()
    returned_payload = result["payload"] if kind == "fit" else result["snapshot"]
    assert returned_payload == original_payload
    assert result["current_limits"]["current_status"] == "stale"
    reasons = result["current_limits"]["reasons"]
    assert reasons
    if mutation == "market":
        assert "listing_sources_unavailable" in reasons

    fresh = request.model_copy(update={"operation_id": uuid4()})
    refreshed = api_client.post(path, json=fresh.model_dump(mode="json"), headers=KEY)
    if mutation == "market":
        assert refreshed.status_code == 422, refreshed.text
    else:
        assert refreshed.status_code == 202, refreshed.text
        async with session_scope() as session:
            admitted = await session.get(PipelineRun, refreshed.json()["run_ref"]["id"])
            assert admitted is not None
            frozen = admitted.request_json["frozen_inputs"]
            source = next(
                row for row in frozen["listing_sources"]
                if row["listing_ref"]["id"] == LISTING_ID
            )
            assert source["price"]["price"] is None
            assert source["price"]["unknown_reason"] == "valuation_price_missing"

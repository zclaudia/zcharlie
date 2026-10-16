"""Real HTTP, worker and immutable-page integration checks for candidate rankings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from investment_agent.api.routes.investment_assessments import service as assessment_service
from investment_agent.candidates.ranking_contracts import (
    BatchTradeAssumptions,
    RankingRequest,
    RankingSourcePolicy,
)
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.calculations import TrustedTradingRule
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.investment_assessment import (
    CandidateRankingEntry,
    InvestmentAssessment,
)
from investment_agent.storage.models.investor import InvestorMutation
from investment_agent.storage.models.portfolio import (
    PortfolioAccount,
    PortfolioSnapshot,
    PortfolioValuation,
)
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.support.account_fit_db import make_inputs, make_trusted_rule
from tests.support.account_fit_flow import persist_inputs

KEY = {"X-API-Key": "secret"}
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000101")


class _AvailableRules:
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime) -> TrustedTradingRule:
        return make_trusted_rule(listing_ref)


@pytest.fixture
def ranking_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "true")
    monkeypatch.setenv("ACCOUNT_FIT_ENABLED", "true")
    monkeypatch.setenv("CANDIDATE_RANKING_ENABLED", "true")


def _settings(path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_key="secret",
        llm_tier3_api_key="test",
        data_dir=path,
        portfolio_ledger_enabled=True,
        account_fit_enabled=True,
        candidate_ranking_enabled=True,
    )


async def _request(listing_ids: tuple[int, ...]) -> RankingRequest:
    # A known holding has a determinable "add" action. Other listings in this
    # partial snapshot intentionally keep holding presence unknown.
    inputs = make_inputs(
        extra_listings=tuple(listing_id for listing_id in listing_ids if listing_id != 600000),
        holdings=((600000, "100", "100"),),
        snapshot_coverage="partial",
        prices={listing_id: ("100" if listing_id == 600000 else "1500") for listing_id in listing_ids},
    )
    async with session_scope() as session:
        await persist_inputs(session, inputs)
        context = await SuitabilityInputService(session).context(ACCOUNT_ID)
    assert context.snapshot is not None and context.mandate_ref is not None
    return RankingRequest(
        operation_id=uuid4(),
        account_id=ACCOUNT_ID,
        snapshot_ref=context.snapshot.reference,
        mandate_ref=context.mandate_ref,
        valuation_binding=context.valuation_binding,
        purpose_binding=context.purpose_binding,
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


async def _seed_screen_run(
    listing_ids: Iterable[int],
    *,
    truncated_ids: frozenset[int] = frozenset(),
    miss_ids: frozenset[int] = frozenset(),
) -> int:
    async with session_scope() as session:
        repo = PipelineRunRepository(session)
        run = await repo.create(
            kind="screen_run",
            triggered_by="candidate-ranking-integration",
            status="success",
            stage="done",
            request_json={"as_of": "2026-10-15"},
        )
        counts = {"hit": 0, "truncated": 0, "miss": 0, "skipped": 0, "excluded": 0}
        row_count = 0
        for rank, listing_id in enumerate(listing_ids, start=1):
            listing = await session.get(Listing, listing_id)
            assert listing is not None, f"missing fixture listing {listing_id}"
            outcome = "miss" if listing_id in miss_ids else "truncated" if listing_id in truncated_ids else "hit"
            counts[outcome] += 1
            row_count += 1
            session.add(
                ScreenResultRecord(
                    screen_run_id=run.id,
                    as_of=date(2026, 10, 15),
                    template_id="rank_a",
                    template_version=1,
                    listing_id=listing_id,
                    ticker=listing.ticker,
                    outcome=outcome,
                    hit=outcome == "hit",
                    rank=rank,
                    strength=0.75,
                    reasons=[{"factor": "synthetic", "rank": rank}],
                    context={"fixture": "candidate-ranking-api"},
                )
            )
        run.finished_at = datetime.now(UTC) - timedelta(seconds=1)
        run.metadata_json = {
            "as_of": "2026-10-15",
            "templates": {"rank_a@1": {"counts": counts}},
            "result_rows": row_count,
            "load_errors": {},
        }
        await session.flush()
        return run.id


async def _run_worker(run_id: int, tmp_path: Path) -> None:
    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="candidate-ranking-api-test",
        kinds=("candidate_rank",),
    )
    await worker.run_once(run_id=run_id)


def _post_ranking(api_client: Any, request: RankingRequest) -> dict[str, Any]:
    response = api_client.post(
        "/candidates/rankings", headers=KEY, json=request.model_dump(mode="json")
    )
    assert response.status_code == 202, response.text
    return cast(dict[str, Any], response.json())


async def _table_counts() -> dict[str, int]:
    async with session_scope() as session:
        models = {
            "accounts": PortfolioAccount,
            "snapshots": PortfolioSnapshot,
            "valuations": PortfolioValuation,
            "candidates": CandidateRecord,
            "watchlist": WatchlistItem,
            "screen_results": ScreenResultRecord,
            "assessments": InvestmentAssessment,
            "entries": CandidateRankingEntry,
            "runs": PipelineRun,
            "mutations": InvestorMutation,
        }
        result: dict[str, int] = {}
        for name, model in models.items():
            result[name] = int(await session.scalar(select(func.count()).select_from(model)) or 0)
        return result


@pytest.mark.asyncio
async def test_http_worker_paginates_frozen_universe_and_reads_stale_snapshot_read_only(
    api_client: Any, ranking_enabled: None, tmp_path: Path
) -> None:
    listing_ids = tuple(range(600000, 600105))  # 105 members: more than one API page.
    request = await _request(listing_ids)
    truncated_id = listing_ids[1]
    miss_id = listing_ids[-1]
    await _seed_screen_run(
        listing_ids,
        truncated_ids=frozenset({truncated_id}),
        miss_ids=frozenset({miss_id}),
    )

    accepted = _post_ranking(api_client, request)
    run_id = int(accepted["run_ref"]["id"])

    # A newer source set after acceptance must not change what the worker ranks.
    extra_id = 600105
    # Extend the persisted account fixture with one new source listing. The
    # portfolio snapshot remains exact and deliberately excludes this listing.
    async with session_scope() as session:
        company = Company(canonical_name="Synthetic later listing")
        session.add(company)
        await session.flush()
        session.add(Listing(id=extra_id, market_code="SSE", company_id=company.id, ticker=str(extra_id)))
    await _seed_screen_run((*listing_ids, extra_id), truncated_ids=frozenset({truncated_id}))
    await _run_worker(run_id, tmp_path)

    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None and run.status == "success"
        assert run.metadata_json["failed_items"] == []
        assert len(run.request_json["expanded_items"]) + len(
            run.request_json["unknown_action_listing_ids"]
        ) == len(listing_ids) - 1
        frozen_ids = {
            int(member["listing_ref"]["id"])
            for member in run.request_json["source_manifest"]["members"]
        }
        assert frozen_ids == set(listing_ids) - {miss_id}
        assert extra_id not in frozen_ids
        # The complete partial-holdings ranking is non-failed; the one known
        # holding gets a frozen action item and unknown presence stays unknown.
        assert len(run.request_json["expanded_items"]) == 1
        assert len(run.request_json["unknown_action_listing_ids"]) == len(frozen_ids) - 1
        expanded = run.request_json["expanded_items"]
        assert all(
            item["trade_assumptions"]["assumption_source"]
            == "synthetic_user_confirmation"
            for item in expanded
        )
        assert all(item["trade_assumptions"]["fixed_fee"] == "10" for item in expanded)
        ranking_ref = ObjectRef.model_validate(run.metadata_json["ranking_ref"])
        assessment = await InvestmentAssessmentRepository(session).read_ranking(
            "local_owner", ranking_ref
        )
        assert assessment is not None
        original_snapshot = assessment.snapshot.model_dump(mode="json")
        assert assessment.snapshot.entry_count == len(frozen_ids)
        assert assessment.snapshot.current_status == "stale"

    # Make current exclusion evidence too large after the immutable ranking has
    # been saved. GET must still serve that ranking and report a stale reason.
    async with session_scope() as session:
        session.add(
            CandidateRecord(
                listing_id=600000,
                ticker="600000",
                status="open",
                opened_at=datetime.now(UTC) - timedelta(days=1),
                expires_at=datetime.now(UTC) + timedelta(days=10),
                sources=[{"kind": "manual", "large": "x" * (4 * 1024 * 1024)}],
            )
        )
        session.add(
            WatchlistItem(
                listing_id=600000,
                added_at=datetime.now(UTC) - timedelta(days=1),
                provider="test",
                enabled=True,
                last_refresh_status="never",
            )
        )

    response = api_client.get(
        f"/candidates/rankings/{ranking_ref.id}",
        headers=KEY,
        params={"account_id": str(ACCOUNT_ID), "limit": 100},
    )
    assert response.status_code == 200, response.text
    first = response.json()
    assert first["snapshot"]["entry_count"] == len(frozen_ids)
    assert first["current_limits"]["current_status"] == "stale"
    assert "source_capacity_exceeded" in first["current_limits"]["reasons"]
    assert first["next_cursor"]
    assert len(first["entries"]) == 100
    next_page = api_client.get(
        f"/candidates/rankings/{ranking_ref.id}",
        headers=KEY,
        params={"account_id": str(ACCOUNT_ID), "limit": 100, "cursor": first["next_cursor"]},
    )
    assert next_page.status_code == 200, next_page.text
    second = next_page.json()
    all_entries = first["entries"] + second["entries"]
    assert len(all_entries) == len(frozen_ids)
    assert len({entry["listing_ref"]["id"] for entry in all_entries}) == len(frozen_ids)
    assert all(entry["ranking_bucket"] == "incomplete" for entry in all_entries)
    assert all(entry["financial_status"] == "unknown" for entry in all_entries)
    assert all(entry["execution_error"] is None for entry in all_entries)
    identified = next(entry for entry in all_entries if entry["listing_ref"]["id"] == 600000)
    assert identified["listing_ticker"] == "600000"
    assert identified["listing_name"] == "Synthetic 600000"
    assert {source["outcome"] for member in first["snapshot"]["source_manifest"]["members"] for source in member["sources"] if source["source_kind"].startswith("screen_")} == {"hit", "truncated"}
    screen_sources = [
        source
        for member in first["snapshot"]["source_manifest"]["members"]
        for source in member["sources"]
        if source["source_kind"].startswith("screen_")
    ]
    for source in screen_sources:
        raw_row = source["row_snapshot"]
        assert {"ticker", "hit", "created_at", "reasons", "context"} <= set(raw_row)
        canonical_row = json.dumps(
            raw_row,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        assert hashlib.sha256(canonical_row).hexdigest() == source["source_snapshot_hash"]
    financial_page = api_client.get(
        f"/candidates/rankings/{ranking_ref.id}",
        headers=KEY,
        params={
            "account_id": str(ACCOUNT_ID),
            "limit": 100,
            "financial_status": "unknown",
        },
    )
    assert financial_page.status_code == 200, financial_page.text
    assert len(financial_page.json()["entries"]) == 100
    assert financial_page.json()["next_cursor"]

    invalid_filter = api_client.get(
        f"/candidates/rankings/{ranking_ref.id}",
        headers=KEY,
        params={
            "account_id": str(ACCOUNT_ID),
            "limit": 50,
            "financial_status": "blocked",
            "cursor": first["next_cursor"],
        },
    )
    assert invalid_filter.status_code == 422
    wrong_account = api_client.get(
        f"/candidates/rankings/{ranking_ref.id}",
        headers=KEY,
        params={"account_id": str(uuid4())},
    )
    assert wrong_account.status_code == 404
    wrong_revision = api_client.get(
        f"/candidates/rankings/{ranking_ref.id}",
        headers=KEY,
        params={"account_id": str(ACCOUNT_ID), "revision": 2},
    )
    assert wrong_revision.status_code == 404

    before = await _table_counts()
    for _ in range(2):
        reread = api_client.get(
            f"/candidates/rankings/{ranking_ref.id}",
            headers=KEY,
            params={"account_id": str(ACCOUNT_ID), "limit": 20},
        )
        assert reread.status_code == 200, reread.text
        assert reread.json()["snapshot"] == first["snapshot"]
        assert reread.json()["current_limits"]["current_status"] == "stale"
    assert await _table_counts() == before
    async with session_scope() as session:
        persisted = await InvestmentAssessmentRepository(session).read_ranking(
            "local_owner", ranking_ref
        )
        assert persisted is not None
        assert persisted.snapshot.model_dump(mode="json") == original_snapshot

    replay = _post_ranking(api_client, request)
    assert replay["run_ref"] == accepted["run_ref"]
    assert replay["replayed"] is True


@pytest.mark.asyncio
async def test_http_empty_complete_batch_is_a_valid_zero_entry_ranking(
    api_client: Any, ranking_enabled: None, tmp_path: Path
) -> None:
    listing_ids = (600000,)
    request = await _request(listing_ids)
    await _seed_screen_run(())
    accepted = _post_ranking(api_client, request)
    await _run_worker(int(accepted["run_ref"]["id"]), tmp_path)

    async with session_scope() as session:
        run = await session.get(PipelineRun, int(accepted["run_ref"]["id"]))
        assert run is not None and run.status == "success"
        ranking_ref = ObjectRef.model_validate(run.metadata_json["ranking_ref"])
    response = api_client.get(
        f"/candidates/rankings/{ranking_ref.id}",
        headers=KEY,
        params={"account_id": str(ACCOUNT_ID)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "available"
    assert body["snapshot"]["entry_count"] == 0
    assert body["entries"] == [] and body["count"] == 0
    assert body["next_cursor"] is None
    assert body["current_limits"]["current_status"] == "valid"


@pytest.mark.asyncio
async def test_http_ranking_current_limits_reuse_admission_rule_resolver(
    api_client: Any, ranking_enabled: None, tmp_path: Path
) -> None:
    async def service_with_trusted_rule() -> AsyncIterator[SuitabilityInputService]:
        async with session_scope() as session:
            yield SuitabilityInputService(session, rules=_AvailableRules())

    api_client.app.dependency_overrides[assessment_service] = service_with_trusted_rule
    try:
        request = await _request((600000,))
        await _seed_screen_run((600000,))
        accepted = _post_ranking(api_client, request)
        await _run_worker(int(accepted["run_ref"]["id"]), tmp_path)

        async with session_scope() as session:
            run = await session.get(PipelineRun, int(accepted["run_ref"]["id"]))
            assert run is not None and run.status == "success"
            ranking_ref = ObjectRef.model_validate(run.metadata_json["ranking_ref"])

        params = {"account_id": str(ACCOUNT_ID)}
        response = api_client.get(
            f"/candidates/rankings/{ranking_ref.id}", headers=KEY, params=params
        )
        assert response.status_code == 200, response.text
        assert response.json()["current_limits"]["current_status"] == "valid"

        exact = api_client.get(
            f"/candidates/rankings/{ranking_ref.id}/entries/600000",
            headers=KEY,
            params=params,
        )
        assert exact.status_code == 200, exact.text
        assert exact.json()["current_limits"]["current_status"] == "valid"

        # A newer complete source run changes current source coverage while
        # leaving the immutable ranking available and unchanged.
        await _seed_screen_run((600000,))
        changed = api_client.get(
            f"/candidates/rankings/{ranking_ref.id}", headers=KEY, params=params
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["current_limits"]["current_status"] == "stale"
        assert "candidate_sources_changed" in changed.json()["current_limits"]["reasons"]
    finally:
        api_client.app.dependency_overrides.pop(assessment_service, None)


@pytest.mark.asyncio
async def test_http_rejects_unbounded_fee_and_excessive_source_exclusion(
    api_client: Any, ranking_enabled: None, tmp_path: Path
) -> None:
    request = await _request((600000,))
    body = request.model_dump(mode="json")
    body["trade_assumptions"]["fixed_fee"] = "1e999999"
    huge_fee = api_client.post("/candidates/rankings", headers=KEY, json=body)
    assert huge_fee.status_code == 422

    body["trade_assumptions"]["fixed_fee"] = "0.0000000000000000001"
    over_precision_fee = api_client.post("/candidates/rankings", headers=KEY, json=body)
    assert over_precision_fee.status_code == 422

    async with session_scope() as session:
        session.add(
            CandidateRecord(
                listing_id=600000,
                ticker="600000",
                status="open",
                opened_at=datetime.now(UTC) - timedelta(days=1),
                expires_at=datetime.now(UTC) + timedelta(days=10),
                sources=[{"kind": "manual", "large": "x" * (4 * 1024 * 1024)}],
            )
        )
        session.add(
            WatchlistItem(
                listing_id=600000,
                added_at=datetime.now(UTC) - timedelta(days=1),
                provider="test",
                enabled=True,
                last_refresh_status="never",
            )
        )
    await _seed_screen_run((600000,))
    excessive_evidence = api_client.post(
        "/candidates/rankings",
        headers=KEY,
        json=request.model_dump(mode="json"),
    )
    assert excessive_evidence.status_code == 422
    assert excessive_evidence.json()["detail"]["code"] == "source_capacity_exceeded"
    async with session_scope() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(PipelineRun).where(PipelineRun.kind == "candidate_rank")
            )
            == 0
        )

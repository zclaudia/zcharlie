"""Snapshot-mode research runs end to end on the database (P1-B5)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from investment_agent.config import Settings
from investment_agent.research import ifind_chain
from investment_agent.research.ifind_chain import (
    ResearchExplanation,
    ResearchPartialError,
    run_ifind_research_chain,
)
from investment_agent.research.snapshot_source import missing_key_fields
from investment_agent.research.watchlist_refresh import (
    enqueue_watchlist_refresh,
    research_source_mode,
)
from investment_agent.research.worker import ResearchJobPayload, _notify_meaningful_change
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun, ResearchSnapshot
from investment_agent.storage.models.verification import IfindCallLedger, VerificationRequest
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_change import ResearchChangeRepository
from investment_agent.storage.repositories.research_snapshot import ResearchSnapshotRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from tests.support.factor_view import AS_OF, TICKER, _seed

EXPLANATION = ResearchExplanation(
    summary="fixture",
    classification_rationale=[],
    positive_evidence=[],
    negative_evidence=[],
    risk_factors=[],
    data_quality_notes=[],
    open_questions=[],
    workflow_conclusion="needs_review",
)


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        data_dir=tmp_path,
    )


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


@pytest.fixture
def fake_explanation(monkeypatch):
    async def explain(**_kwargs: object) -> ResearchExplanation:
        return EXPLANATION

    monkeypatch.setattr(ifind_chain, "_explain_score", explain)


async def _run(tmp_path, ticker: str = TICKER, industry: str | None = None):
    return await run_ifind_research_chain(
        settings=_settings(tmp_path),
        proxy_url="http://must-not-be-called.invalid",
        proxy_api_key="",
        ticker=ticker,
        industry=industry,
        report_year=2025,
        as_of=AS_OF,
        output_root=tmp_path / "research-runs",
        source_mode="snapshot",
    )


async def test_snapshot_run_scores_from_the_snapshot_store(db_session, tmp_path, fake_explanation):
    del db_session
    async with session_scope() as session:
        await _seed(session)

    result = await _run(tmp_path)

    assert result.score["source_mode"] == "snapshot"
    assert result.score["industry"] == "酒、饮料和精制茶制造业"  # CSRC name, no user text
    assert (result.artifact_dir / "raw_snapshot.json").is_file()
    pool = json.loads((result.artifact_dir / "pool.json").read_text(encoding="utf-8"))
    assert pool["metadata"]["source_mode"] == "snapshot"
    assert pool["items"][0]["industry_group"] == "consumer"
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(result.run_id)
        assert run is not None and run.status == "success"
        assert run.metadata_json["source_mode"] == "snapshot"
        assert run.metadata_json["provider"] == "snapshot_view"
        assert run.metadata_json["source_changed"] is False
        snapshot = await ResearchSnapshotRepository(session).by_run(result.run_id)
        assert snapshot is not None and snapshot.report_year == 2025
        ledger = (await session.execute(select(IfindCallLedger))).scalars().all()
        assert ledger == []  # no iFinD call
        # key fields no free source has (no Eastmoney rows seeded) -> one A13
        # ``missing`` request, executed later within the iFinD budget
        requested = run.metadata_json["verification_requested_fields"]
        assert {"net_profit_parent", "revenue_total", "capital_expenditure"} <= set(requested)
        (request,) = (await session.execute(select(VerificationRequest))).scalars().all()
        assert (request.trigger, request.fields) == ("missing", requested)
        assert request.report_period == date(2025, 12, 31)


def test_missing_key_fields_skip_fields_not_applicable_to_the_profile():
    score = {
        "missing_fields": ["free_cash_flow", "gross_margin", "debt_ratio"],
        "company_profile": {"non_applicable_fields": ["gross_margin", "free_cash_flow"]},
    }
    assert missing_key_fields(score) == ["debt_ratio"]
    assert missing_key_fields({"missing_fields": ["free_cash_flow"]}) == [
        "capital_expenditure",
        "free_cash_flow",
    ]


async def test_first_snapshot_run_after_ifind_is_marked_source_changed(
    db_session, tmp_path, fake_explanation
):
    del db_session
    async with session_scope() as session:
        await _seed(session)
        previous_run = await PipelineRunRepository(session).create(kind="ifind_research")
        await ResearchSnapshotRepository(session).create(
            pipeline_run_id=previous_run.id,
            symbol="600519",
            market="SH",
            company_name="贵州茅台",
            industry="白酒",
            as_of=date(2026, 5, 5),
            report_year=2025,
            canonical_schema_version="ifind-canonical.v1",
            scoring_version="v1.3",
            score_schema_version="v2.1",
            score_json={"business_score": 40.0, "key_metrics": {"pe": 30.0}},  # iFinD era
            explanation_json=None,
            artifact_manifest={},
        )

    result = await _run(tmp_path, industry="白酒")

    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(result.run_id)
        assert run is not None
        assert run.metadata_json["source_changed"] is True
        assert run.metadata_json["previous_source_mode"] == "ifind"
        change_set = await ResearchChangeRepository(session).by_run(result.run_id)
        assert change_set is not None
        assert change_set.diff_json["source_changed"] is True
        assert change_set.has_meaningful_changes is False
    path = await _notify_meaningful_change(settings=_settings(tmp_path), run_id=result.run_id)
    assert path is not None
    body = _read(path)
    assert "数据源变更：iFinD → 免费源快照" in body

    second = await _run(tmp_path, industry="白酒")
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(second.run_id)
        assert run is not None and run.metadata_json["source_changed"] is False


async def test_missing_free_source_data_ends_partial_with_a_verification_request(
    db_session, tmp_path, fake_explanation
):
    del db_session
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing(
            "600000.SH", company_name="浦发银行"
        )
        listing_id = listing.id

    with pytest.raises(ResearchPartialError) as exc_info:
        await _run(tmp_path, ticker="600000.SH")

    assert "snapshot_data_missing" in exc_info.value.reason
    assert "trigger=missing" in exc_info.value.reason
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(exc_info.value.run_id)
        assert run is not None and run.status == "partial"
        assert run.metadata_json["partial_reason"] == "snapshot_data_missing"
        assert run.metadata_json["verification_requested"] is True
        (request,) = (await session.execute(select(VerificationRequest))).scalars().all()
        assert (request.listing_id, request.trigger, request.source_run_id) == (
            listing_id,
            "missing",
            exc_info.value.run_id,
        )
        assert request.report_period == date(2025, 12, 31)
        snapshots = (await session.execute(select(ResearchSnapshot))).scalars().all()
        assert snapshots == []
        ledger = (await session.execute(select(IfindCallLedger))).scalars().all()
        assert ledger == []


async def test_unknown_listing_ends_partial_without_a_request(db_session, tmp_path):
    del db_session
    with pytest.raises(ResearchPartialError) as exc_info:
        await _run(tmp_path, ticker="688999.SH")
    assert "listing unknown" in exc_info.value.reason
    async with session_scope() as session:
        assert (await session.execute(select(VerificationRequest))).scalars().all() == []


@pytest.mark.parametrize(
    ("provider", "mode"),
    [
        ("auto", "snapshot"),
        ("snapshot", "snapshot"),
        ("ifind", "ifind"),
        ("ifind-proxy", "ifind"),
        (None, "snapshot"),
        ("tushare", None),
    ],
)
def test_provider_routing(provider, mode):
    assert research_source_mode(provider) == mode


def test_legacy_queued_payload_without_provider_is_an_ifind_run():
    payload = ResearchJobPayload.model_validate(
        {"ticker": "600406.SH", "industry": "电力设备", "report_year": 2025, "as_of": "2026-10-05"}
    )
    assert payload.provider == "ifind"
    snapshot = ResearchJobPayload.model_validate(
        {"ticker": "600406.SH", "as_of": "2026-10-05", "provider": "snapshot"}
    )
    assert snapshot.industry is None and snapshot.report_year is None
    with pytest.raises(ValueError, match="industry and report_year"):
        ResearchJobPayload.model_validate(
            {"ticker": "600406.SH", "as_of": "2026-10-05", "provider": "ifind"}
        )


async def test_watchlist_refresh_routes_auto_to_snapshot_and_keeps_explicit_ifind(db_session):
    del db_session
    async with session_scope() as session:
        listings = ListingRepository(session)
        repository = WatchlistRepository(session)
        auto = await listings.ensure_a_share_listing("600519.SH", company_name="贵州茅台")
        ifind = await listings.ensure_a_share_listing("600406.SH", company_name="国电南瑞")
        bare = await listings.ensure_a_share_listing("601398.SH", company_name="工商银行")
        await repository.add(auto.id, provider="auto")  # no industry / report year needed
        await repository.add(ifind.id, provider="ifind", industry="电力设备", report_year=2025)
        await repository.add(bare.id, provider="ifind")  # iFinD needs both
        ids = {"auto": auto.id, "ifind": ifind.id, "bare": bare.id}

    async with session_scope() as session:
        outcomes = {
            outcome.listing_id: outcome
            for outcome in await enqueue_watchlist_refresh(
                session, as_of=date(2026, 9, 30), triggered_by="test", ifind_available=False
            )
        }
    assert outcomes[ids["auto"]].run_id is not None
    assert outcomes[ids["ifind"]].error == "IFIND_PROXY_API_KEY is not configured"
    assert outcomes[ids["bare"]].status == "failed"

    async with session_scope() as session:
        outcomes = {
            outcome.listing_id: outcome
            for outcome in await enqueue_watchlist_refresh(
                session, as_of=date(2026, 9, 30), triggered_by="test"
            )
        }
        runs = {
            run.listing_id: run
            for run in (await session.execute(select(PipelineRun))).scalars().all()
        }
    assert runs[ids["auto"]].request_json["provider"] == "snapshot"
    assert runs[ids["auto"]].request_json["industry"] is None
    assert runs[ids["ifind"]].request_json["provider"] == "ifind"
    assert outcomes[ids["bare"]].error == "industry is required"

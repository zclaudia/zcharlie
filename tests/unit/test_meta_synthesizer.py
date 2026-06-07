"""Tests for Meta-Agent synthesizer (consensus/divergent/overlooked)."""

from decimal import Decimal

import pytest
import pytest_asyncio

from investment_agent.meta.synthesizer import synthesize_discovery_report
from investment_agent.storage.models.analysis import (
    PipelineRun,
    StockScore,
    Strategy,
)
from investment_agent.storage.models.reference import Company, Listing, Market


@pytest_asyncio.fixture
async def seeded_run(db_session):
    """Seed Market + Company + 3 Listings + 3 Strategies + 1 PipelineRun."""
    from datetime import UTC, datetime

    market = Market(code="NASDAQ", name="NASDAQ", timezone="UTC", currency="USD")
    db_session.add(market)
    await db_session.flush()

    company = Company(canonical_name="Test Co")
    db_session.add(company)
    await db_session.flush()

    listings = [
        Listing(company_id=company.id, market_code="NASDAQ", ticker=f"T{i}")
        for i in range(5)
    ]
    db_session.add_all(listings)
    await db_session.flush()

    strategies = [
        Strategy(code=c, name=c, version="1.0", config_json={}, enabled=True)
        for c in ("value_v1", "growth_v1", "momentum_v1")
    ]
    db_session.add_all(strategies)
    await db_session.flush()

    run = PipelineRun(
        kind="discovery",
        triggered_by="test",
        started_at=datetime.now(UTC),
        status="running",
        metadata_json={},
    )
    db_session.add(run)
    await db_session.flush()

    return {"listings": listings, "strategies": strategies, "run": run}


async def _add_score(
    session, run_id: int, listing_id: int, strategy_code: str, score: int
) -> None:
    session.add(
        StockScore(
            pipeline_run_id=run_id,
            listing_id=listing_id,
            strategy_code=strategy_code,
            tier=2,
            score=score,
            confidence=Decimal("0.8"),
            key_metrics={},
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_consensus_pick_detected(db_session, seeded_run):
    run_id = seeded_run["run"].id
    lid = seeded_run["listings"][0].id
    # 3 strategies, all >=70, spread 10 (<20), avg 75
    await _add_score(db_session, run_id, lid, "value_v1", 70)
    await _add_score(db_session, run_id, lid, "growth_v1", 75)
    await _add_score(db_session, run_id, lid, "momentum_v1", 80)

    report = await synthesize_discovery_report(db_session, run_id)
    assert len(report.consensus_picks) == 1
    pick = report.consensus_picks[0]
    assert pick["listing_id"] == lid
    assert pick["avg_score"] == 75.0
    assert set(pick["supporting"]) == {"value_v1", "growth_v1", "momentum_v1"}
    assert report.divergent_picks == []
    assert report.overlooked_picks == []


@pytest.mark.asyncio
async def test_divergent_pick_detected(db_session, seeded_run):
    run_id = seeded_run["run"].id
    lid = seeded_run["listings"][1].id
    # spread 60 (>=40), supporter (>=70) AND opposer (<=40)
    await _add_score(db_session, run_id, lid, "value_v1", 90)
    await _add_score(db_session, run_id, lid, "growth_v1", 30)
    await _add_score(db_session, run_id, lid, "momentum_v1", 55)

    report = await synthesize_discovery_report(db_session, run_id)
    assert report.consensus_picks == []
    assert len(report.divergent_picks) == 1
    pick = report.divergent_picks[0]
    assert pick["listing_id"] == lid
    assert pick["spread"] == 60
    assert pick["bull"] == ["value_v1"]
    assert pick["bear"] == ["growth_v1"]


@pytest.mark.asyncio
async def test_overlooked_pick_detected(db_session, seeded_run):
    run_id = seeded_run["run"].id
    lid = seeded_run["listings"][2].id
    # exactly one strategy >=80, all others <50 but >40 (avoid divergent)
    await _add_score(db_session, run_id, lid, "value_v1", 85)
    await _add_score(db_session, run_id, lid, "growth_v1", 45)
    await _add_score(db_session, run_id, lid, "momentum_v1", 49)

    report = await synthesize_discovery_report(db_session, run_id)
    assert report.consensus_picks == []
    assert report.divergent_picks == []
    assert len(report.overlooked_picks) == 1
    pick = report.overlooked_picks[0]
    assert pick["listing_id"] == lid
    assert pick["champion"] == "value_v1"
    assert pick["champion_score"] == 85


@pytest.mark.asyncio
async def test_none_of_the_above_excluded(db_session, seeded_run):
    run_id = seeded_run["run"].id
    lid = seeded_run["listings"][3].id
    # all middling: 55/60/65 — no consensus (none>=70), no divergent (no >=70 + <=40), no overlooked
    await _add_score(db_session, run_id, lid, "value_v1", 55)
    await _add_score(db_session, run_id, lid, "growth_v1", 60)
    await _add_score(db_session, run_id, lid, "momentum_v1", 65)

    report = await synthesize_discovery_report(db_session, run_id)
    assert report.consensus_picks == []
    assert report.divergent_picks == []
    assert report.overlooked_picks == []


@pytest.mark.asyncio
async def test_synthesize_is_upsert(db_session, seeded_run):
    run_id = seeded_run["run"].id
    lid_a = seeded_run["listings"][0].id
    lid_b = seeded_run["listings"][1].id

    # first run: lid_a forms a consensus
    await _add_score(db_session, run_id, lid_a, "value_v1", 75)
    await _add_score(db_session, run_id, lid_a, "growth_v1", 80)

    report1 = await synthesize_discovery_report(db_session, run_id)
    assert len(report1.consensus_picks) == 1

    # add more scores so lid_b also becomes consensus, then re-run
    await _add_score(db_session, run_id, lid_b, "value_v1", 90)
    await _add_score(db_session, run_id, lid_b, "growth_v1", 85)

    report2 = await synthesize_discovery_report(db_session, run_id)
    assert report2.pipeline_run_id == report1.pipeline_run_id
    assert len(report2.consensus_picks) == 2
    # lid_b should be first (higher avg)
    assert report2.consensus_picks[0]["listing_id"] == lid_b
    assert report2.consensus_picks[1]["listing_id"] == lid_a

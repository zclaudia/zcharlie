"""Shared seeding helpers for the candidate / decision / promote tests."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest_asyncio

from investment_agent.rules.schema import RuleInstance
from investment_agent.rules.store import RuleStore
from investment_agent.rules.validation import parse_template
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, IndustryClassification
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.rules import RuleInstanceRecord, ScreenResultRecord
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

AS_OF = date(2026, 9, 27)
TEMPLATE_ID = "cand_test"

TEMPLATE: dict[str, Any] = {
    "id": TEMPLATE_ID,
    "name": "Candidate test",
    "applicable_groups": ["consumer", "banking"],
    "screen": {
        "all": [
            {"factor": "roe", "op": ">=", "value": "$roe_min"},
            {"factor": "gross_margin", "op": ">=", "value": 0.5},
        ]
    },
    "candidate_zones": {
        "buy_zone": {"all": [{"factor": "pe_percentile_5y", "op": "<=", "value": "$pe_pct_max"}]},
        "drop": {"any": [{"factor": "roe", "op": "<", "value": 0.05}]},
    },
    "holding_zones": {
        "add": {"all": [{"factor": "position_weight", "op": "<", "value": 0.1}]},
        "trim": {"any": [{"factor": "gain_vs_cost", "op": ">", "value": 0.5}]},
        "exit_review": {"any": [{"factor": "roe", "op": "<", "value": 0.05}]},
    },
    "params": {"roe_min": 0.1, "pe_pct_max": 0.3},
    "param_ranges": {
        "roe_min": {"min": 0.05, "max": 0.3},
        "pe_pct_max": {"min": 0.1, "max": 0.6},
    },
}

REASONS: list[dict[str, Any]] = [
    {
        "rule_path": "screen.all[0]",
        "factor": "roe",
        "op": ">=",
        "threshold": 0.1,
        "param": "roe_min",
        "actual": 0.31,
        "quality": "ok",
        "flags": [],
        "outcome": "met",
        "snapshot_ref": {"financial_snapshot_id": 11, "report_period": "2025-12-31"},
    },
    {
        "rule_path": "screen.all[1]",
        "factor": "gross_margin",
        "op": ">=",
        "threshold": 0.5,
        "param": None,
        "actual": 0.91,
        "quality": "ok",
        "flags": [],
        "outcome": "met",
        "snapshot_ref": {"financial_snapshot_id": 11, "report_period": "2025-12-31"},
    },
]


@pytest_asyncio.fixture
async def cand_db(tmp_path, monkeypatch):
    """A fresh file-backed SQLite (so rollbacks are observable from new sessions)."""

    url = f"sqlite+aiosqlite:///{tmp_path / 'cand.db'}"
    for key, value in {
        "API_KEY": "secret",
        "LLM_TIER3_API_KEY": "x",
        "DATA_DIR": str(tmp_path),
        "DATABASE_URL": url,
    }.items():
        monkeypatch.setenv(key, value)
    engine = init_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield url
    await engine.dispose()


def template_data(**overrides: Any) -> dict[str, Any]:
    data = copy.deepcopy(TEMPLATE)
    data.update(overrides)
    return data


async def seed_template(session, **overrides: Any) -> int:
    template = parse_template(template_data(**overrides))
    outcomes = await RuleStore(session).sync_templates([(None, template)])
    return outcomes[0].version


async def seed_listing(session, ticker: str = "600519.SH", name: str = "贵州茅台") -> Listing:
    return await ListingRepository(session).ensure_a_share_listing(ticker, company_name=name)


async def seed_screen_run(session, as_of: date = AS_OF) -> int:
    run = await PipelineRunRepository(session).create(
        kind="screen_run",
        universe_code=None,
        triggered_by="test",
        status="success",
        stage="done",
        request_json={"universe_code": "CSI300", "as_of": as_of.isoformat()},
    )
    return run.id


async def seed_result(
    session,
    *,
    run_id: int,
    listing: Listing,
    ticker: str = "600519.SH",
    template_id: str = TEMPLATE_ID,
    template_version: int = 1,
    outcome: str = "hit",
    rank: int | None = 1,
    strength: float | None = 0.5,
    as_of: date = AS_OF,
) -> ScreenResultRecord:
    row = ScreenResultRecord(
        screen_run_id=run_id,
        as_of=as_of,
        template_id=template_id,
        template_version=template_version,
        listing_id=listing.id,
        ticker=ticker,
        outcome=outcome,
        hit=outcome == "hit",
        rank=rank if outcome in {"hit", "truncated"} else None,
        strength=strength,
        reasons=copy.deepcopy(REASONS),
        context={"provider": "baostock", "as_of": as_of.isoformat(), "industry_group": "consumer"},
    )
    session.add(row)
    await session.flush()
    return row


async def seed_classification(
    session, listing_id: int, *, shenwan: bool = True, csrc: bool = True
) -> None:
    captured = datetime(2026, 9, 1, tzinfo=UTC)
    if shenwan:
        session.add(
            IndustryClassification(
                listing_id=listing_id,
                system="sw2021",
                code="340501",
                name="白酒Ⅲ",
                valid_from=date(2021, 7, 30),
                valid_to=None,
                source="akshare",
                captured_at=captured,
            )
        )
    if csrc:
        session.add(
            IndustryClassification(
                listing_id=listing_id,
                system="csrc",
                code="C15",
                name="酒、饮料和精制茶制造业",
                valid_from=date(2015, 1, 1),
                valid_to=None,
                source="baostock",
                captured_at=captured,
            )
        )
    await session.flush()


async def seed_financials(session, listing_id: int) -> None:
    captured = datetime(2026, 9, 1, tzinfo=UTC)
    for period, available, kind in (
        (date(2024, 12, 31), date(2025, 4, 3), "annual"),
        (date(2025, 12, 31), date(2026, 4, 17), "annual"),
        (date(2026, 6, 30), date(2026, 8, 15), "semi"),
        (date(2026, 12, 31), None, "annual"),  # not available: never counts
    ):
        session.add(
            FinancialSnapshot(
                listing_id=listing_id,
                report_period=period,
                period_kind=kind,
                available_from=available,
                availability="actual",
                fields={},
                provider="baostock",
                captured_at=captured,
                is_backfill=False,
                version=1,
            )
        )
    await session.flush()


async def seed_promotable(
    *,
    previous_active: bool = True,
    draft: bool = True,
    shenwan: bool = True,
) -> dict[str, int]:
    """Template, listing (industry + financials), an optional active rule,
    one screen hit turned into an open candidate and its draft rule."""

    from investment_agent.candidates.lifecycle import (
        CandidatePolicy,
        create_candidates_from_screen,
    )

    ids: dict[str, int] = {}
    async with session_scope() as session:
        await seed_template(session)
        listing = await seed_listing(session)
        ids["listing_id"] = listing.id
        await seed_classification(session, listing.id, shenwan=shenwan)
        await seed_financials(session, listing.id)
        if previous_active:
            old = await seed_draft(session, params={"pe_pct_max": 0.4})
            await RuleStore(session).approve(old.id, approved_by="user")
            ids["previous_rule_id"] = old.id
        run_id = await seed_screen_run(session)
        ids["run_id"] = run_id
        hit = await seed_result(session, run_id=run_id, listing=listing)
        ids["screen_result_id"] = hit.id
    async with session_scope() as session:
        outcome = await create_candidates_from_screen(
            session,
            run_id=run_id,
            policy=CandidatePolicy(),
            now=datetime.now(UTC) - timedelta(minutes=5),
        )
        ids["candidate_id"] = outcome.created[0]["candidate_id"]
    if draft:
        async with session_scope() as session:
            ids["draft_rule_id"] = (await seed_draft(session)).id
    return ids


async def seed_draft(
    session,
    ticker: str = "600519.SH",
    *,
    params: dict[str, float] | None = None,
    template_version: int = 1,
) -> RuleInstanceRecord:
    instance = RuleInstance(
        template_ref=f"{TEMPLATE_ID}@{template_version}",
        symbol=ticker,
        scope="candidate",
        params=params or {"pe_pct_max": 0.25},
        drafted_by="llm:test",
        draft_rationale="valuation in the lower quarter of its 5y range",
    )
    return await RuleStore(session).create_draft(instance)

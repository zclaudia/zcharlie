"""Service-level guards of the holding-rule slot, without the HTTP layer.

Direct tests for the two generation guards of ``rules/holding.py`` that the
HTTP route previously covered only end to end:

* the shared drafting generation claim — a newer draft request supersedes the
  previous token, and the superseded request is refused at store/preflight;
* the review supersede guards — approve/reject act only on the current open
  draft and on a caller who still sees the active version.
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select

from investment_agent.rules.drafting import RULE_DRAFT
from investment_agent.rules.generation import (
    SUPERSEDED_MESSAGE,
    current_generation,
    guard_store,
    is_current_token,
    preflight_reason,
)
from investment_agent.rules.holding import (
    HoldingRuleConflictError,
    HoldingRuleInputError,
    approve_holding_rule,
    create_holding_draft,
    reject_holding_rule,
)
from investment_agent.rules.schema import RuleInstance
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.rules import RuleInstanceRecord

TICKER = "600519.SH"
TEMPLATE = "quality_growth"
AS_OF = date(2026, 5, 6)


@pytest_asyncio.fixture
async def slot(tmp_path, monkeypatch):
    """One A-share listing on a fresh file SQLite with synced templates."""

    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "holding-service.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Fixtures", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="贵州茅台")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600519")
        session.add(listing)
        await session.flush()
        listing_id = listing.id
        await RuleStore(session).sync_templates(load_templates())
    yield listing_id
    await engine.dispose()


async def request_draft(listing_id: int) -> tuple[str, int]:
    async with session_scope() as session:
        return await create_holding_draft(
            session, listing_id, template_id=TEMPLATE, as_of=AS_OF
        )


async def store_draft(listing_id: int) -> int:
    """Store one draft version the way the worker would after drafting."""

    async with session_scope() as session:
        store = RuleStore(session)
        record = await store.latest_template_record(TEMPLATE)
        instance = RuleInstance(
            template_ref=f"{TEMPLATE}@{record.version}",
            symbol=TICKER,
            scope="holding",
            drafted_by="fixture",
        )
        draft = await store.create_draft(instance)
        return draft.id


async def test_newer_draft_request_supersedes_the_previous_generation(slot) -> None:
    first_request, first_run = await request_draft(slot)
    second_request, second_run = await request_draft(slot)
    assert (first_request, first_run) != (second_request, second_run)

    async with session_scope() as session:
        generation = await current_generation(session, listing_id=slot, scope="holding")
        assert generation.generation == 2
        assert generation.token == second_request
        assert generation.origin_kind == "holding" and generation.origin_id == slot
        assert not await is_current_token(
            session, listing_id=slot, scope="holding", token=first_request
        )
        assert await is_current_token(
            session, listing_id=slot, scope="holding", token=second_request
        )
        # The superseded request is refused before the model call and at store time.
        assert (
            await preflight_reason(session, symbol=TICKER, scope="holding", token=first_request)
            == SUPERSEDED_MESSAGE
        )
        assert (
            await guard_store(session, symbol=TICKER, scope="holding", token=first_request)
            == SUPERSEDED_MESSAGE
        )
        assert await guard_store(session, symbol=TICKER, scope="holding", token=second_request) is None

    # Each request enqueued its own real rule_draft run for this listing.
    async with session_scope() as session:
        runs = (
            await session.scalars(select(PipelineRun).where(PipelineRun.kind == RULE_DRAFT))
        ).all()
        assert [(run.id, run.status) for run in runs] == [
            (first_run, "queued"),
            (second_run, "queued"),
        ]


async def test_review_actions_only_act_on_the_current_open_draft(slot) -> None:
    first_draft = await store_draft(slot)
    async with session_scope() as session:
        approved, decision = await approve_holding_rule(session, slot, rule_instance_id=first_draft)
        assert approved.status == "active"
        assert decision.kind == "approve_rule" and decision.context["source"] == "holding"

    # The approved instance is no longer an open draft; approving it again is a conflict.
    with pytest.raises(HoldingRuleConflictError, match="out of date"):
        async with session_scope() as session:
            await approve_holding_rule(session, slot, rule_instance_id=first_draft)

    second_draft = await store_draft(slot)
    with pytest.raises(HoldingRuleConflictError, match="active rule changed"):
        async with session_scope() as session:
            # The caller believes no rule is active; a stale expectation is refused.
            await approve_holding_rule(
                session,
                slot,
                rule_instance_id=second_draft,
                expected_active_rule_instance_id=None,
            )
    async with session_scope() as session:
        active = await RuleStore(session).active_instance(slot, "holding")
        assert active.id == first_draft, "the conflicted approval must not activate anything"

    # Approving with the exact active version the caller saw succeeds and retires it.
    async with session_scope() as session:
        approved, _ = await approve_holding_rule(
            session,
            slot,
            rule_instance_id=second_draft,
            expected_active_rule_instance_id=first_draft,
        )
        assert approved.id == second_draft and approved.version == 2

    third_draft = await store_draft(slot)
    with pytest.raises(HoldingRuleInputError, match="non-empty reason"):
        async with session_scope() as session:
            await reject_holding_rule(session, slot, rule_instance_id=third_draft, reason="   ")
    async with session_scope() as session:
        rejected, decision = await reject_holding_rule(
            session, slot, rule_instance_id=third_draft, reason="参数与持仓计划不符"
        )
        assert rejected.id == third_draft and decision.kind == "reject_rule"
        active = await RuleStore(session).active_instance(slot, "holding")
        assert active.id == second_draft, "rejecting a draft never touches the active version"
    async with session_scope() as session:
        records = (
            await session.scalars(
                select(RuleInstanceRecord)
                .where(RuleInstanceRecord.listing_id == slot, RuleInstanceRecord.scope == "holding")
                .order_by(RuleInstanceRecord.version)
            )
        ).all()
    assert [(row.id, row.status) for row in records] == [
        (first_draft, "retired"),
        (second_draft, "active"),
        (third_draft, "retired"),
    ]

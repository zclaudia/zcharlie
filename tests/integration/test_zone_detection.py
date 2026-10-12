"""Daily zone detection (watch-holding-plan 6.2 WP8, funnel 5.4/6/7.3/8/9).

The kernel runs against stub factor values (fixed inputs, no snapshots) and -
for the holding scope - against a real portfolio ledger and a real immutable
valuation, so the position factors are exercised end to end. The job handler
runs on an isolated migrated SQLite database.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.factors.registry import FactorValue
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.contracts import (
    AppendEvent,
    Balance,
    CreateAccount,
    Holding,
)
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.valuation import ValuationService
from investment_agent.research.worker import ResearchWorker
from investment_agent.rules import zone_detection as zd
from investment_agent.rules.schema import RuleInstance
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.rules.zone_jobs import (
    ZoneDetectionHandler,
    ZoneDetectionPayload,
    enqueue_zone_detection,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.portfolio import (
    PortfolioAccount,
)
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.zones import ZoneEventRecord, ZoneIntervalStateRecord

TICKER = "600519.SH"
TEMPLATE = "quality_growth"
DAY = date(2026, 5, 12)  # Tuesday
T = datetime(2026, 5, 12, 8, 0, tzinfo=UTC)
D = Decimal


class _Factors:
    """A mutable stub of the point-in-time factor loader."""

    def __init__(self) -> None:
        self.values: dict[str, float | None] = {}
        self.revision = 0

    def set(self, **values: float | None) -> None:
        self.values.update(values)

    def bump_inputs(self) -> None:
        """A revised input with the same values: a new snapshot pair."""

        self.revision += 1

    async def __call__(self, session: Any, ticker: str, as_of: date, names: Any) -> dict[str, Any]:
        if ticker != TICKER:
            raise LookupError(f"no fixture factors for {ticker}")
        return {
            name: FactorValue(
                name,
                "fixture",
                self.values[name],
                "ok" if self.values[name] is not None else "missing",
                inputs_ref={
                    "provider": "fixture",
                    "as_of": as_of.isoformat(),
                    "capture": self.revision,
                },
            )
            for name in names
            if name in self.values
        }


@pytest.fixture
def factors(monkeypatch) -> _Factors:
    stub = _Factors()
    monkeypatch.setattr(zd, "candidate_factor_values", stub)
    return stub


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'zones.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Fixture", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="贵州茅台")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600519")
        session.add(listing)
        await session.flush()
        listing_id = listing.id
        session.add(
            MarketSnapshot(
                listing_id=listing.id,
                trade_date=DAY,
                open=D(10),
                high=D(10),
                low=D(10),
                close=D(10),
                volume=D(1),
                adjust="none",
                provider="fixture",
                captured_at=T,
                version=1,
            )
        )
        await RuleStore(session).sync_templates(load_templates())
    yield SimpleNamespace(listing_id=listing_id, tmp_path=tmp_path, settings=Settings(
        _env_file=None, api_key="test", llm_tier3_api_key="test", data_dir=tmp_path
    ))
    await engine.dispose()


async def _active_rule(
    *,
    scope: str,
    params: dict[str, float] | None = None,
    template_id: str = TEMPLATE,
    listing_id: int | None = None,
) -> int:
    async with session_scope() as session:
        store = RuleStore(session)
        template = await store.get_template(template_id)
        instance = RuleInstance(
            template_ref=template.ref,
            symbol=TICKER,
            scope=scope,
            params=dict(params or {}),
            drafted_by="user",
        )
        record = await store.create_draft(instance)
        await store.approve(record.id, approved_by="user")
        listing = await session.get(Listing, listing_id) if listing_id else None
        assert listing is None or record.listing_id == listing.id
        return record.id


async def _run(*, as_of: date = DAY, scope: str, settings: Settings | None = None):
    async with session_scope() as session:
        return await zd.run_zone_detection(session, as_of=as_of, scope=scope, settings=settings)


async def _events(*, scope: str | None = None) -> list[ZoneEventRecord]:
    async with session_scope() as session:
        stmt = select(ZoneEventRecord).order_by(ZoneEventRecord.id)
        if scope is not None:
            stmt = stmt.where(ZoneEventRecord.scope == scope)
        return list((await session.scalars(stmt)).all())


async def _state(*, zone: str, scope: str = "candidate") -> ZoneIntervalStateRecord | None:
    async with session_scope() as session:
        stmt = select(ZoneIntervalStateRecord).where(
            ZoneIntervalStateRecord.scope == scope, ZoneIntervalStateRecord.zone == zone
        )
        return (await session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Candidate / observation scope
# --------------------------------------------------------------------------- #

BUY = {"pe_percentile_5y": 0.10, "revenue_growth_3y": 0.16, "roe": 0.20}
OUTSIDE = {"pe_percentile_5y": 0.50, "revenue_growth_3y": 0.16, "roe": 0.20}
DROP = {"pe_percentile_5y": 0.90, "revenue_growth_3y": 0.16, "roe": 0.20}


@pytest.mark.asyncio
async def test_buy_zone_enters_once_and_notifies_only_on_entry(env, factors) -> None:
    rule_id = await _active_rule(scope="candidate")
    factors.set(**BUY)

    first = await _run(scope="candidate")
    assert len(first.created) == 1
    events = await _events()
    assert [event.zone for event in events] == ["buy_zone"]
    event = events[0]
    assert event.transition == "entered"
    assert event.level == "info"
    assert event.rule_instance_id == rule_id
    assert event.rule_version == 1
    assert event.template_id == TEMPLATE
    assert event.calibration_status == "uncalibrated"
    assert event.notified_at is not None
    assert [reason["factor"] for reason in event.conditions_met] == ["pe_percentile_5y"]
    assert event.conditions_met[0]["actual"] == 0.10
    assert event.conditions_met[0]["snapshot_ref"]["capture"] == 0
    assert event.input_digest and event.inputs["rule_instance_id"] == rule_id

    # Same facts, same rule version: reproducible, no new event, no new notice.
    again = await _run(scope="candidate")
    assert again.created == () and again.result.notified == ()
    assert len(await _events()) == 1

    # Still inside the next day: the day's own event (a new as_of / input pair
    # is a new identity) but no further notice (funnel 9).
    tomorrow = await _run(as_of=DAY + timedelta(days=1), scope="candidate")
    assert len(tomorrow.created) == 1 and tomorrow.result.notified == ()

    # Leaving records an event without a notice.
    factors.set(**OUTSIDE)
    left = await _run(as_of=DAY + timedelta(days=2), scope="candidate")
    assert len(left.created) == 1
    assert left.result.notified == ()
    events = await _events()
    assert [event.transition for event in events] == ["entered", "entered", "left"]
    assert events[2].zone == "buy_zone" and events[2].conditions_met == []
    assert events[2].notified_at is None
    state = await _state(zone="buy_zone")
    assert state is not None and state.inside is False and state.entered_as_of is None

    # Staying outside adds nothing at all.
    outside_again = await _run(as_of=DAY + timedelta(days=3), scope="candidate")
    assert outside_again.created == () and len(await _events()) == 3

    # Re-entering notifies once more.
    factors.set(**BUY)
    back = await _run(as_of=DAY + timedelta(days=4), scope="candidate")
    assert len(back.created) == 1 and len(back.result.notified) == 1
    events = await _events()
    assert [event.transition for event in events] == ["entered", "entered", "left", "entered"]
    assert len([event for event in events if event.notified_at is not None]) == 2


@pytest.mark.asyncio
async def test_revised_inputs_are_a_new_identity_without_a_new_notice(env, factors) -> None:
    await _active_rule(scope="candidate")
    factors.set(**BUY)
    await _run(scope="candidate")
    factors.bump_inputs()  # revised snapshot pair, same values and rule version

    revised = await _run(scope="candidate")
    assert len(revised.created) == 1
    assert revised.result.notified == ()
    events = await _events()
    assert len(events) == 2
    assert len({event.input_digest for event in events}) == 2
    assert all(event.notified_at is not None for event in events[:1])
    assert events[1].notified_at is None


@pytest.mark.asyncio
async def test_drop_archives_the_open_candidate_and_records_the_rule(env, factors) -> None:
    rule_id = await _active_rule(scope="candidate")
    async with session_scope() as session:
        session.add(
            CandidateRecord(
                listing_id=env.listing_id,
                ticker=TICKER,
                status="open",
                opened_at=T,
                expires_at=T + timedelta(days=14),
                sources=[{"kind": "manual"}],
                draft_state="none",
            )
        )
    factors.set(**DROP)

    outcome = await _run(scope="candidate")
    assert len(outcome.created) == 1
    assert len(outcome.result.archived_candidates) == 1
    events = await _events()
    assert [event.zone for event in events] == ["drop"]
    assert events[0].level == "action_required"

    async with session_scope() as session:
        candidate = await session.scalar(select(CandidateRecord))
        assert candidate.status == "dismissed" and candidate.closed_at is not None
        decisions = list((await session.scalars(select(DecisionRecord))).all())
        assert [row.kind for row in decisions] == ["drop"]
        decision = decisions[0]
        assert decision.actor == "system"
        assert decision.rule_instance_id == rule_id
        assert f"{TEMPLATE}@1" in decision.rule_ref
        assert decision.context["zone_event"]["zone"] == "drop"
        assert decision.context["zone_event"]["conditions_met"][0]["factor"] == "pe_percentile_5y"

    # A second run the same day neither re-archives nor re-notifies.
    again = await _run(scope="candidate")
    assert again.created == () and again.result.archived_candidates == ()
    async with session_scope() as session:
        assert len(list((await session.scalars(select(DecisionRecord))).all())) == 1


@pytest.mark.asyncio
async def test_without_an_active_rule_no_event_is_ever_recorded(env, factors) -> None:
    factors.set(**BUY)
    outcome = await _run(scope="candidate")
    assert outcome.rules == 0 and outcome.created == ()
    assert await _events() == []


@pytest.mark.asyncio
async def test_zone_summaries_expose_the_interval_and_latest_event(env, factors) -> None:
    rule_id = await _active_rule(scope="candidate")
    factors.set(**BUY)
    await _run(scope="candidate")

    async with session_scope() as session:
        summaries = await zd.zone_summaries(
            session, listing_ids=[env.listing_id], scope="candidate"
        )
    summary = summaries[env.listing_id]
    assert summary["interval"]["inside"] is True
    assert summary["interval"]["zone"] == "buy_zone"
    assert summary["interval"]["entered_as_of"] == DAY
    assert summary["interval"]["rule_instance_id"] == rule_id
    assert summary["interval"]["notified_at"] is not None
    assert set(summary["zones"]) >= {"buy_zone", "drop"}
    assert summary["zones"]["buy_zone"]["inside"] is True
    assert summary["zones"]["drop"]["inside"] is False
    assert summary["latest"]["zone"] == "buy_zone"
    assert summary["latest"]["rule_version"] == 1
    assert summary["latest"]["rule_ref"] == f"{TICKER}/{TEMPLATE}@1/v1"


# --------------------------------------------------------------------------- #
# Holding scope (real ledger + real valuation)
# --------------------------------------------------------------------------- #


async def _ledger(env, *, quantity: str = "100", average_cost: str = "10", cash: str = "50000") -> ObjectRef:
    ref = ObjectRef(kind="listing", id=env.listing_id, revision=1)
    async with session_scope() as session:
        account = await PortfolioService(session).create_account(
            CreateAccount(idempotency_key=uuid4(), name="Zone fixture")
        )
        service = PortfolioService(session)
        await service.append(
            AppendEvent(
                account_id=account.id,
                expected_revision=0,
                idempotency_key=uuid4(),
                fact=Balance(
                    kind="opening_balance",
                    occurred_at=T,
                    source="user statement",
                    user_confirmed=True,
                    coverage="complete",
                    asset_scope="This explicit brokerage account",
                    holdings=(
                        Holding(
                            listing_ref=ref,
                            quantity=D(quantity),
                            average_cost=D(average_cost),
                            opened_on=DAY - timedelta(days=30),
                        ),
                    ),
                    cash=D(cash),
                    prior_history="included_in_confirmed_balance",
                ),
            )
        )
    async with session_scope() as session:
        valuation = await ValuationService(session).ensure(
            account.id, valuation_date=DAY, now=T
        )
        assert valuation is not None
    async with session_scope() as session:
        row = await session.get(PortfolioAccount, str(account.id))
        assert row.current_valuation_id == str(valuation.id)
        assert valuation.holdings[0].quantity == D(quantity)
    return ref


@pytest.mark.asyncio
async def test_holding_zones_use_the_valuation_position_factors(env, factors) -> None:
    await _ledger(env, quantity="100", average_cost="10")
    rule_id = await _active_rule(scope="holding")
    # add: pe <= 0.25 and position_weight < 0.08; trim: gain >= 0.50 ...
    factors.set(pe_percentile_5y=0.10, gross_margin=0.60, roe=0.20, net_profit_growth_3y=0.10)

    outcome = await _run(scope="holding")
    assert outcome.created, outcome.metadata()
    events = await _events(scope="holding")
    entered = [event for event in events if event.transition == "entered"]
    assert [event.zone for event in entered] == ["add"]
    add = entered[0]
    assert add.rule_instance_id == rule_id
    # The valuation provenance is frozen into the event (plan 2.10 / 5.4).
    assert add.ledger_revision == 1
    assert add.knowledge_cutoff is not None
    assert len(add.valuation_ids) == 1
    assert add.inputs["valuation"]["positions"][0]["valuation_id"] == add.valuation_ids[0]
    reasons = {reason["factor"]: reason for reason in add.conditions_met}
    assert set(reasons) == {"pe_percentile_5y", "position_weight"}
    # 100 shares at the 10 CNY fixture close with 50000 cash -> 1000/51000.
    assert reasons["position_weight"]["actual"] == pytest.approx(0.0196078, abs=1e-6)

    # A price rise makes gain_vs_cost fire trim (info), exit_review stays quiet.
    async with session_scope() as session:
        session.add(
            MarketSnapshot(
                listing_id=env.listing_id,
                trade_date=DAY + timedelta(days=1),
                open=D(17),
                high=D(17),
                low=D(17),
                close=D(17),
                volume=D(1),
                adjust="none",
                provider="fixture",
                captured_at=T + timedelta(days=1),
                version=1,
            )
        )
    async with session_scope() as session:
        account = await session.scalar(select(PortfolioAccount))
        valuation = await ValuationService(session).ensure(
            account.id, valuation_date=DAY + timedelta(days=1), now=T + timedelta(days=1)
        )
        assert valuation is not None
    factors.set(pe_percentile_5y=0.95)
    trim = await _run(as_of=DAY + timedelta(days=1), scope="holding")
    assert trim.created and trim.result.notified
    events = await _events(scope="holding")
    trims = [event for event in events if event.zone == "trim"]
    assert [event.transition for event in trims] == ["entered"]
    assert trims[0].level == "info"
    trim_reasons = {reason["factor"] for reason in trims[0].conditions_met}
    assert "gain_vs_cost" in trim_reasons


@pytest.mark.asyncio
async def test_exit_review_is_action_required_and_does_not_touch_the_ledger(env, factors) -> None:
    await _ledger(env)
    await _active_rule(scope="holding")
    factors.set(pe_percentile_5y=0.10, gross_margin=0.10, roe=0.20, net_profit_growth_3y=0.10)

    outcome = await _run(scope="holding")
    assert outcome.created
    async with session_scope() as session:
        exits = list(
            (
                await session.scalars(
                    select(ZoneEventRecord).where(ZoneEventRecord.zone == "exit_review")
                )
            ).all()
        )
    assert len(exits) == 1
    assert exits[0].level == "action_required"
    assert [reason["factor"] for reason in exits[0].conditions_met] == ["gross_margin"]
    # A rule event is a prompt and a record: facts are untouched.
    async with session_scope() as session:
        account = await session.scalar(select(PortfolioAccount))
        valuation = await ValuationService(session).current(account.id)
        assert valuation.holdings[0].quantity == D(100)
        assert account.revision == valuation.ledger_revision


@pytest.mark.asyncio
async def test_holding_rule_without_a_position_is_skipped(env, factors) -> None:
    await _active_rule(scope="holding")
    factors.set(pe_percentile_5y=0.10)
    outcome = await _run(scope="holding")
    assert outcome.created == ()
    assert outcome.evaluated == ()
    assert any(item.endswith(":not_held") for item in outcome.skipped)


@pytest.mark.asyncio
async def test_unavailable_factors_are_reported_not_invented(env, factors) -> None:
    """No factor available: the zone does not hold and nothing is claimed."""

    await _ledger(env)
    await _active_rule(scope="holding")
    factors.set()  # nothing available at all

    outcome = await _run(scope="holding")
    assert outcome.created == ()
    assert await _events() == []
    # Every zone was evaluated (a missing factor means "does not hold", the
    # screen engine's documented three-valued rule) and the state is outside.
    state = await _state(zone="trim", scope="holding")
    assert state is not None and state.inside is False
    assert state.last_evaluated_as_of == DAY


@pytest.mark.asyncio
async def test_not_applicable_zone_is_undecided_and_records_nothing(env) -> None:
    """All conditions skipped (not_applicable): unknown, not "outside"."""

    from investment_agent.rules.schema import ConditionGroup
    from investment_agent.rules.screen import evaluate_zone_outcomes

    zone = ConditionGroup.model_validate(
        {"all": [{"factor": "debt_ratio", "op": "<", "value": 0.55}]}
    )
    factors = {
        "debt_ratio": FactorValue("debt_ratio", "fixture", None, "not_applicable"),
    }
    outcomes = evaluate_zone_outcomes(zones={"add": zone}, factors=factors, params={})
    assert outcomes["add"].holds is None

    async with session_scope() as session:
        rule = zd.ZoneRule(
            listing_id=env.listing_id,
            ticker=TICKER,
            scope="holding",
            rule_instance_id=1,
            rule_version=1,
            template_id=TEMPLATE,
            template_version=1,
            calibration_status="uncalibrated",
            zones={"add": zone},
            params={},
        )
        evaluation = zd.evaluate_rule_zones(rule, as_of=DAY, factors=factors)
        assert evaluation.signals == {} and evaluation.undecided == ("add",)
        result = await zd.record_evaluations(session, [evaluation])
    assert result.created == ()
    async with session_scope() as session:
        assert (
            await session.scalar(select(ZoneIntervalStateRecord))
        ) is None
        assert await _events() == []


# --------------------------------------------------------------------------- #
# Job plumbing
# --------------------------------------------------------------------------- #


CALENDAR_YAML = """\
calendar_id: fixture
timezone: Asia/Shanghai
source:
  title: fixture
  url: https://example.invalid/fixture
  published_on: 2025-12-01
years:
- year: 2026
  closed_dates: []
"""

CALENDAR_YAML_STALE = """\
calendar_id: fixture-stale
timezone: Asia/Shanghai
source:
  title: fixture
  url: https://example.invalid/fixture
  published_on: 2024-12-01
years:
- year: 2025
  closed_dates: []
"""


async def _run_handler(
    env, *, as_of: date, scope: str, reason: str = "cron", calendar_path: str | None = None
) -> int:
    run_id = await _enqueue(as_of=as_of, scope=scope, reason=reason)
    worker = ResearchWorker(
        settings=env.settings,
        worker_id="zone-worker",
        handlers=[
            ZoneDetectionHandler(settings=env.settings, calendar_path=calendar_path)
        ],
    )
    assert await worker.run_once(run_id=run_id) == run_id
    return run_id


async def _enqueue(*, as_of: date, scope: str, reason: str = "cron") -> int:
    async with session_scope() as session:
        run, created = await enqueue_zone_detection(
            session, as_of=as_of, scope=scope, triggered_by="test", reason=reason
        )
        assert created
        return run.id


@pytest.mark.asyncio
async def test_job_is_idempotent_per_scope_date_and_reason(env, factors, tmp_path) -> None:
    await _active_rule(scope="candidate")
    factors.set(**BUY)
    calendar = tmp_path / "calendar.yaml"
    calendar.write_text(CALENDAR_YAML, encoding="utf-8")
    run_id = await _run_handler(env, as_of=DAY, scope="candidate", calendar_path=str(calendar))

    async with session_scope() as session:
        stored = await session.get(PipelineRun, run_id)
        assert stored.status == "success"
        assert stored.metadata_json["scope"] == "candidate"
        assert len(stored.metadata_json["created_events"]) == 1
        run, created = await enqueue_zone_detection(
            session, as_of=DAY, scope="candidate", triggered_by="test", reason="cron"
        )
        assert created is False and run.id == run_id

    # A different trigger token is a new run: the same inputs still insert nothing.
    await _run_handler(env, as_of=DAY, scope="candidate", reason="close_quote", calendar_path=str(calendar))
    assert len(await _events()) == 1


@pytest.mark.asyncio
async def test_candidate_scope_is_gated_to_reviewed_trading_days(env, factors, tmp_path) -> None:
    await _active_rule(scope="candidate")
    factors.set(**BUY)
    calendar = tmp_path / "calendar.yaml"
    calendar.write_text(CALENDAR_YAML, encoding="utf-8")
    saturday = DAY + timedelta(days=4)
    assert saturday.weekday() == 5
    run_id = await _run_handler(
        env, as_of=saturday, scope="candidate", calendar_path=str(calendar)
    )
    async with session_scope() as session:
        stored = await session.get(PipelineRun, run_id)
        assert stored.metadata_json["skipped_reason"] == "non_trading_day"
    assert await _events() == []

    await _run_handler(env, as_of=DAY, scope="candidate", calendar_path=str(calendar))
    assert len(await _events()) == 1


@pytest.mark.asyncio
async def test_unreviewed_calendar_year_keeps_the_candidate_gate_closed(env, factors, tmp_path) -> None:
    """A calendar without the year cannot vouch for the day: the candidate
    gate stays closed instead of producing weekend noise events."""

    await _active_rule(scope="candidate")
    factors.set(**BUY)
    stale = tmp_path / "calendar-stale.yaml"
    stale.write_text(CALENDAR_YAML_STALE, encoding="utf-8")
    run_id = await _run_handler(env, as_of=DAY, scope="candidate", calendar_path=str(stale))
    async with session_scope() as session:
        stored = await session.get(PipelineRun, run_id)
        assert stored.metadata_json["skipped_reason"] == "trading_day_unknown"
    assert await _events() == []

    # Holding scope keeps its every-day semantics - the gate does not apply.
    holding_run = await _run_handler(env, as_of=DAY, scope="holding", calendar_path=str(stale))
    async with session_scope() as session:
        stored = await session.get(PipelineRun, holding_run)
        assert "skipped_reason" not in stored.metadata_json


@pytest.mark.asyncio
async def test_holding_scope_ignores_the_trading_day_gate(env, factors, tmp_path) -> None:
    await _ledger(env)
    await _active_rule(scope="holding")
    factors.set(pe_percentile_5y=0.10, gross_margin=0.60, roe=0.20, net_profit_growth_3y=0.10)
    calendar = tmp_path / "calendar.yaml"
    calendar.write_text(CALENDAR_YAML, encoding="utf-8")
    saturday = DAY + timedelta(days=4)
    await _run_handler(env, as_of=saturday, scope="holding", calendar_path=str(calendar))
    events = await _events()
    assert [event.scope for event in events] == ["holding"]
    # The event carries the valuation's own date, not the run's Saturday.
    assert events[0].as_of == DAY


@pytest.mark.asyncio
async def test_zone_summaries_keep_only_each_listings_latest_event(env, factors) -> None:
    """Multi-listing pages: the latest ``entered`` event is picked per listing
    in the database, not by scanning every historical event."""

    await _active_rule(scope="candidate")
    factors.set(**BUY)
    await _run(scope="candidate")
    real_latest = (await _events())[-1].id

    async with session_scope() as session:
        rule_id = (await session.scalars(select(ZoneEventRecord.rule_instance_id).limit(1))).one()
        company = Company(canonical_name="五粮液")
        session.add(company)
        await session.flush()
        other = Listing(company_id=company.id, market_code="SSE", ticker="000858")
        session.add(other)
        await session.flush()
        other_id = other.id

        def raw_event(listing_id: int, ticker: str, as_of: date, zone: str) -> None:
            session.add(
                ZoneEventRecord(
                    as_of=as_of,
                    listing_id=listing_id,
                    ticker=ticker,
                    scope="candidate",
                    zone=zone,
                    transition="entered",
                    level="info",
                    rule_instance_id=rule_id,
                    rule_version=1,
                    template_id=TEMPLATE,
                    template_version=1,
                    calibration_status="uncalibrated",
                    conditions_met=[],
                    inputs={},
                    input_digest=f"digest-{listing_id}-{as_of.isoformat()}-{zone}",
                )
            )

        # An older entered event of the first listing must not win, and the
        # second listing keeps its own latest (not another listing's).
        raw_event(env.listing_id, TICKER, DAY - timedelta(days=1), "drop")
        raw_event(other_id, "000858", DAY, "buy_zone")
        raw_event(other_id, "000858", DAY + timedelta(days=1), "drop")

    async with session_scope() as session:
        summaries = await zd.zone_summaries(
            session, listing_ids=[env.listing_id, other_id], scope="candidate"
        )
    assert summaries[env.listing_id]["latest"]["id"] == real_latest
    assert summaries[other_id]["latest"]["zone"] == "drop"
    assert summaries[other_id]["latest"]["as_of"] == DAY + timedelta(days=1)


@pytest.mark.asyncio
async def test_closing_the_position_records_left_and_clears_the_interval(env, factors) -> None:
    """A closed position cannot leave a holding zone stuck inside forever."""

    await _ledger(env)
    rule_id = await _active_rule(scope="holding")
    factors.set(pe_percentile_5y=0.10, gross_margin=0.60, roe=0.20, net_profit_growth_3y=0.10)
    first = await _run(scope="holding")
    assert first.created
    state = await _state(zone="add", scope="holding")
    assert state is not None and state.inside is True

    # The position is fully closed: the next valuation has no holdings.
    async with session_scope() as session:
        account = await session.scalar(select(PortfolioAccount))
        await PortfolioService(session).append(
            AppendEvent(
                account_id=account.id,
                expected_revision=1,
                idempotency_key=uuid4(),
                fact=Balance(
                    kind="reconciliation",
                    occurred_at=T + timedelta(days=1),
                    source="position closed",
                    user_confirmed=True,
                    coverage="complete",
                    asset_scope="This explicit brokerage account",
                    holdings=(),
                    cash=D("51000"),
                    unexplained_difference="all positions closed",
                    prior_history="included_in_confirmed_balance",
                ),
            )
        )
    async with session_scope() as session:
        account = await session.scalar(select(PortfolioAccount))
        valuation = await ValuationService(session).ensure(
            account.id, valuation_date=DAY + timedelta(days=1), now=T + timedelta(days=1)
        )
        assert valuation is not None

    outcome = await _run(as_of=DAY + timedelta(days=1), scope="holding")
    assert f"{TICKER}:not_held" in outcome.skipped
    lefts = [event for event in await _events(scope="holding") if event.transition == "left"]
    assert [event.zone for event in lefts] == ["add"]
    assert lefts[0].rule_instance_id == rule_id
    assert lefts[0].conditions_met == [] and lefts[0].notified_at is None
    assert lefts[0].inputs["position_closed"] is True
    state = await _state(zone="add", scope="holding")
    assert state is not None
    assert state.inside is False and state.entered_as_of is None

    # Re-running the same day stays idempotent (the left event replays).
    again = await _run(as_of=DAY + timedelta(days=1), scope="holding")
    assert again.created == ()
    lefts = [event for event in await _events(scope="holding") if event.transition == "left"]
    assert len(lefts) == 1


@pytest.mark.asyncio
async def test_holding_events_read_factors_at_the_event_date(env, factors) -> None:
    """The event's factor payload is what was knowable on the event's own
    date (the valuation date), not on the later run date."""

    await _ledger(env)  # valuation dated DAY
    await _active_rule(scope="holding")
    factors.set(pe_percentile_5y=0.10, gross_margin=0.60, roe=0.20, net_profit_growth_3y=0.10)
    outcome = await _run(as_of=DAY + timedelta(days=1), scope="holding")
    assert outcome.created
    event = (await _events(scope="holding"))[0]
    assert event.as_of == DAY
    ref = event.inputs["factors"]["pe_percentile_5y"]["ref"]
    assert ref["as_of"] == DAY.isoformat()


@pytest.mark.asyncio
async def test_a_failed_notification_does_not_poison_the_run(env, factors, monkeypatch) -> None:
    """The outbox write runs in its own savepoint: a failed notification flush
    rolls back alone and the detection run still records its event."""

    from sqlalchemy import event as sa_event

    from investment_agent.investor.controls import UserControlService
    from investment_agent.storage.models.monitoring import InvestorNotification

    async def allowed(self, case_id):  # noqa: ANN001
        return True

    monkeypatch.setattr(UserControlService, "notifications_allowed", allowed)
    await _active_rule(scope="candidate")
    from investment_agent.storage.models.investor import InvestorCase

    async with session_scope() as session:
        session.add(
            InvestorCase(
                id=str(uuid4()),
                owner_id=zd.OWNER,
                listing_id=env.listing_id,
                revision=1,
                payload={},
                source_refs=[],
            )
        )
    factors.set(**BUY)

    def boom(mapper, connection, target) -> None:
        raise RuntimeError("notification write failed")

    sa_event.listen(InvestorNotification, "before_insert", boom)
    try:
        outcome = await _run(scope="candidate", settings=env.settings)
    finally:
        sa_event.remove(InvestorNotification, "before_insert", boom)

    assert len(outcome.created) == 1
    async with session_scope() as session:
        notifications = await session.scalar(
            select(func.count()).select_from(InvestorNotification)
        )
        assert notifications == 0


@pytest.mark.asyncio
async def test_instance_zones_outside_the_whitelist_are_skipped(env, factors) -> None:
    """A leftover or hand-edited rule row cannot invent a zone the template
    never declared."""

    from investment_agent.rules.schema import RuleInstance

    async with session_scope() as session:
        store = RuleStore(session)
        template = await store.get_template(TEMPLATE)
        record = await store.create_draft(
            RuleInstance(
                template_ref=template.ref,
                symbol=TICKER,
                scope="candidate",
                params={},
                drafted_by="user",
            )
        )
        definition = dict(record.definition or {})
        definition["zones"] = {
            "buy_zone": {"all": [{"factor": "pe_percentile_5y", "op": "<", "value": 0.3}]},
            "moon_zone": {"all": [{"factor": "roe", "op": ">", "value": 0.0}]},
        }
        record.definition = definition
        await store.approve(record.id, approved_by="user")

    async with session_scope() as session:
        rules = await zd.active_zone_rules(session, "candidate")
    assert len(rules) == 1
    assert set(rules[0].zones) == {"buy_zone"}

    factors.set(**BUY)
    await _run(scope="candidate")
    assert [event.zone for event in await _events()] == ["buy_zone"]


@pytest.mark.asyncio
async def test_reservation_lock_keeps_updated_at(env, factors) -> None:
    """The SQLite writer reservation is a no-op that must not refresh the
    state row's audit timestamp."""

    await _active_rule(scope="candidate")
    factors.set(**BUY)
    await _run(scope="candidate")  # creates the interval state row
    pinned = datetime(2000, 1, 1)

    async with session_scope() as session:
        await session.execute(
            update(ZoneIntervalStateRecord).where(
                ZoneIntervalStateRecord.scope == "candidate"
            ).values(updated_at=pinned)
        )
    async with session_scope() as session:
        state = await zd._locked_state(
            session, listing_id=env.listing_id, scope="candidate", zone="buy_zone"
        )
        assert state is not None
        assert state.updated_at == pinned


def test_payload_rejects_unknown_scope() -> None:
    with pytest.raises(ValueError):
        ZoneDetectionPayload(as_of=DAY, scope="watching")
    assert shanghai_today() is not None

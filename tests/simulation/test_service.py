from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    TradingCalendarSource,
    TradingCalendarYear,
)
from investment_agent.simulation.contracts import Decision, ExperimentConfig, OrderIntent
from investment_agent.simulation.data import ExecutionEvidenceInput
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.ledger import CorporateAction
from investment_agent.simulation.service import SimulationService
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.simulation import (
    SimulationAccountRecord,
    SimulationLedgerEventRecord,
)
from investment_agent.storage.repositories.listing import ListingRepository

DAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 9, tzinfo=UTC)
DEADLINE = datetime(2026, 11, 10, 0, tzinfo=UTC)


async def add_bar(session, listing_id, day, *, price="10", close="10", captured=None):
    session.add(
        MarketSnapshot(
            listing_id=listing_id,
            trade_date=day,
            open=Decimal(price),
            high=max(Decimal(price), Decimal(close)),
            low=min(Decimal(price), Decimal(close)),
            close=Decimal(close),
            volume=Decimal("100000000"),
            amount=Decimal("1000000000"),
            trade_status=1,
            is_st=False,
            adjust="none",
            provider="fixture",
            version=1,
            captured_at=captured or datetime.combine(day, time(8), UTC),
            raw_ref="archive:fixture",
            is_backfill=False,
        )
    )
    await session.flush()


async def add_proof(service, experiment_id, listing_id, day):
    return await service.ingest_execution_evidence(
        experiment_id,
        ExecutionEvidenceInput(
            listing_id=listing_id,
            trade_date=day,
            exchange="SSE",
            board="mainboard",
            security_type="ordinary_a",
            ipo_date=date(2000, 1, 1),
            ordinary_limit_session=True,
            reference_price="10",
            upper_limit="12",
            lower_limit="8",
            tick_size="0.01",
            lot_size=100,
            is_st=False,
            rule_version="mainboard_v1",
            rule_source="exchange:fixture",
            rule_verified=True,
            corporate_actions_complete=True,
            published_at=datetime.combine(day, time(0), UTC),
            captured_at=datetime.combine(day, time(1), UTC),
        ),
        source_kind="user",
        source_ref="reviewed-exchange-archive",
    )


@pytest.fixture
async def arena(db_session, tmp_path, sim_config, request):
    listings = [
        await ListingRepository(db_session).ensure_a_share_listing(ticker)
        for ticker in ("600519.SH", "600036.SH")
    ]
    calendar = TradingCalendar(
        calendar_id="sse_2026",
        timezone="Asia/Shanghai",
        source=TradingCalendarSource(
            title="fixture", url="https://example.test/calendar", published_on=DAY
        ),
        years=[TradingCalendarYear(year=2026)],
    )
    clock = [NOW]
    service = SimulationService(
        db_session, data_dir=tmp_path, calendar=calendar, now=lambda: clock[0]
    )
    body = sim_config.model_dump() | {"universe": tuple(listing.id for listing in listings)}
    if getattr(request, "param", None) == "memory":
        body["participants"][-1]["memory_policy"] = "matured_own_reviews"
    if getattr(request, "param", None) == "reverse-universe":
        body["universe"] = tuple(reversed(body["universe"]))
    if getattr(request, "param", None) == "huge-fee":
        body["rules"]["fee_schedule"]["minimum_commission"] = Decimal("1000001")
    config = ExperimentConfig.model_validate(body)
    experiment = await service.create(config, idempotency_key="create")
    experiment = await service.start(experiment.id, expected_revision=0, idempotency_key="start")
    for listing in listings:
        await add_bar(db_session, listing.id, DAY)
        await add_proof(service, experiment.id, listing.id, DAY)
    return service, experiment, listings, clock


async def prepare(arena):
    service, experiment, _, _ = arena
    return await service.prepare_round(
        experiment.id,
        observation_date=DAY,
        cutoff=NOW,
        decision_deadline=DEADLINE,
        idempotency_key="prepare",
        require_financials=False,
    )


async def agent_decision(arena, round_, **changes):
    service, experiment, listings, _ = arena
    participant = next(
        p for p in await service.participants(experiment.id) if p.config.kind == "agent"
    )
    account = await service.account(experiment.id, participant.id)
    decision = Decision(
        experiment_id=experiment.id,
        participant_id=participant.id,
        account_id=account.account_id,
        id="client-decision",
        round_id=round_.id,
        account_revision=account.revision,
        status="submitted",
        action="trade",
        orders=(
            OrderIntent(
                listing_id=listings[0].id, side="buy", quantity="100", protection_price="10.1"
            ),
        ),
        rationale="frozen observation",
        submitted_at=NOW,
        idempotency_key="agent-decision",
    )
    if changes:
        decision = Decision.model_validate(decision.model_dump() | changes)
    return participant, decision


async def locked(arena):
    service, experiment, _, clock = arena
    round_ = await prepare(arena)
    participant, decision = await agent_decision(arena, round_)
    await service.submit_decision(experiment.id, round_.id, participant.id, decision)
    clock[0] = DEADLINE
    return await service.close_round(experiment.id, round_.id)


async def test_durable_two_day_flow_and_replays(arena, db_session):
    service, experiment, listings, clock = arena
    round_ = await prepare(arena)
    assert round_.status == "collecting_decisions"
    participant, decision = await agent_decision(arena, round_)
    accepted = await service.submit_decision(experiment.id, round_.id, participant.id, decision)
    assert (
        await service.submit_decision(experiment.id, round_.id, participant.id, decision)
        == accepted
    )
    with pytest.raises(SimulationError, match="another request"):
        await service.submit_decision(
            experiment.id,
            round_.id,
            participant.id,
            Decision.model_validate(decision.model_dump() | {"rationale": "changed"}),
        )
    clock[0] = DEADLINE
    await service.close_round(experiment.id, round_.id)
    assert len(await service.orders(experiment.id)) == 3
    clock[0] = datetime(2026, 11, 10, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 10), close="11")
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 10))
    settled = await service.settle_round(experiment.id, round_.id)
    assert settled.status == "settled"
    fills = await service.fills(experiment.id)
    assert len(fills) == 3
    assert await service.settle_round(experiment.id, round_.id) == settled
    assert await service.fills(experiment.id) == fills
    agent = await service.account(experiment.id, participant.id)
    assert agent.state.cash == Decimal("998994")
    assert agent.state.lots[0].available_on == date(2026, 11, 11)
    cash = next(p for p in await service.participants(experiment.id) if p.config.kind == "cash")
    assert (await service.account(experiment.id, cash.id)).state.cash == Decimal("1000000")
    board = await service.leaderboard(experiment.id)
    assert board["common_observations"] == 2
    assert all(row["official"] for row in board["rows"])
    assert len(await service.value(experiment.id, date(2026, 11, 10))) == 3
    events = sorted(await service.ledger(experiment.id, participant.id), key=lambda e: e.sequence)
    assert [e.kind for e in events] == ["initial_cash", "reserve", "fill"]
    assert events[-1].state_after == agent.state
    await db_session.commit()
    assert (await service.export(experiment.id))["schema_version"] == "simulation_export_v1"
    second = await service.prepare_round(
        experiment.id,
        observation_date=date(2026, 11, 10),
        cutoff=clock[0],
        decision_deadline=datetime(2026, 11, 11, 0, tzinfo=UTC),
        idempotency_key="same-week",
        require_financials=False,
    )
    assert second.status == "skipped"


async def test_missing_execution_proof_recovers_without_double_fills(arena, db_session):
    service, experiment, listings, clock = arena
    round_ = await locked(arena)
    clock[0] = datetime(2026, 11, 10, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 10))
    waiting = await service.settle_round(experiment.id, round_.id)
    assert waiting.status == "awaiting_execution_data"
    assert not await service.fills(experiment.id)
    assert any(n.quality == "incomplete" for n in await service.nav(experiment.id))
    for listing in listings:
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 10))
    assert (await service.settle_round(experiment.id, round_.id)).status == "settled"
    assert len(await service.fills(experiment.id)) == 3
    assert all(row["official"] for row in (await service.leaderboard(experiment.id))["rows"])


async def test_scope_revision_and_late_submission(arena):
    service, experiment, _, clock = arena
    round_ = await prepare(arena)
    participant, decision = await agent_decision(arena, round_)
    other = next(p for p in await service.participants(experiment.id) if p.id != participant.id)
    with pytest.raises(SimulationError, match="identity"):
        await service.submit_decision(experiment.id, round_.id, other.id, decision)
    with pytest.raises(SimulationError, match="stale"):
        await service.submit_decision(
            experiment.id,
            round_.id,
            participant.id,
            Decision.model_validate(decision.model_dump() | {"account_revision": 0}),
        )
    clock[0] = DEADLINE + timedelta(seconds=1)
    with pytest.raises(SimulationError, match="window"):
        await service.submit_decision(experiment.id, round_.id, participant.id, decision)


async def test_pause_cancels_future_orders_but_preserves_past_economics(arena):
    service, experiment, _, clock = arena
    round_ = await locked(arena)
    paused = await service.pause(
        experiment.id, expected_revision=experiment.revision, idempotency_key="pause"
    )
    assert paused.status == "paused"
    assert all(o.status == "cancelled" for o in await service.orders(experiment.id))
    assert (await service.get_round(experiment.id, round_.id)).status == "cancelled"
    for participant in await service.participants(experiment.id):
        assert (await service.account(experiment.id, participant.id)).state.reserved_cash == 0


async def test_pause_after_open_keeps_orders_for_honest_settlement(arena):
    service, experiment, _, clock = arena
    await locked(arena)
    clock[0] = datetime(2026, 11, 10, 2, tzinfo=UTC)
    await service.pause(
        experiment.id, expected_revision=experiment.revision, idempotency_key="pause"
    )
    assert all(o.status == "locked" for o in await service.orders(experiment.id))


async def test_account_payload_scope_corruption_is_rejected(arena, db_session):
    service, experiment, _, _ = arena
    participant = (await service.participants(experiment.id))[0]
    row = await db_session.get(SimulationAccountRecord, participant.account_id)
    row.payload = row.payload | {"participant_id": "another"}
    await db_session.flush()
    with pytest.raises(SimulationError, match="differs from payload"):
        await service.account(experiment.id, participant.id)


async def test_missing_decision_records_timeout_and_never_backfills_orders(arena):
    service, experiment, _, clock = arena
    round_ = await prepare(arena)
    clock[0] = datetime(2026, 11, 10, 2, tzinfo=UTC)
    closed = await service.close_round(experiment.id, round_.id)
    assert closed.status == "skipped"
    assert not await service.orders(experiment.id)
    assert all(
        d.action == "no_submission"
        for d in await service.decisions(experiment.id)
        if d.status == "timeout"
    )


async def test_corporate_action_receivable_payment_scope_and_replay(arena, db_session):
    service, experiment, listings, clock = arena
    participant = next(
        p for p in await service.participants(experiment.id) if p.config.kind == "agent"
    )
    round_ = await locked(arena)
    clock[0] = datetime(2026, 11, 10, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 10))
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 10))
    await service.settle_round(experiment.id, round_.id)
    clock[0] = datetime(2026, 11, 11, 9, tzinfo=UTC)
    account = await service.account(experiment.id, participant.id)
    before_cash = account.state.cash
    action = CorporateAction(
        id="dividend-entitlement",
        listing_id=listings[0].id,
        kind="cash_entitlement",
        economic_at=datetime(2026, 11, 11, 1, tzinfo=UTC),
        confirmed_at=clock[0],
        source_ref="official:dividend",
        verified=True,
        amount="10",
        entitlement_ref="record-date-proof",
    )
    account = await service.corporate_action(
        experiment.id,
        participant.id,
        action,
        expected_revision=account.revision,
        idempotency_key="entitlement",
        entitlement_date=date(2026, 11, 10),
        cash_per_share=Decimal("0.1"),
    )
    assert account.state.receivables == 10
    bad_payment = action.model_copy(
        update={
            "id": "wrong-dividend",
            "kind": "cash_payment",
            "entitlement_ref": "other-entitlement",
        }
    )
    with pytest.raises(SimulationError, match="account/listing entitlement"):
        await service.corporate_action(
            experiment.id,
            participant.id,
            bad_payment,
            expected_revision=account.revision,
            idempotency_key="wrong-payment",
        )
    payment = action.model_copy(update={"id": "dividend-paid", "kind": "cash_payment"})
    result = await service.corporate_action(
        experiment.id,
        participant.id,
        payment,
        expected_revision=account.revision,
        idempotency_key="payment",
    )
    assert result.state.cash == before_cash + Decimal("10") and result.state.receivables == 0
    assert (
        await service.corporate_action(
            experiment.id,
            participant.id,
            payment,
            expected_revision=account.revision,
            idempotency_key="payment",
        )
        == result
    )
    assert (
        len(
            (
                await db_session.scalars(
                    select(SimulationLedgerEventRecord).where(
                        SimulationLedgerEventRecord.account_id == account.account_id
                    )
                )
            ).all()
        )
        == 5
    )


async def settled_arena(arena, db_session):
    service, experiment, listings, clock = arena
    round_ = await locked(arena)
    clock[0] = datetime(2026, 11, 10, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 10))
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 10))
    await service.settle_round(experiment.id, round_.id)
    return round_


async def test_round_reservation_metadata_cannot_override_frozen_facts(arena, db_session):
    from investment_agent.storage.models.simulation import SimulationRoundRecord

    service, experiment, _, _ = arena
    round_ = await prepare(arena)
    row = await db_session.get(SimulationRoundRecord, round_.id)
    payload = dict(row.payload)
    metadata = dict(payload["_service"])
    terms = {key: dict(value) for key, value in metadata["reservation_terms"].items()}
    terms[next(iter(terms))]["historical_turnover"] = "99999999999999999"
    row.payload = payload | {"_service": metadata | {"reservation_terms": terms}}
    await db_session.flush()
    with pytest.raises(SimulationError, match="differs from terms"):
        await service.get_round(experiment.id, round_.id)


async def test_round_price_metadata_cannot_override_archived_close(arena, db_session):
    from investment_agent.storage.models.simulation import SimulationRoundRecord

    service, experiment, listings, _ = arena
    round_ = await prepare(arena)
    row = await db_session.get(SimulationRoundRecord, round_.id)
    metadata = dict(row.payload["_service"])
    row.payload = row.payload | {
        "_service": metadata | {"position_prices": {str(listings[0].id): "99"}}
    }
    await db_session.flush()
    with pytest.raises(SimulationError, match="prices differ"):
        await service.common_facts(experiment.id, round_.id)


async def test_calendar_semantics_changes_stop_progress_but_history_remains_readable(arena):
    service, experiment, _, _ = arena
    service.calendar.years[0].closed_dates.add(date(2026, 11, 10))
    assert (await service.get(experiment.id)).id == experiment.id
    assert (await service.export(experiment.id))["schema_version"] == "simulation_export_v1"
    with pytest.raises(SimulationError, match="sessions differ"):
        await prepare(arena)


async def test_adding_future_calendar_year_does_not_change_frozen_season(arena):
    service, experiment, _, _ = arena
    service.calendar.years.append(TradingCalendarYear(year=2027))
    assert (await service.get(experiment.id)).id == experiment.id
    assert (await prepare(arena)).status == "collecting_decisions"


async def test_submission_replay_cannot_return_another_participant_payload(arena, db_session):
    from investment_agent.storage.models.simulation import SimulationRequestRecord

    service, experiment, _, _ = arena
    round_ = await prepare(arena)
    participant, decision = await agent_decision(arena, round_)
    await service.submit_decision(experiment.id, round_.id, participant.id, decision)
    scope = f"submit:{experiment.id}:{participant.id}:{round_.id}"
    row = await db_session.get(SimulationRequestRecord, (scope, decision.idempotency_key))
    row.payload = row.payload | {"participant_id": "another-participant"}
    await db_session.flush()
    with pytest.raises(SimulationError, match="bound request scope"):
        await service.submit_decision(experiment.id, round_.id, participant.id, decision)


async def test_old_valuation_cannot_read_future_account_projection(arena, db_session):
    service, experiment, _, _ = arena
    await settled_arena(arena, db_session)
    with pytest.raises(SimulationError, match="original account projection"):
        await service.value(experiment.id, DAY)


async def test_settlement_failure_rolls_back_all_accounts_and_retries_once(
    arena, db_session, monkeypatch
):
    service, experiment, listings, clock = arena
    round_ = await locked(arena)
    clock[0] = datetime(2026, 11, 10, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 10))
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 10))
    await db_session.commit()
    persist = service._persist_settlement

    async def persist_then_crash(*args, **kwargs):
        await persist(*args, **kwargs)
        raise RuntimeError("injected account settlement crash")

    monkeypatch.setattr(service, "_persist_settlement", persist_then_crash)
    with pytest.raises(RuntimeError, match="injected"):
        await service.settle_round(experiment.id, round_.id)
    # Production API/jobs own session_scope and roll back on failure.
    await db_session.rollback()
    monkeypatch.setattr(service, "_persist_settlement", persist)
    assert not await service.fills(experiment.id)
    assert all(order.status == "locked" for order in await service.orders(experiment.id))
    assert (await service.settle_round(experiment.id, round_.id)).status == "settled"
    assert len(await service.fills(experiment.id)) == 3


async def test_empty_account_cannot_receive_unproven_dividend(arena):
    service, experiment, listings, clock = arena
    participant = (await service.participants(experiment.id))[0]
    account = await service.account(experiment.id, participant.id)
    clock[0] = datetime(2026, 11, 10, 9, tzinfo=UTC)
    action = CorporateAction(
        id="unearned",
        listing_id=listings[0].id,
        kind="cash_entitlement",
        economic_at=clock[0],
        confirmed_at=clock[0],
        source_ref="official:div",
        verified=True,
        amount="10",
        entitlement_ref="record-proof",
    )
    with pytest.raises(SimulationError, match="record date"):
        await service.corporate_action(
            experiment.id,
            participant.id,
            action,
            expected_revision=account.revision,
            idempotency_key="missing-proof",
        )
    with pytest.raises(SimulationError, match="record-date holdings"):
        await service.corporate_action(
            experiment.id,
            participant.id,
            action,
            expected_revision=account.revision,
            idempotency_key="empty",
            entitlement_date=date(2026, 11, 10),
            cash_per_share=Decimal("0.1"),
        )
    assert (await service.account(experiment.id, participant.id)).state.receivables == 0


async def test_model_review_cannot_shorten_window_or_backdate_memory(arena, db_session):
    from investment_agent.simulation.contracts import SimulationReview

    service, experiment, _, clock = arena
    round_ = await settled_arena(arena, db_session)
    participant = next(
        p for p in await service.participants(experiment.id) if p.config.kind == "agent"
    )
    decision = next(
        d for d in await service.decisions(experiment.id, participant.id) if d.status == "locked"
    )
    end_nav = next(
        n
        for n in await service.nav(experiment.id, participant.id)
        if n.valuation_date == date(2026, 11, 10) and n.quality == "complete"
    )
    review = SimulationReview(
        experiment_id=experiment.id,
        participant_id=participant.id,
        account_id=participant.account_id,
        id="model-result",
        decision_id=decision.id,
        observation_start=date(2026, 11, 10),
        observation_end=date(2026, 11, 10),
        available_at=clock[0],
        status="matured",
        nav_refs=(end_nav.id,),
        findings="premature",
        memory_eligible=True,
    )
    with pytest.raises(SimulationError, match="observation horizon"):
        await service.save_model_review(experiment.id, participant.id, review)
    clock[0] = datetime(2026, 11, 11, 9, tzinfo=UTC)
    for listing in arena[2]:
        await add_bar(db_session, listing.id, date(2026, 11, 11))
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 11))
    await service.value(experiment.id, date(2026, 11, 11))
    end_nav = next(
        n
        for n in await service.nav(experiment.id, participant.id)
        if n.valuation_date == date(2026, 11, 11) and n.quality == "complete"
    )
    review = SimulationReview.model_validate(
        review.model_dump()
        | {
            "observation_end": date(2026, 11, 11),
            "available_at": datetime(2026, 11, 11, 8, tzinfo=UTC),
            "nav_refs": (end_nav.id,),
            "findings": "matured",
        }
    )
    accepted = await service.save_model_review(experiment.id, participant.id, review)
    assert accepted.available_at == clock[0]
    assert await service.save_model_review(experiment.id, participant.id, review) == accepted
    gaps = await service.round_data_gaps(experiment.id, round_.id)
    assert isinstance(gaps, tuple) and all(isinstance(reason, str) for reason in gaps)


async def test_dividend_uses_record_date_holdings_after_all_shares_sold(arena, db_session):
    service, experiment, listings, clock = arena
    await settled_arena(arena, db_session)
    participant = next(
        p for p in await service.participants(experiment.id) if p.config.kind == "agent"
    )
    # The next weekly rebalance sells the original100 shares before accrual.
    clock[0] = datetime(2026, 11, 16, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 16))
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 16))
    round_ = await service.prepare_round(
        experiment.id,
        observation_date=date(2026, 11, 16),
        cutoff=clock[0],
        decision_deadline=datetime(2026, 11, 17, 0, tzinfo=UTC),
        idempotency_key="second-week",
        require_financials=False,
    )
    account = await service.account(experiment.id, participant.id)
    sell = Decision(
        experiment_id=experiment.id,
        participant_id=participant.id,
        account_id=account.account_id,
        id="sell",
        round_id=round_.id,
        account_revision=account.revision,
        status="submitted",
        action="trade",
        orders=(
            OrderIntent(
                listing_id=listings[0].id, side="sell", quantity="100", protection_price="9.9"
            ),
        ),
        rationale="sell before dividend confirmation",
        submitted_at=clock[0],
        idempotency_key="sell-all",
    )
    await service.submit_decision(experiment.id, round_.id, participant.id, sell)
    clock[0] = datetime(2026, 11, 17, 0, tzinfo=UTC)
    await service.close_round(experiment.id, round_.id)
    clock[0] = datetime(2026, 11, 17, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 17))
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 17))
    await service.settle_round(experiment.id, round_.id)
    account = await service.account(experiment.id, participant.id)
    assert not account.state.lots
    clock[0] = datetime(2026, 11, 18, 9, tzinfo=UTC)
    action = CorporateAction(
        id="historical-rights",
        listing_id=listings[0].id,
        kind="cash_entitlement",
        economic_at=clock[0],
        confirmed_at=clock[0],
        source_ref="official:historical-rights",
        verified=True,
        amount="10",
        entitlement_ref="nov10-register",
    )
    accrued = await service.corporate_action(
        experiment.id,
        participant.id,
        action,
        expected_revision=account.revision,
        idempotency_key="historical-rights",
        entitlement_date=date(2026, 11, 10),
        cash_per_share=Decimal("0.1"),
    )
    assert accrued.state.receivables == Decimal(10)
    duplicate = CorporateAction.model_validate(
        action.model_dump() | {"id": "different-id-same-rights"}
    )
    with pytest.raises(SimulationError, match="already accrued"):
        await service.corporate_action(
            experiment.id,
            participant.id,
            duplicate,
            expected_revision=accrued.revision,
            idempotency_key="duplicate-rights",
            entitlement_date=date(2026, 11, 10),
            cash_per_share=Decimal("0.1"),
        )


@pytest.mark.parametrize("arena", ["memory"], indirect=True)
async def test_private_memory_uses_latest_version_available_at_frozen_cutoff(arena, db_session):
    from investment_agent.simulation.contracts import SimulationReview
    from investment_agent.simulation.risk import stable_id

    service, experiment, listings, clock = arena
    await settled_arena(arena, db_session)
    participant = next(
        p for p in await service.participants(experiment.id) if p.config.kind == "agent"
    )
    decision = next(
        d for d in await service.decisions(experiment.id, participant.id) if d.status == "locked"
    )
    clock[0] = datetime(2026, 11, 11, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 11))
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 11))
    nav = next(
        n
        for n in await service.value(experiment.id, date(2026, 11, 11))
        if n.participant_id == participant.id
    )
    old = SimulationReview(
        experiment_id=experiment.id,
        participant_id=participant.id,
        account_id=participant.account_id,
        id=stable_id("review", decision.id, 1),
        decision_id=decision.id,
        observation_start=date(2026, 11, 10),
        observation_end=date(2026, 11, 11),
        available_at=clock[0],
        status="matured",
        nav_refs=(nav.id,),
        findings="previously available",
        memory_eligible=True,
        version=1,
    )
    service._store_review(old)
    await db_session.flush()
    clock[0] = datetime(2026, 11, 16, 9, tzinfo=UTC)
    for listing in listings:
        await add_bar(db_session, listing.id, date(2026, 11, 16))
        await add_proof(service, experiment.id, listing.id, date(2026, 11, 16))
    round_ = await service.prepare_round(
        experiment.id,
        observation_date=date(2026, 11, 16),
        cutoff=clock[0],
        decision_deadline=datetime(2026, 11, 17, 0, tzinfo=UTC),
        idempotency_key="memory-next-week",
        require_financials=False,
    )
    clock[0] += timedelta(minutes=1)
    later = SimulationReview.model_validate(
        old.model_dump()
        | {
            "id": stable_id("review", decision.id, 2),
            "version": 2,
            "available_at": clock[0],
            "findings": "learned after current round cutoff",
        }
    )
    service._store_review(later)
    await db_session.flush()
    context = await service.private_context(experiment.id, round_.id, participant.id)
    assert context.matured_reviews == (old,)


@pytest.mark.parametrize("arena", ["huge-fee"], indirect=True)
async def test_equal_weight_with_unaffordable_fee_records_hold_instead_of_crashing(arena):
    service, experiment, _, _ = arena
    round_ = await prepare(arena)
    equal = next(
        p for p in await service.participants(experiment.id) if p.config.kind == "equal_weight"
    )
    decision = next(
        d for d in await service.decisions(experiment.id, equal.id) if d.round_id == round_.id
    )
    assert decision.action == "hold" and not decision.orders


@pytest.mark.parametrize("arena", ["reverse-universe"], indirect=True)
async def test_universe_order_does_not_break_frozen_manifest_validation(arena):
    service, experiment, _, _ = arena
    round_ = await prepare(arena)
    assert round_.manifest.universe == tuple(sorted(experiment.config.universe))
    assert (await service.get_round(experiment.id, round_.id)).status == "collecting_decisions"


async def test_verified_suspended_zero_volume_has_zero_capacity_without_blocking_pool(
    arena, db_session
):
    service, experiment, listings, _ = arena
    original = await db_session.scalar(
        select(MarketSnapshot).where(MarketSnapshot.listing_id == listings[1].id)
    )
    db_session.add(
        MarketSnapshot(
            listing_id=original.listing_id,
            trade_date=DAY,
            open=original.open,
            high=original.high,
            low=original.low,
            close=original.close,
            volume=Decimal(0),
            amount=None,
            trade_status=0,
            is_st=False,
            adjust="none",
            provider="fixture",
            version=2,
            captured_at=NOW,
            raw_ref="archive:suspended",
            is_backfill=False,
        )
    )
    await db_session.flush()
    round_ = await prepare(arena)
    assert round_.status == "collecting_decisions" and round_.manifest.complete
    _, _, metadata = await service._round(experiment.id, round_.id)
    assert Decimal(metadata["reservation_terms"][str(listings[1].id)]["historical_turnover"]) == 0
    equal = next(
        p for p in await service.participants(experiment.id) if p.config.kind == "equal_weight"
    )
    equal_decision = next(
        d for d in await service.decisions(experiment.id, equal.id) if d.round_id == round_.id
    )
    assert {o.listing_id for o in equal_decision.orders} == {listings[0].id}
    participant, decision = await agent_decision(
        arena,
        round_,
        orders=(
            OrderIntent(
                listing_id=listings[1].id, side="buy", quantity="100", protection_price="10"
            ),
        ),
    )
    with pytest.raises(SimulationError, match="capacity"):
        await service.submit_decision(experiment.id, round_.id, participant.id, decision)

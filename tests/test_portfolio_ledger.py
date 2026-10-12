"""S12 user facts, immutable revisions and file SQLite concurrency."""
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select

from investment_agent.api.app import create_app
from investment_agent.api.auth import get_settings
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio import service as portfolio_service
from investment_agent.portfolio.contracts import (
    AppendEvent,
    Balance,
    CashFact,
    CorporateAction,
    CreateAccount,
    Reversal,
    Trade,
)
from investment_agent.portfolio.service import PortfolioService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.portfolio import PortfolioEvent, PortfolioSnapshot
from investment_agent.storage.models.reference import Listing
from tests.support.portfolio import (  # noqa: F401
    D,
    T,
    append,
    balance,
    trade,
)
from tests.support.portfolio import (
    ledger as ledger,
)


@pytest.mark.asyncio
async def test_unknown_fee_partial_and_old_snapshot(ledger):
    account, ref = ledger
    first = await append(account, balance(ref, coverage="partial"), 0)
    second = await append(account, trade(ref, fee=None), 1)
    assert second.snapshot.known_cash is None
    assert second.snapshot.cash_subtotal == D(970)
    assert second.snapshot.holdings[0].confirmed_quantity == D(13)
    assert second.snapshot.total_assets is None and second.snapshot.holdings[0].weight is None
    assert "unlisted_assets_unknown" in second.snapshot.unknowns
    assert any(u.startswith("unknown_fee:") for u in second.snapshot.unknowns)
    async with session_scope() as session:
        old = await PortfolioService(session).snapshot(account, snapshot_id=first.snapshot.id)
        assert old == first.snapshot
        assert old.known_cash == D(1000)


@pytest.mark.asyncio
async def test_import_replay_exact_manual_keys_and_similar_distinct_facts(ledger):
    account, ref = ledger
    await append(account, balance(ref), 0)
    batch = uuid4()
    original = await append(account, trade(ref), 1, batch_id=batch, row_id="row-1")
    replay = await append(account, trade(ref), 0, batch_id=batch, row_id="row-1")
    assert replay == original
    manual = original.event.request
    async with session_scope() as session:
        assert await PortfolioService(session).append(manual) == original
    with pytest.raises(InvestorError, match="another body"):
        await append(account, trade(ref, quantity="4"), 1, idempotency_key=manual.idempotency_key)
    similar = await append(account, trade(ref), 2)
    assert similar.snapshot.holdings[0].confirmed_quantity == D(16)
    assert similar.event.id != original.event.id


@pytest.mark.asyncio
async def test_same_timestamp_backfill_and_reversal_rebuild_old_versions(ledger):
    account, ref = ledger
    opening = await append(account, balance(ref), 0)
    same = await append(account, trade(ref, occurred_at=T), 1)
    assert same.snapshot.holdings[0].confirmed_quantity == D(13)
    later = await append(account, trade(ref, occurred_at=T + timedelta(days=3)), 2)
    backfill = await append(account, trade(ref, occurred_at=T + timedelta(days=2), quantity="2"), 3)
    assert backfill.snapshot.known_cash == D(917)
    preopening = await append(account, trade(ref, occurred_at=T - timedelta(days=1)), 4)
    assert preopening.snapshot.known_cash == D(917)
    assert preopening.event.id in preopening.snapshot.excluded_prior_event_ids
    reversed_result = await append(account, Reversal(kind="reversal", occurred_at=T + timedelta(days=4), source="user correction", user_confirmed=True, original_event_id=backfill.event.id, reason="incorrect quantity; replacement follows"), 5)
    assert reversed_result.snapshot.known_cash == D(938)
    with pytest.raises(InvestorError, match="already reversed"):
        await append(account, Reversal(kind="reversal", occurred_at=T + timedelta(days=4), source="user", user_confirmed=True, original_event_id=backfill.event.id, reason="again"), 6)
    async with session_scope() as session:
        assert await PortfolioService(session).snapshot(account, revision=3) == later.snapshot
        assert await PortfolioService(session).snapshot(account, revision=1) == opening.snapshot


@pytest.mark.asyncio
async def test_opening_reversal_replacement_never_guesses_zero(ledger):
    account, ref = ledger
    opening = await append(account, balance(ref), 0)
    await append(account, trade(ref), 1)
    reversed_result = await append(account, Reversal(kind="reversal", occurred_at=T + timedelta(days=2), source="user", user_confirmed=True, original_event_id=opening.event.id, reason="wrong baseline"), 2)
    assert "no_active_opening" in reversed_result.snapshot.unknowns
    assert reversed_result.snapshot.known_cash is None
    assert reversed_result.snapshot.holdings[0].confirmed_quantity is None
    corrected = await append(account, balance(ref, cash="2000"), 3)
    assert corrected.snapshot.known_cash == D(1969)
    assert corrected.snapshot.holdings[0].confirmed_quantity == D(13)


@pytest.mark.asyncio
async def test_reconciliation_only_future_baseline_and_historical_unknowns(ledger):
    account, ref = ledger
    await append(account, balance(ref), 0)
    unknown = await append(account, trade(ref, fee=None), 1)
    reconcile = await append(account, balance(ref, kind="reconciliation", occurred_at=T + timedelta(days=2), cash="960", quantity="13"), 2)
    assert reconcile.snapshot.known_cash == D(960)
    assert any(u.startswith("unknown_fee:") for u in reconcile.snapshot.historical_unknowns)
    assert reconcile.snapshot.total_assets == D(1090)
    assert reconcile.snapshot.conflicts
    earlier = await append(account, CashFact(kind="cash_transfer", occurred_at=T + timedelta(hours=12), source="found statement", user_confirmed=True, amount=D(100)), 3)
    assert earlier.snapshot.known_cash == D(960)
    later = await append(account, CashFact(kind="cash_transfer", occurred_at=T + timedelta(days=3), source="user", user_confirmed=True, amount=D(10)), 4)
    assert later.snapshot.known_cash == D(970)
    async with session_scope() as session:
        assert await PortfolioService(session).snapshot(account, snapshot_id=unknown.snapshot.id) == unknown.snapshot
        events = list(await session.scalars(select(PortfolioEvent)))
        assert len(events) == 5  # no fake difference/fee event


@pytest.mark.asyncio
async def test_company_actions_and_actual_violations_preserved(ledger):
    account, ref = ledger
    await append(account, balance(ref, cash="1"), 0)
    actual = await append(account, trade(ref, quantity="1.5"), 1)
    assert actual.snapshot.cash_subtotal == D(-15)
    assert any(c.startswith("negative_cash:") for c in actual.snapshot.conflicts)
    pending = await append(account, CorporateAction(kind="corporate_action", occurred_at=T + timedelta(days=2), source="user statement", user_confirmed=True, listing_ref=ref, action="pending", description="await rights issue confirmation"), 2)
    assert pending.snapshot.holdings[0].confirmed_quantity is None
    assert pending.snapshot.known_cash is None and pending.snapshot.total_assets is None
    await append(account, Reversal(kind="reversal", occurred_at=T + timedelta(days=2), source="user", user_confirmed=True, original_event_id=pending.event.id, reason="replace confirmed split"), 3)
    split = await append(account, CorporateAction(kind="corporate_action", occurred_at=T + timedelta(days=2), source="user confirmed", user_confirmed=True, listing_ref=ref, action="split", ratio=D(2), affects=("quantity",), description="two for one"), 4)
    assert split.snapshot.holdings[0].confirmed_quantity == D(23)
    # Plan 2.8: a sell beyond the confirmed basis is rejected at commit. A
    # recorded negative position stays reserved for unknown bases only; the
    # rejection must leave the account untouched.
    with pytest.raises(InvestorError, match="exceeds the confirmable quantity"):
        await append(account, trade(ref, direction="sell", quantity="25", occurred_at=T + timedelta(days=3)), 5)
    sold = await append(account, trade(ref, direction="sell", quantity="23", occurred_at=T + timedelta(days=3)), 5)
    assert sold.event.request.fact.holding.quantity == D(23)
    assert sold.snapshot.holdings[0].quantity_subtotal == D(0)
    assert sold.snapshot.holdings[0].confirmed_quantity == D(0)
    assert sold.snapshot.holdings[0].remaining_cost == D(0)


@pytest.mark.asyncio
async def test_owner_currency_listing_and_missing_price(ledger):
    account, ref = ledger
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="not found"):
            await PortfolioService(session, "other_owner").snapshot(account)
    with pytest.raises(ValidationError):
        Balance.model_validate({**balance(ref).model_dump(), "currency": "USD"})
    with pytest.raises(ValidationError):
        Trade.model_validate({**trade(ref).model_dump(), "actual_price": 1.5})
    invalid = ObjectRef(kind="listing", id=999999, revision=1)
    with pytest.raises(InvestorError, match="Listing unavailable"):
        await append(account, balance(invalid), 0)
    async with session_scope() as session:
        await session.execute(delete(MarketSnapshot))
    with pytest.raises(InvestorError, match="already occurred"):
        await append(account, balance(ref, occurred_at=datetime.now(UTC) + timedelta(days=1)), 0)
    first = await append(account, balance(ref, occurred_at=T - timedelta(days=1)), 0)
    assert first.snapshot.price_coverage == "partial" and first.snapshot.total_assets is None
    assert first.snapshot.known_subtotal == D(1000)


@pytest.mark.asyncio
async def test_snapshot_price_window_uses_the_shanghai_date(ledger, monkeypatch):
    """Recorded 00:30 Beijing (16:30 UTC) must already see that Beijing day's close."""
    account, ref = ledger
    frozen = T + timedelta(hours=16, minutes=30)  # UTC 16:30 = Beijing next-day 00:30

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(portfolio_service, "datetime", FrozenClock)
    async with session_scope() as session:
        session.add(MarketSnapshot(listing_id=int(ref.id), trade_date=T.date() + timedelta(days=1), open=D(20), high=D(20), low=D(20), close=D(20), volume=D(1), adjust="none", provider="fixture", captured_at=frozen - timedelta(minutes=30), version=1))
    await append(account, balance(ref), 0)
    transfer = await append(account, CashFact(kind="cash_transfer", occurred_at=T + timedelta(hours=1), source="user", user_confirmed=True, amount=D(1)), 1)
    # The UTC date of `recorded` is still 2025-01-01; the Beijing day is 01-02.
    assert transfer.snapshot.holdings[0].price == D(20)
    assert transfer.snapshot.holdings[0].price_date == T.date() + timedelta(days=1)


@pytest.mark.asyncio
async def test_file_sqlite_concurrent_cas_and_lost_ack_replay(ledger):
    account, ref = ledger
    await append(account, balance(ref), 0)
    request = AppendEvent(account_id=account, expected_revision=1, idempotency_key=uuid4(), fact=trade(ref))
    async def write(body):
        async with session_scope() as session:
            return await PortfolioService(session).append(body)
    same = await asyncio.gather(write(request), write(request))
    assert same[0] == same[1]
    different = [request.model_copy(update={"idempotency_key": uuid4(), "expected_revision": 2}) for _ in range(2)]
    results = await asyncio.gather(*(write(r) for r in different), return_exceptions=True)
    assert sum(isinstance(r, InvestorError) for r in results) == 1
    async with session_scope() as session:
        assert len(list(await session.scalars(select(PortfolioEvent)))) == 3
        assert len(list(await session.scalars(select(PortfolioSnapshot)))) == 3


@pytest.mark.asyncio
async def test_authenticated_api_default_off_and_ack_commit(ledger, monkeypatch):
    account, ref = ledger
    monkeypatch.setenv("API_KEY", "portfolio-test-key")
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/portfolio/accounts")).status_code == 401
        headers = {"X-API-Key": "portfolio-test-key"}
        body = AppendEvent(account_id=account, expected_revision=0, idempotency_key=uuid4(), fact=balance(ref)).model_dump(mode="json")
        assert (await client.post("/portfolio/events", json=body, headers=headers)).status_code == 403
        async def enabled_settings():
            return Settings(api_key="portfolio-test-key", portfolio_ledger_enabled=True)
        app.dependency_overrides[get_settings] = enabled_settings
        response = await client.post("/portfolio/events", json=body, headers=headers)
        assert response.status_code == 200, response.text
        snapshot_id = response.json()["snapshot"]["id"]
        async with session_scope() as session:
            assert await session.get(PortfolioSnapshot, snapshot_id) is not None
        assert (await client.post("/portfolio/events", json=body, headers=headers)).json() == response.json()
        body["fact"]["cash"] = "2000"
        assert (await client.post("/portfolio/events", json=body, headers=headers)).status_code == 409
        read = await client.get("/portfolio/snapshot", params={"account_id": str(account), "snapshot_id": snapshot_id, "revision": 1}, headers=headers)
        assert read.status_code == 200 and read.json() == response.json()["snapshot"]
        body["actor"] = "user"
        assert (await client.post("/portfolio/events", json=body, headers=headers)).status_code == 422


@pytest.mark.asyncio
async def test_import_replay_binds_new_request_key_and_atomic_rollback(ledger):
    account, ref = ledger
    await append(account, balance(ref), 0)
    batch = uuid4()
    original = await append(account, trade(ref), 1, batch_id=batch, row_id="same")
    alias_key = uuid4()
    replay = await append(account, trade(ref), 0, batch_id=batch, row_id="same", idempotency_key=alias_key)
    assert replay == original
    with pytest.raises(InvestorError, match="another body"):
        await append(account, trade(ref), 2, idempotency_key=alias_key)
    request = AppendEvent(account_id=account, expected_revision=2, idempotency_key=uuid4(), fact=trade(ref))
    with pytest.raises(RuntimeError, match="lost transaction"):
        async with session_scope() as session:
            await PortfolioService(session).append(request)
            raise RuntimeError("lost transaction")
    async with session_scope() as session:
        assert (await PortfolioService(session).snapshot(account)).revision == 2
        assert len(list(await session.scalars(select(PortfolioEvent)))) == 2
    result = await append(account, request.fact, 2, idempotency_key=request.idempotency_key)
    assert result.snapshot.revision == 3


@pytest.mark.asyncio
async def test_concurrent_reversal_and_snapshot_prices_frozen(ledger):
    account, ref = ledger
    initial = await append(account, balance(ref), 0)
    execution = await append(account, trade(ref, fee=None), 1)
    fact = Reversal(kind="reversal", occurred_at=T + timedelta(days=2), source="user correction", user_confirmed=True, original_event_id=execution.event.id, reason="wrong execution")
    results = await asyncio.gather(append(account, fact, 2), append(account, fact, 2), return_exceptions=True)
    assert sum(isinstance(r, InvestorError) for r in results) == 1
    async with session_scope() as session:
        session.add(MarketSnapshot(listing_id=int(ref.id), trade_date=T.date() + timedelta(days=10), open=D(20), high=D(20), low=D(20), close=D(20), volume=D(1), adjust="none", provider="fixture", captured_at=T + timedelta(days=10), version=1))
    newer = await append(account, CashFact(kind="cash_transfer", occurred_at=T + timedelta(days=3), source="user", user_confirmed=True, amount=D(1)), 3)
    assert newer.snapshot.fact_as_of == T + timedelta(days=3)
    assert newer.snapshot.valuation_at > newer.snapshot.fact_as_of
    assert newer.snapshot.holdings[0].price == D(20)
    assert newer.snapshot.holdings[0].price_date == T.date() + timedelta(days=10)
    async with session_scope() as session:
        assert await PortfolioService(session).snapshot(account, snapshot_id=initial.snapshot.id) == initial.snapshot
        assert initial.snapshot.holdings[0].price == D(10)


@pytest.mark.asyncio
async def test_reconciliation_computed_difference_and_unknown_history(ledger):
    account, ref = ledger
    await append(account, balance(ref, cash="100", quantity="10"), 0)
    stated_none = balance(ref, kind="reconciliation", cash="150", quantity="12", occurred_at=T + timedelta(days=1)).model_copy(update={"unexplained_difference": "none"})
    changed = await append(account, stated_none, 1)
    comparison = changed.snapshot.reconciliations[0]
    assert (comparison.before_cash, comparison.confirmed_cash, comparison.cash_delta) == (D(100), D(150), D(50))
    assert comparison.holdings[0].delta == D(2)
    assert changed.snapshot.conflicts and comparison.user_difference_explanation == "none"
    same = await append(account, stated_none.model_copy(update={"occurred_at": T + timedelta(days=2)}), 2)
    assert same.snapshot.reconciliations[-1].cash_delta == D(0)
    assert len(same.snapshot.conflicts) == 1  # unchanged point isn't a new conflict
    async with session_scope() as session:
        partial = await PortfolioService(session).create_account(CreateAccount(idempotency_key=uuid4(), name="Unknown independent account"))
    await append(partial.id, balance(ref, coverage="partial", cash=None), 0)
    other_ref = ObjectRef(kind="listing", id=int(ref.id) + 1, revision=1)
    async with session_scope() as session:
        listing = await session.get(Listing, ref.id)
        session.add(Listing(id=int(other_ref.id), company_id=listing.company_id, market_code="SH", ticker="600001.SH"))
    await append(partial.id, trade(other_ref), 1)
    corrected = await append(partial.id, balance(ref, kind="reconciliation", occurred_at=T + timedelta(days=2), cash="200"), 2)
    assert corrected.snapshot.known_cash == D(200)
    assert any(u.startswith("unknown_cash:") for u in corrected.snapshot.historical_unknowns)
    assert f"unknown_quantity:{other_ref.id}" in corrected.snapshot.historical_unknowns
    assert "unlisted_assets_unknown" in corrected.snapshot.historical_unknowns
    comparison = corrected.snapshot.reconciliations[0]
    assert comparison.cash_delta is None and comparison.cash_unknown_reason is not None
    assert next(h for h in comparison.holdings if h.listing_ref == other_ref).delta is None
    async with session_scope() as session:
        assert (await PortfolioService(session).snapshot(account)).known_cash == D(150)


@pytest.mark.asyncio
async def test_confirmed_zero_quantity_needs_no_price_unknown_is_not_zero(ledger):
    account, ref = ledger
    async with session_scope() as session:
        await session.execute(delete(MarketSnapshot))
    opening = await append(account, balance(ref, quantity="0"), 0)
    assert opening.snapshot.total_assets == D(1000)
    assert opening.snapshot.holdings[0].market_value == D(0)
    assert opening.snapshot.price_coverage == "complete"
    pending = await append(account, CorporateAction(kind="corporate_action", occurred_at=T + timedelta(days=1), source="user", user_confirmed=True, listing_ref=ref, action="pending", affects=("quantity",), description="await quantity confirmation"), 1)
    assert pending.snapshot.holdings[0].confirmed_quantity is None
    assert pending.snapshot.holdings[0].market_value is None
    assert pending.snapshot.total_assets is None


@pytest.mark.asyncio
async def test_event_audit_api_stable_pagination_identity_owner_and_default_off(ledger, monkeypatch):
    account, ref = ledger
    opening = await append(account, balance(ref), 0)
    execution = await append(account, trade(ref, fee=None), 1)
    correction = await append(account, Reversal(kind="reversal", occurred_at=T + timedelta(days=2), source="user", user_confirmed=True, original_event_id=execution.event.id, reason="correct later"), 2)
    async with session_scope() as session:
        other = await PortfolioService(session).create_account(CreateAccount(idempotency_key=uuid4(), name="other"))
        foreign = await PortfolioService(session, "foreign").create_account(CreateAccount(idempotency_key=uuid4(), name="private"))
    monkeypatch.setenv("API_KEY", "portfolio-audit-key")
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-API-Key": "portfolio-audit-key"}
        params = {"account_id": str(account), "limit": 1}
        assert (await client.get("/portfolio/events", params=params)).status_code == 401
        page = await client.get("/portfolio/events", params=params, headers=headers)
        assert page.status_code == 200
        assert page.json()["items"] == [opening.event.model_dump(mode="json")]
        assert page.json()["next_after_revision"] == 1
        params["after_revision"] = 1
        params["limit"] = 2
        rest = (await client.get("/portfolio/events", params=params, headers=headers)).json()
        assert [e["revision"] for e in rest["items"]] == [2, 3]
        assert rest["next_after_revision"] is None and rest["observed_revision"] == 3
        assert rest["items"][0]["request"]["fact"]["fee"] is None
        assert rest["items"][1]["request"]["fact"]["original_event_id"] == str(execution.event.id)
        exact = await client.get(f"/portfolio/events/{execution.event.id}", params={"account_id": str(account)}, headers=headers)
        assert exact.json() == execution.event.model_dump(mode="json")  # reversed original still immutable/readable
        assert correction.event.id in correction.snapshot.source_event_ids
        for scoped_account in [other.id, foreign.id, uuid4()]:
            assert (await client.get(f"/portfolio/events/{execution.event.id}", params={"account_id": str(scoped_account)}, headers=headers)).status_code == 404
        assert (await client.get("/portfolio/events", params={"account_id": str(foreign.id)}, headers=headers)).status_code == 404
        assert (await client.get(f"/portfolio/events/{uuid4()}", params={"account_id": str(account)}, headers=headers)).status_code == 404
        assert (await client.get("/portfolio/events/1", params={"account_id": str(account)}, headers=headers)).status_code == 422
        for invalid in [{"after_revision": -1}, {"limit": 201}, {"limit": 0}]:
            assert (await client.get("/portfolio/events", params={"account_id": str(account), **invalid}, headers=headers)).status_code == 422

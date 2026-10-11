"""Whole-batch import with a server-side preview (watch-holding-plan 2.8 / WP4).

The batch is one frozen request (operation UUID + base revision + exact rows):
the preview names each row's inclusion and effect without writing, a conflicting
batch stops at the preview (and the commit replays whole), a cleared account's
historical "buy 100 / sell 100" segment passes as one time-ordered replay, and a
lost receipt is retried with the same identity.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.contracts import (
    AppendEvent,
    Holding,
    ImportBatch,
    ImportRow,
    Reversal,
    Trade,
)
from investment_agent.portfolio.service import PortfolioService
from investment_agent.storage.database import session_scope
from tests.test_portfolio_ledger import append, balance, ledger as ledger_fixture, trade

ledger = ledger_fixture
D = Decimal


def _trade(ref, *, occurred_at, direction="buy", quantity="100", price="10", fee="0"):
    return Trade(
        kind="trade",
        occurred_at=occurred_at,
        source="broker statement import",
        user_confirmed=True,
        holding=Holding(listing_ref=ref, quantity=D(quantity)),
        direction=direction,
        actual_price=D(price),
        fee=D(fee),
    )


def _batch(account, revision, rows, *, operation_id=None, source="broker statement"):
    return ImportBatch(
        account_id=account,
        expected_revision=revision,
        operation_id=operation_id or uuid4(),
        source=source,
        rows=tuple(ImportRow(row_id=row_id, fact=fact) for row_id, fact in rows),
    )


@pytest.mark.asyncio
async def test_cleared_account_imports_buy_and_sell_as_one_replay(ledger):
    account, ref = ledger
    # A confirmed current balance of zero (the account was cleared).
    await append(account, balance(ref, quantity="0", cash="1000"), 0)
    base = datetime(2025, 1, 10, tzinfo=UTC)
    request = _batch(
        account,
        1,
        [
            ("buy", _trade(ref, occurred_at=base, direction="buy", quantity="100")),
            ("sell", _trade(ref, occurred_at=base + timedelta(hours=1), direction="sell", quantity="100", price="11")),
        ],
    )
    async with session_scope() as session:
        service = PortfolioService(session)
        preview = await service.preview_import(request)
    assert preview.ready, preview.conflicts
    assert preview.resulting_revision == 3
    assert [row.inclusion for row in preview.rows] == ["applied", "applied"]
    assert preview.rows[0].quantity_delta == {str(ref.id): D(100)}
    assert preview.rows[1].quantity_delta == {str(ref.id): D(-100)}
    assert preview.rows[1].cash_delta == D(1100)

    async with session_scope() as session:
        result = await PortfolioService(session).apply_import(request)
    assert result.snapshot.revision == 3
    assert [event.request.row_id for event in result.events] == ["buy", "sell"]
    held = next(h for h in result.snapshot.holdings if h.listing_ref == ref)
    assert held.confirmed_quantity == D(0)  # the whole replay ends flat
    assert result.audit_only_row_ids == ()


@pytest.mark.asyncio
async def test_preview_marks_prior_history_rows_audit_only(ledger):
    account, ref = ledger
    # The confirmed balance is *after* the imported segment: history only.
    await append(account, balance(ref, occurred_at=datetime(2025, 2, 1, tzinfo=UTC), quantity="100", cash="1000"), 0)
    request = _batch(
        account,
        1,
        [("old-buy", _trade(ref, occurred_at=datetime(2025, 1, 1, tzinfo=UTC), direction="buy", quantity="50"))],
    )
    async with session_scope() as session:
        preview = await PortfolioService(session).preview_import(request)
    assert preview.ready
    assert preview.rows[0].inclusion == "audit_prior_history"
    assert preview.rows[0].quantity_delta == {} and preview.rows[0].cash_delta is None

    async with session_scope() as session:
        result = await PortfolioService(session).apply_import(request)
    assert result.audit_only_row_ids == ("old-buy",)
    held = next(h for h in result.snapshot.holdings if h.listing_ref == ref)
    assert held.confirmed_quantity == D(100)  # unchanged by the audit row
    assert held.quantity_subtotal == D(100)


@pytest.mark.asyncio
async def test_conflicting_batch_stops_at_preview_and_commit_rejects_whole(ledger):
    account, ref = ledger
    await append(account, balance(ref, quantity="100", cash="1000"), 0)
    base = datetime(2025, 1, 10, tzinfo=UTC)
    request = _batch(
        account,
        1,
        [
            ("buy", _trade(ref, occurred_at=base, direction="buy", quantity="10")),
            ("too-much", _trade(ref, occurred_at=base + timedelta(hours=1), direction="sell", quantity="1000")),
        ],
    )
    async with session_scope() as session:
        preview = await PortfolioService(session).preview_import(request)
    assert not preview.ready
    rejected = {row.row_id for row in preview.rows if not row.accepted}
    assert rejected == {"too-much"}
    assert any("sell_exceeds_confirmable_quantity" in conflict for conflict in preview.conflicts)

    async with session_scope() as session:
        service = PortfolioService(session)
        with pytest.raises(InvestorError, match="would not replay validly"):
            await service.apply_import(request)
    async with session_scope() as session:
        snapshot = await PortfolioService(session).snapshot(account)
    assert snapshot.revision == 1  # nothing half-written
    assert snapshot.holdings[0].confirmed_quantity == D(100)


@pytest.mark.asyncio
async def test_stale_base_revision_is_refused_and_a_lost_receipt_replays(ledger):
    account, ref = ledger
    await append(account, balance(ref, quantity="0", cash="1000"), 0)
    request = _batch(
        account,
        1,
        [("buy", _trade(ref, occurred_at=datetime(2025, 1, 10, tzinfo=UTC), direction="buy", quantity="10"))],
    )
    # Facts move on: the preview (and the commit) refuse the old base revision.
    await append(account, trade(ref, quantity="1"), 1)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="预览基准事实版本已变化"):
            await PortfolioService(session).preview_import(request)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="Reload current account revision"):
            await PortfolioService(session).apply_import(request)

    # A fresh preview on the current revision commits; the identical retry
    # (lost receipt) returns the original events instead of importing twice.
    fresh = request.model_copy(update={"expected_revision": 2})
    async with session_scope() as session:
        first = await PortfolioService(session).apply_import(fresh)
    async with session_scope() as session:
        again = await PortfolioService(session).apply_import(fresh)
    assert again.snapshot.revision == first.snapshot.revision == 3
    assert [event.id for event in again.events] == [event.id for event in first.events]
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="Import key reused with another body"):
            await PortfolioService(session).apply_import(fresh.model_copy(update={"source": "another body"}))
    async with session_scope() as session:
        snapshot = await PortfolioService(session).snapshot(account)
    assert snapshot.revision == 3


def test_reversals_belong_to_corrections_not_imports():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="correction path"):
        ImportRow(
            row_id="r1",
            fact=Reversal(
                kind="reversal",
                occurred_at=datetime(2025, 1, 10, tzinfo=UTC),
                source="statement",
                user_confirmed=True,
                original_event_id=uuid4(),
                reason="wrong line",
            ),
        )


@pytest.mark.asyncio
async def test_import_endpoints_preview_then_commit_with_receipt_replay(api_client, monkeypatch):
    from investment_agent.storage.models.pit_snapshot import IndustryClassification
    from investment_agent.storage.models.reference import Company, Listing, Market

    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "true")
    key = {"X-API-Key": "secret"}
    assert api_client.post("/portfolio/imports/preview", json={}).status_code == 401
    async with session_scope() as session:
        session.add(Market(code="SH", name="Fixture", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="Fixture")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SH", ticker="600010.SH")
        session.add(listing)
        await session.flush()
        session.add(IndustryClassification(listing_id=listing.id, system="CSRC", code="fixture", name="Fixture", valid_from=datetime(2025, 1, 1).date(), source="fixture", captured_at=datetime(2025, 1, 1, tzinfo=UTC)))
        listing_id = listing.id
    account = api_client.post(
        "/portfolio/accounts", headers=key, json={"idempotency_key": str(uuid4()), "name": "Import"}
    ).json()["id"]
    ref = {"kind": "listing", "id": listing_id, "revision": 1}
    opening = {
        "occurred_at": "2025-01-01T00:00:00+08:00",
        "source": "user statement",
        "user_confirmed": True,
        "kind": "opening_balance",
        "coverage": "complete",
        "asset_scope": "explicit account",
        "holdings": [{"listing_ref": ref, "quantity": "0", "average_cost": "0"}],
        "cash": "1000",
        "prior_history": "included_in_confirmed_balance",
    }
    assert (
        api_client.post(
            "/portfolio/events",
            headers=key,
            json={"account_id": account, "expected_revision": 0, "idempotency_key": str(uuid4()), "fact": opening},
        ).status_code
        == 200
    )
    batch = {
        "account_id": account,
        "expected_revision": 1,
        "operation_id": str(uuid4()),
        "source": "broker statement csv",
        "rows": [
            {
                "row_id": "row-1",
                "fact": {
                    "occurred_at": "2025-01-10T00:00:00+08:00",
                    "source": "broker statement csv",
                    "user_confirmed": True,
                    "kind": "trade",
                    "holding": {"listing_ref": ref, "quantity": "50"},
                    "direction": "buy",
                    "actual_price": "10",
                    "fee": "1",
                },
            }
        ],
    }
    preview = api_client.post("/portfolio/imports/preview", headers=key, json=batch)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["ready"], body["conflicts"]
    assert body["rows"][0]["inclusion"] == "applied"
    assert body["rows"][0]["quantity_delta"] == {str(listing_id): "50"}
    assert body["conflicts"] == []

    committed = api_client.post("/portfolio/imports", headers=key, json=batch)
    assert committed.status_code == 200, committed.text
    assert committed.json()["snapshot"]["revision"] == 2
    replay = api_client.post("/portfolio/imports", headers=key, json=batch)
    assert replay.status_code == 200
    assert replay.json()["snapshot"]["revision"] == 2  # receipt replay, no second batch
    assert [event["id"] for event in replay.json()["events"]] == [
        event["id"] for event in committed.json()["events"]
    ]
    stale = api_client.post("/portfolio/imports", headers=key, json={**batch, "expected_revision": 0})
    assert stale.status_code == 409, stale.text


@pytest.mark.asyncio
async def test_correction_writes_only_final_state_and_split_keeps_total_cost(ledger):
    """WP5: one correction writes only the final state; a split keeps total cost."""

    from investment_agent.portfolio.contracts import (
        ApplyCorrection,
        Balance,
        CorporateAction,
    )

    account, ref = ledger
    opening = Balance(
        kind="opening_balance",
        occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        source="user statement",
        user_confirmed=True,
        coverage="complete",
        asset_scope="explicit account",
        holdings=(Holding(listing_ref=ref, quantity=D(100), average_cost=D(20), opened_on=datetime(2025, 1, 1).date()),),
        cash=D(1000),
        prior_history="included_in_confirmed_balance",
    )
    await append(account, opening, 0)
    buy = await append(account, trade(ref, quantity="20", fee="2"), 1)
    sold = await append(account, trade(ref, quantity="10", direction="sell", fee="1"), 2)

    replacement = Trade(
        kind="trade",
        occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        source="corrected statement",
        user_confirmed=True,
        holding=Holding(listing_ref=ref, quantity=D(15)),
        direction="buy",
        actual_price=D(10),
        fee=D(1),
    )
    async with session_scope() as session:
        result = await PortfolioService(session).apply_correction(
            ApplyCorrection(
                account_id=account,
                expected_revision=3,
                operation_id=uuid4(),
                reason="两笔都录错了，合并为正确的一笔",
                reversals=(buy.event.id, sold.event.id),
                replacements=(replacement,),
            )
        )
    assert result.snapshot.revision == 6  # two reversals + one replacement, one transaction
    held = next(h for h in result.snapshot.holdings if h.listing_ref == ref)
    assert held.confirmed_quantity == D(115)
    assert held.remaining_cost == D(100) * D(20) + D(15) * D(10) + D(1)
    assert held.opened_on == datetime(2025, 1, 1).date()
    # The account head exposes only the final state; the originals stay history.
    async with session_scope() as session:
        assert (await PortfolioService(session).snapshot(account)).revision == 6

    # A confirmed split scales the quantity, keeps the total cost and the cycle.
    before = held
    async with session_scope() as session:
        await PortfolioService(session).append(
            AppendEvent(
                account_id=account,
                expected_revision=6,
                idempotency_key=uuid4(),
                fact=CorporateAction(
                    kind="corporate_action",
                    occurred_at=datetime(2025, 1, 2, tzinfo=UTC),
                    source="user statement",
                    user_confirmed=True,
                    listing_ref=ref,
                    action="split",
                    ratio=D(2),
                    affects=("quantity",),
                    description="用户确认的比例变更 2:1",
                ),
            )
        )
    async with session_scope() as session:
        after = await PortfolioService(session).snapshot(account)
    scaled = next(h for h in after.holdings if h.listing_ref == ref)
    assert scaled.confirmed_quantity == before.confirmed_quantity * 2
    assert scaled.remaining_cost == before.remaining_cost  # total cost unchanged
    # The average reverses (exact division down to boundary precision).
    assert (scaled.average_cost * 2).quantize(D("0.000001")) == before.average_cost.quantize(D("0.000001"))
    assert scaled.opened_on == before.opened_on  # the cycle does not reset

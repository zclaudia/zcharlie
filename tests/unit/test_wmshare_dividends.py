from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from investment_agent.dividends.service import dividend_context, persist_wmshare_capture
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.view_db import load_dividend_history, load_snapshot_view
from investment_agent.providers.wmshare_dividends import (
    WmshareCapture,
    WmshareDividendClient,
    parse_history,
)
from investment_agent.storage.repositories.dividend import DividendRepository
from tests.support.factor_view import AS_OF, ON_TIME, TICKER, _seed


def capture(rows):
    result = WmshareCapture(TICKER, AS_OF, ON_TIME)
    parse_history(result, rows)
    return result


def test_history_uses_real_ex_div_and_never_certifies_empty_or_ambiguous():
    assert not capture([]).covered_years
    rows = [
        {"ex_div": None, "ex_date": "2026-05-01", "dps": 1},
        {"ex_div": "2026-04-01", "dps": 0},
    ]
    result = capture(rows)
    assert result.events == [] and result.covered_years == []
    assert len(result.pending) == 1
    rows = [{"ex_div": "2026-04-01", "dps": 1}, {"ex_div": "2026-04-01", "dps": 2}]
    assert capture(rows).covered_years == []
    assert capture(rows).events == []
    rows = [{"ex_div": "2022-01-01", "dps": None}, {"ex_div": "2026-04-01", "dps": 1}]
    assert capture(rows).covered_years == [2025, 2026]


@pytest.mark.asyncio
async def test_client_partial_error_and_secret_redaction():
    class QuotaError(Exception):
        pass

    called = []

    def bad(*args, **kwargs):
        called.append("history")
        raise QuotaError("secret-value")

    client = SimpleNamespace(
        client_secret="secret-value",
        last_ec_cost=None,
        dividend=SimpleNamespace(history=bad, snapshot=lambda **kw: called.append("snapshot")),
    )
    result = await WmshareDividendClient(client).fetch(TICKER, AS_OF)
    assert result.terminal and "secret-value" not in result.history_error
    assert called == ["history"]


@pytest.mark.asyncio
async def test_provider_isolation_and_combined_cash_actions(db_session):
    listing = await _seed(db_session)
    repo = DividendRepository(db_session)
    # A cash-only plan with blank reserve follows the existing baostock parser.
    for ex, stock, reserve, description in [
        (date(2026, 1, 1), Decimal(0), None, "10派1元"),
        (date(2026, 3, 1), Decimal("0.5"), Decimal(0), "10送5股"),
    ]:
        await repo.upsert_event(
            listing_id=listing,
            ex_date=ex,
            available_from=ex,
            operate_year=2026,
            provider="baostock",
            captured_at=ON_TIME,
            payload={
                "cash_per_share_pre_tax": Decimal(3),
                "stock_per_share": stock,
                "reserve_per_share": reserve,
            },
            details={"dividCashStock": description},
        )
    for year in (2025, 2026):
        await repo.record_coverage(
            listing_id=listing, provider="baostock", year=year, checked_at=ON_TIME, event_count=1
        )
    wm = capture(
        [
            {"ex_div": "2026-01-01", "announce": "2025-12-01", "dps": 1},
            {"ex_div": "2026-03-01", "announce": "2026-02-01", "dps": 0.5},
        ]
    )
    wm.snapshot = {"quote_date": AS_OF.isoformat(), "yield_ttm": 0.1, "payout_ttm": 0.5}
    await persist_wmshare_capture(db_session, listing, wm)
    hist = await load_dividend_history(db_session, listing, AS_OF)
    assert hist.provider == "wmshare"
    assert len(hist.events) == 2
    assert hist.events[1].cash_per_share == 0.5 and hist.events[1].share_multiplier == 1.5
    assert hist.events[1].action_ref.startswith("dividend_event:")
    view = await load_snapshot_view(db_session, TICKER, AS_OF)
    factor = DEFAULT_REGISTRY.get("dividend_yield_ttm").compute(view)
    assert factor.value == pytest.approx(round((1 / 1.5 + 0.5) / 1504, 8))
    assert factor.inputs_ref["dividend_provider"] == "wmshare"
    result = await dividend_context(db_session, TICKER, AS_OF)
    assert result["cash"]["provider"] == "wmshare"
    assert result["platform"]["yield_ttm"] == 0.1
    assert result["difference"]["label"] == "口径差异"
    # An incomplete refresh cannot replace even part of the last complete set.
    partial = capture(
        [
            {"ex_div": "2026-01-01", "dps": 99},
            {"ex_div": "2026-03-01", "dps": None},
        ]
    )
    assert partial.history_error and not partial.covered_years
    await persist_wmshare_capture(db_session, listing, partial)
    after = await load_dividend_history(db_session, listing, AS_OF)
    assert after.events[0].cash_per_share == 1
    moved = capture([{"ex_div": "2026-01-02", "dps": 1}, {"ex_div": "2026-03-01", "dps": 0.5}])
    await persist_wmshare_capture(db_session, listing, moved)
    assert "reconciliation" in moved.history_error
    assert [
        item.ex_date for item in (await load_dividend_history(db_session, listing, AS_OF)).events
    ] == [date(2026, 1, 1), date(2026, 3, 1)]
    # History can refresh pending items even when platform collection fails.
    history_only = capture(
        [
            {"ex_div": "2026-01-01", "dps": 1},
            {"ex_div": "2026-03-01", "dps": 0.5},
            {"ex_div": None, "announce": "2026-04-01", "dps": 0.25},
        ]
    )
    await persist_wmshare_capture(db_session, listing, history_only)
    result = await dividend_context(db_session, TICKER, AS_OF)
    assert result["platform"]["yield_ttm"] == 0.1
    assert result["pending"][0]["dps"] == 0.25
    # A later successful snapshot with failed history preserves pending.
    snapshot_only = capture([])
    snapshot_only.snapshot = {"quote_date": AS_OF.isoformat(), "yield_ttm": 0.2}
    await persist_wmshare_capture(db_session, listing, snapshot_only)
    result = await dividend_context(db_session, TICKER, AS_OF)
    assert result["platform"]["yield_ttm"] == 0.2
    assert result["pending"][0]["dps"] == 0.25
    # Same date/version from both providers persists independently.
    assert (await repo.latest_events(listing, [date(2026, 1, 1)], provider="baostock"))[
        date(2026, 1, 1)
    ].version == 1
    assert (await repo.latest_events(listing, [date(2026, 1, 1)], provider="wmshare"))[
        date(2026, 1, 1)
    ].version == 2  # The history-only refresh omitted the earlier announcement.
    # Before the capture, neither provider's new coverage is visible.
    assert (
        await load_dividend_history(db_session, listing, date(2026, 5, 5))
    ).provider == "baostock"


def test_recorded_bank_history_keeps_undated_payment_pending():
    import json
    from pathlib import Path

    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/wmshare/dividends-real.json").read_text()
    )
    history = data["stocks"]["601988.SH"]["history_recent"]["result"]
    result = WmshareCapture("601988.SH", date(2026, 11, 13), ON_TIME)
    parse_history(result, history)
    assert [item.cash_per_share for item in result.events[-2:]] == [
        Decimal("0.1094"),
        Decimal("0.1169"),
    ]
    assert result.pending[0]["dps"] == 0.119
    assert result.pending[0]["ex_div"] is None


def test_migration_preserves_existing_event_version_and_isolates_provider(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    database = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.upgrade(config, "067")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO dividend_event "
            "(id, listing_id, ex_date, available_from, operate_year, details, provider, captured_at, is_backfill, version) "
            "VALUES (7, 1, '2026-01-01', '2025-12-01', 2026, '{}', 'baostock', '2026-01-01', 0, 3)"
        )
    command.upgrade(config, "068")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT id, version FROM dividend_event").fetchall() == [(7, 3)]
        conn.execute(
            "INSERT INTO dividend_event "
            "(id, listing_id, ex_date, available_from, operate_year, details, provider, captured_at, is_backfill, version) "
            "VALUES (8, 1, '2026-01-01', '2025-12-01', 2026, '{}', 'wmshare', '2026-01-01', 0, 3)"
        )
    with pytest.raises(RuntimeError, match="collide"):
        command.downgrade(config, "067")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT id, version FROM dividend_event ORDER BY id").fetchall() == [
            (7, 3),
            (8, 3),
        ]


@pytest.mark.asyncio
async def test_pending_only_is_visible_without_claiming_cash_coverage(db_session):
    listing = await _seed(db_session)
    pending_only = capture([{"ex_div": None, "announce": "2026-04-01", "dps": 0.25}])
    assert pending_only.pending_only and pending_only.history_error
    assert pending_only.covered_years == []
    await persist_wmshare_capture(db_session, listing, pending_only)
    result = await dividend_context(db_session, TICKER, AS_OF)
    assert result["pending"][0]["dps"] == 0.25
    assert result["cash"]["value"] is None
    assert result["platform"] is None

    # A failed, empty or malformed response cannot replace that pending list.
    for rows in ([], [{"ex_div": None, "dps": None}], [{"ex_div": None, "dps": 1}] * 500):
        bad = capture(rows)
        assert not bad.pending_only
        await persist_wmshare_capture(db_session, listing, bad)
    assert (await dividend_context(db_session, TICKER, AS_OF))["pending"][0]["dps"] == 0.25

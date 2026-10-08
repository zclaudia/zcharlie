"""API tests for GET /market/{ticker}/bars (sparkline source, plan 03 / D7)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from investment_agent.clock import shanghai_today
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository

H = {"X-API-Key": "secret"}
SEEDED_BARS = 150


def _weekdays_back(count: int):
    """``count`` weekdays ending yesterday or earlier, oldest first."""

    days = []
    cursor = shanghai_today() - timedelta(days=1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


async def _seed(ticker: str, base: int, *, provider: str = "baostock") -> int:
    captured = datetime.now(UTC) - timedelta(hours=1)
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing(ticker)
        repo = MarketSnapshotRepository(session)
        for index, trade_date in enumerate(_weekdays_back(SEEDED_BARS)):
            value = Decimal(base + index)
            await repo.insert(
                listing_id=listing.id,
                trade_date=trade_date,
                open=value,
                high=value,
                low=value,
                close=value,
                volume=Decimal(100),
                provider=provider,
                captured_at=captured,
            )
        return listing.id


def test_bars_require_auth(api_client):
    assert api_client.get("/market/600519.SH/bars").status_code == 401


def test_bars_unknown_listing_404(api_client):
    response = api_client.get("/market/600519.SH/bars", headers=H)
    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["600519", "600519.HK", "AAPL", "60051.SH"])
def test_bars_reject_non_a_share_tickers(api_client, bad: str):
    assert api_client.get(f"/market/{bad}/bars", headers=H).status_code == 422


@pytest.mark.parametrize("days", [0, 19, 401, -1])
def test_bars_days_is_bounded(api_client, days: int):
    assert api_client.get(f"/market/600519.SH/bars?days={days}", headers=H).status_code == 422


async def test_bars_default_window_and_order(api_client):
    await _seed("600519.SH", 1000)
    await _seed("000001.SZ", 10)

    response = api_client.get("/market/600519.sh/bars", headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "600519.SH"
    assert body["provider"] == "baostock,tencent"  # D2 precedence (plan 02)
    assert datetime.fromisoformat(body["as_of"]).tzinfo is not None
    bars = body["bars"]
    assert len(bars) == 120
    dates = [bar["trade_date"] for bar in bars]
    assert dates == sorted(dates)
    assert bars[-1]["trade_date"] == _weekdays_back(1)[0].isoformat()
    # The newest 120 of the seeded 150 closes, per listing.
    assert [bar["close"] for bar in bars] == [float(1000 + i) for i in range(30, 150)]

    other = api_client.get("/market/000001.SZ/bars?days=20", headers=H).json()
    assert [bar["close"] for bar in other["bars"]] == [float(10 + i) for i in range(130, 150)]

    everything = api_client.get("/market/600519.SH/bars?days=400", headers=H).json()
    assert len(everything["bars"]) == SEEDED_BARS


async def test_bars_filter_rows_captured_after_as_of_and_prefer_baostock(api_client):
    listing_id = await _seed("600519.SH", 1000)
    today = shanghai_today()
    yesterday = _weekdays_back(1)[0]

    async def insert(day, close, provider, captured):
        async with session_scope() as session:
            value = Decimal(close)
            await MarketSnapshotRepository(session).insert(
                listing_id=listing_id,
                trade_date=day,
                open=value,
                high=value,
                low=value,
                close=value,
                volume=Decimal(100),
                provider=provider,
                captured_at=captured,
            )

    # A live row whose capture lies in the future is not knowledge yet.
    await insert(today, 5, "baostock", datetime.now(UTC) + timedelta(days=1))
    # The Tencent close quote already stored for today fills the day (D2).
    await insert(today, 7, "tencent", datetime.now(UTC) - timedelta(minutes=5))
    # Yesterday has both sources: the baostock bar wins over the Tencent quote.
    await insert(yesterday, 9, "tencent", datetime.now(UTC) - timedelta(hours=2))

    body = api_client.get("/market/600519.SH/bars?days=20", headers=H).json()
    assert len(body["bars"]) == 20
    assert body["bars"][-1] == {"trade_date": today.isoformat(), "close": 7.0}
    assert body["bars"][-2]["trade_date"] == yesterday.isoformat()
    assert body["bars"][-2]["close"] == 1000 + SEEDED_BARS - 1  # baostock, not 9.0
    assert all(bar["close"] != 5.0 for bar in body["bars"])


async def test_bars_empty_when_no_snapshots(api_client):
    async with session_scope() as session:
        await ListingRepository(session).ensure_a_share_listing("300750.SZ")
    body = api_client.get("/market/300750.SZ/bars", headers=H).json()
    assert body == {
        "ticker": "300750.SZ",
        "provider": "baostock,tencent",
        "as_of": body["as_of"],
        "bars": [],
    }

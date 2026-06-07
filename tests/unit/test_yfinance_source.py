import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from investment_agent.sources.yfinance_source import YFinanceSource


_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "yfinance_history.json").read_text()
)


def _fake_download_factory(payload: dict):
    def _fake_download(tickers, start, end, **kwargs):
        rows = []
        for t in (tickers if isinstance(tickers, list) else [tickers]):
            for r in payload.get(t, []):
                rows.append({**r, "ticker": t})
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        return df
    return _fake_download


@pytest.mark.asyncio
async def test_fetch_daily_prices_parses_yfinance_dataframe(monkeypatch):
    import yfinance

    monkeypatch.setattr(
        yfinance, "download", _fake_download_factory(_FIXTURE)
    )

    src = YFinanceSource(max_concurrent=2)
    records = await src.fetch_daily_prices(
        tickers=["AAPL"], start=date(2025, 6, 2), end=date(2025, 6, 3)
    )
    assert len(records) == 2
    assert records[0].ticker == "AAPL"
    assert records[0].date == date(2025, 6, 2)
    assert float(records[0].close) == 197.4


@pytest.mark.asyncio
async def test_fetch_daily_prices_handles_empty_response(monkeypatch):
    import yfinance

    monkeypatch.setattr(yfinance, "download", _fake_download_factory({}))
    src = YFinanceSource(max_concurrent=2)
    out = await src.fetch_daily_prices(
        tickers=["GHOST"], start=date(2025, 6, 1), end=date(2025, 6, 2)
    )
    assert out == []


def _fake_ticker_info(payload: dict):
    class FakeTicker:
        def __init__(self, ticker):
            self.info = payload.get(ticker, {})
    return FakeTicker


@pytest.mark.asyncio
async def test_fetch_fundamental_snapshot_parses_info(monkeypatch):
    import yfinance

    info = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "yfinance_info.json").read_text()
    )
    monkeypatch.setattr(yfinance, "Ticker", _fake_ticker_info(info))

    src = YFinanceSource(max_concurrent=2)
    snap = await src.fetch_fundamental_snapshot("AAPL", as_of=date(2025, 6, 3))
    assert snap.ticker == "AAPL"
    assert float(snap.pe) == 32.5
    assert float(snap.pb) == 51.2
    assert float(snap.roe) == pytest.approx(1.5612, rel=1e-4)
    assert float(snap.market_cap) == 3_000_000_000_000


@pytest.mark.asyncio
async def test_fetch_fundamental_snapshot_handles_missing_fields(monkeypatch):
    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", _fake_ticker_info({"GHOST": {}}))
    src = YFinanceSource(max_concurrent=2)
    snap = await src.fetch_fundamental_snapshot("GHOST", as_of=date(2025, 6, 3))
    assert snap.pe is None
    assert snap.market_cap is None

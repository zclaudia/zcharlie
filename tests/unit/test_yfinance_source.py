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


@pytest.mark.asyncio
async def test_fetch_universe_returns_nasdaq_100(monkeypatch):
    """Mock yfinance.Tickers / index member fetch."""
    # NDX index components come from a local YAML or pandas read_html;
    # for unit test we monkeypatch _fetch_nasdaq_100_tickers.
    from investment_agent.sources import yfinance_source as ys
    monkeypatch.setattr(
        ys, "_fetch_nasdaq_100_tickers",
        lambda: [("AAPL", "Apple Inc.", "TECH"), ("MSFT", "Microsoft Corporation", "TECH")],
    )
    src = YFinanceSource(max_concurrent=2)
    refs = await src.fetch_universe("NASDAQ_100", as_of=date(2025, 6, 3))
    assert len(refs) == 2
    assert refs[0].market_code == "NASDAQ"
    assert refs[0].ticker == "AAPL"
    assert refs[0].company_name == "Apple Inc."


@pytest.mark.asyncio
async def test_fetch_universe_unknown_code_raises():
    src = YFinanceSource(max_concurrent=2)
    with pytest.raises(ValueError, match="Unsupported universe"):
        await src.fetch_universe("UNKNOWN_INDEX", as_of=date(2025, 6, 3))


@pytest.mark.asyncio
async def test_fetch_financial_report_parses_quarterly(monkeypatch):
    import yfinance
    import pandas as pd

    fixture = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "yfinance_financials.json").read_text()
    )

    class FakeTicker:
        def __init__(self, ticker):
            self._data = fixture[ticker]

        @property
        def quarterly_income_stmt(self):
            return pd.DataFrame(self._data["quarterly_income_stmt"])

        @property
        def quarterly_cashflow(self):
            return pd.DataFrame(self._data["quarterly_cashflow"])

        @property
        def quarterly_balance_sheet(self):
            return pd.DataFrame(self._data["quarterly_balance_sheet"])

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)

    src = YFinanceSource(max_concurrent=2)
    reports = await src.fetch_financial_report("AAPL", periods=["2025-Q1"])
    # We expect Q1 2025 (period_end 2025-03-31) to match
    q1 = [r for r in reports if r.fiscal_period == "2025-Q1"]
    assert q1, f"No Q1 report found in {[r.fiscal_period for r in reports]}"
    assert float(q1[0].revenue) == 95_360_000_000
    assert float(q1[0].net_income) == 23_636_000_000
    assert float(q1[0].fcf) == 24_400_000_000


@pytest.mark.asyncio
async def test_fetch_news_parses_recent_items(monkeypatch):
    import yfinance
    from freezegun import freeze_time
    fixture = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "yfinance_news.json").read_text()
    )

    class FakeTicker:
        def __init__(self, ticker):
            self.news = fixture[ticker]

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)

    src = YFinanceSource(max_concurrent=2)
    with freeze_time("2025-06-03 12:00:00"):
        items = await src.fetch_news("AAPL", days=7)
    assert len(items) == 2

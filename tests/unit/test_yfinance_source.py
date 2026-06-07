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

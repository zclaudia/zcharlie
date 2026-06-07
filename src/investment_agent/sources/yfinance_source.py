"""yfinance-based DataSource implementation."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import yfinance as yf

from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FinancialReportRecord,
    FundamentalSnapshotRecord,
    ListingRef,
    NewsRecord,
)
from investment_agent.sources.retry import RateLimiter, retry_async


class YFinanceSource:
    name = "yfinance"
    supported_markets = ["NASDAQ", "NYSE", "HKEX", "TWSE", "SSE", "SZSE"]

    def __init__(self, max_concurrent: int = 10):
        self._limiter = RateLimiter(max_concurrent=max_concurrent)

    @retry_async(attempts=3, base_delay=1.0)
    async def fetch_daily_prices(
        self, tickers: list[str], start: date, end: date
    ) -> list[DailyPriceRecord]:
        async with self._limiter:
            df = await asyncio.to_thread(
                yf.download,
                tickers,
                start=start.isoformat(),
                end=end.isoformat(),
                progress=False,
                auto_adjust=False,
                threads=False,
            )
        if df is None or df.empty:
            return []
        return _df_to_price_records(df, tickers)

    async def fetch_universe(
        self, universe_code: str, as_of: date
    ) -> list[ListingRef]:
        raise NotImplementedError("Implemented in Task 17")

    async def fetch_financial_report(
        self, ticker: str, periods: list[str]
    ) -> list[FinancialReportRecord]:
        raise NotImplementedError("Implemented in Task 18")

    async def fetch_fundamental_snapshot(
        self, ticker: str, as_of: date
    ) -> FundamentalSnapshotRecord:
        raise NotImplementedError("Implemented in Task 16")

    async def fetch_news(self, ticker: str, days: int) -> list[NewsRecord]:
        raise NotImplementedError("Implemented in Task 19")

    async def fetch_analyst_ratings(
        self, ticker: str, days: int
    ) -> list[AnalystRatingRecord]:
        raise NotImplementedError("Implemented in Task 20")


def _df_to_price_records(
    df: pd.DataFrame, tickers: list[str]
) -> list[DailyPriceRecord]:
    out: list[DailyPriceRecord] = []
    df = df.reset_index()

    if "ticker" in df.columns:
        ticker_col = "ticker"
    else:
        ticker_col = None

    for _, row in df.iterrows():
        ticker = row[ticker_col] if ticker_col else tickers[0]
        d = row["date"] if "date" in row.index else row["Date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        out.append(
            DailyPriceRecord(
                ticker=str(ticker),
                date=d,
                open=Decimal(str(row.get("open", row.get("Open")))),
                high=Decimal(str(row.get("high", row.get("High")))),
                low=Decimal(str(row.get("low", row.get("Low")))),
                close=Decimal(str(row.get("close", row.get("Close")))),
                volume=int(row.get("volume", row.get("Volume", 0))),
                adjusted_close=Decimal(
                    str(row.get("adjusted_close", row.get("Adj Close", row.get("close", row.get("Close")))))
                ),
            )
        )
    return out

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
        if universe_code != "NASDAQ_100":
            raise ValueError(f"Unsupported universe code: {universe_code}")
        async with self._limiter:
            tickers = await asyncio.to_thread(_fetch_nasdaq_100_tickers)
        return [
            ListingRef(
                market_code="NASDAQ",
                ticker=t,
                company_name=n,
                sector_code=s,
            )
            for (t, n, s) in tickers
        ]

    @retry_async(attempts=3, base_delay=1.0)
    async def fetch_financial_report(
        self, ticker: str, periods: list[str]
    ) -> list[FinancialReportRecord]:
        """Fetch quarterly financial statements.

        `periods` is a list like ['2025-Q1', '2025-Q2'] used to filter which quarters to return.
        """
        async with self._limiter:
            t = await asyncio.to_thread(yf.Ticker, ticker)
            income = await asyncio.to_thread(lambda: t.quarterly_income_stmt)
            cashflow = await asyncio.to_thread(lambda: t.quarterly_cashflow)
            balance = await asyncio.to_thread(lambda: t.quarterly_balance_sheet)

        out: list[FinancialReportRecord] = []
        for col in income.columns:
            period_end = pd.to_datetime(col).date()
            period_label = f"{period_end.year}-Q{(period_end.month - 1) // 3 + 1}"
            if periods and period_label not in periods:
                continue
            rev = income.at["Total Revenue", col] if "Total Revenue" in income.index else None
            ni = income.at["Net Income", col] if "Net Income" in income.index else None
            fcf_val = None
            if "Free Cash Flow" in cashflow.index and col in cashflow.columns:
                fcf_val = cashflow.at["Free Cash Flow", col]
            ta = None
            te = None
            if col in balance.columns:
                if "Total Assets" in balance.index:
                    ta = balance.at["Total Assets", col]
                if "Stockholders Equity" in balance.index:
                    te = balance.at["Stockholders Equity", col]

            out.append(
                FinancialReportRecord(
                    ticker=ticker,
                    fiscal_period=period_label,
                    period_end=period_end,
                    revenue=_maybe_decimal(rev),
                    net_income=_maybe_decimal(ni),
                    fcf=_maybe_decimal(fcf_val),
                    total_assets=_maybe_decimal(ta),
                    total_equity=_maybe_decimal(te),
                    raw_data={
                        "income": income[col].dropna().to_dict() if col in income.columns else {},
                        "cashflow": cashflow[col].dropna().to_dict() if col in cashflow.columns else {},
                        "balance": balance[col].dropna().to_dict() if col in balance.columns else {},
                    },
                )
            )
        return out

    @retry_async(attempts=3, base_delay=1.0)
    async def fetch_fundamental_snapshot(
        self, ticker: str, as_of: date
    ) -> FundamentalSnapshotRecord:
        async with self._limiter:
            info = await asyncio.to_thread(lambda: yf.Ticker(ticker).info)
        info = info or {}

        market_cap = _maybe_decimal(info.get("marketCap"))
        fcf = _maybe_decimal(info.get("freeCashflow"))
        fcf_yield = None
        if fcf is not None and market_cap not in (None, Decimal(0)):
            fcf_yield = fcf / market_cap

        return FundamentalSnapshotRecord(
            ticker=ticker,
            date=as_of,
            pe=_maybe_decimal(info.get("trailingPE")),
            pb=_maybe_decimal(info.get("priceToBook")),
            peg=_maybe_decimal(info.get("trailingPegRatio")),
            ps=_maybe_decimal(info.get("priceToSalesTrailing12Months")),
            roe=_maybe_decimal(info.get("returnOnEquity")),
            roa=_maybe_decimal(info.get("returnOnAssets")),
            fcf_yield=fcf_yield,
            debt_to_equity=_maybe_decimal(info.get("debtToEquity")),
            market_cap=market_cap,
        )

    async def fetch_news(self, ticker: str, days: int) -> list[NewsRecord]:
        raise NotImplementedError("Implemented in Task 19")

    async def fetch_analyst_ratings(
        self, ticker: str, days: int
    ) -> list[AnalystRatingRecord]:
        raise NotImplementedError("Implemented in Task 20")


def _fetch_nasdaq_100_tickers() -> list[tuple[str, str, str]]:
    """Fetch NDX components from Wikipedia table.

    Returns list of (ticker, company_name, sector_code).
    """
    tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
    # Wikipedia's NDX page typically has constituents in one of the early tables.
    for tbl in tables:
        cols = {c.lower() for c in tbl.columns.astype(str)}
        if "ticker" in cols and ("company" in cols or "security" in cols):
            df = tbl
            ticker_col = next(c for c in df.columns if c.lower() == "ticker")
            name_col = next(c for c in df.columns if c.lower() in ("company", "security"))
            sector_col = next(
                (c for c in df.columns if "sector" in str(c).lower()), None
            )
            out: list[tuple[str, str, str]] = []
            for _, row in df.iterrows():
                ticker = str(row[ticker_col]).strip()
                name = str(row[name_col]).strip()
                sector = str(row[sector_col]).strip() if sector_col else "UNKNOWN"
                sector_code = _normalize_sector(sector)
                out.append((ticker, name, sector_code))
            return out
    raise RuntimeError("Could not locate NASDAQ-100 components table")


def _normalize_sector(raw: str) -> str:
    s = raw.lower()
    if "tech" in s or "information" in s:
        return "TECH"
    if "communication" in s:
        return "COMMUNICATION"
    if "consumer discretionary" in s:
        return "CONSUMER_DISCRETIONARY"
    if "consumer staples" in s:
        return "CONSUMER_STAPLES"
    if "health" in s:
        return "HEALTHCARE"
    if "industrial" in s:
        return "INDUSTRIALS"
    if "material" in s:
        return "MATERIALS"
    if "energy" in s:
        return "ENERGY"
    if "financial" in s:
        return "FINANCIALS"
    if "utilities" in s:
        return "UTILITIES"
    if "real estate" in s:
        return "REAL_ESTATE"
    return "UNKNOWN"


def _maybe_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


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

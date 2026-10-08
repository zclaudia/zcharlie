"""Synthetic provider edges for the offline funnel smoke (P1-F1).

A ten-stock target pool (published as ``CSI300`` by the fake baostock) whose
profiles cover a bank, utilities, a cyclical, a loss-maker, growth names and a
recent IPO. Everything is a pure function of the profile and the calendar
date, so any window the collectors ask for is answered consistently:

* :class:`SyntheticBaostock` - the raw ``baostock`` module surface used by
  ``BaostockReferenceClient`` / ``BaostockMarketClient`` (constituents, CSRC
  industry, stock basics, daily bars with valuation columns, quarterly
  profit / balance / cash-flow / growth tables, dividend events);
* :func:`shenwan_history` - the AkShare Shenwan 2021 history table;
* :class:`SyntheticEastmoney` - the AkShare Eastmoney period summaries;
* :class:`StubLLM` - the OpenAI-compatible endpoint (respx) answering rule
  drafts and research explanations with valid, policy-conforming JSON;
* :func:`ifind_answers` - iFinD proxy answers for verification.

Prices: ``close = base * shape(t) * exp(sigma * z)`` with an iid Gaussian
``z`` seeded by ticker and day (so daily returns stay realistic and any
window replays identically). PE / PB / PS follow from the latest annual
report public on that day.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.providers.akshare_eastmoney import SUMMARY_COLUMNS, SummaryEndpoint
from investment_agent.providers.akshare_shenwan import RawTable
from investment_agent.rules.schema import RuleTemplate

HISTORY_START = date(2023, 9, 1)
LAST_TRADE_DAY = date(2026, 9, 30)
POOL_START = date(2023, 1, 2)
"""Monday from which the fake index publishes the pool (IPO: later)."""
ADJUST_FACTOR = 1.8
"""Backward-adjusted closes are the raw closes times this constant."""

PROFIT_FIELDS = [
    "code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM",
    "MBRevenue", "totalShare", "liqaShare",
]
BALANCE_FIELDS = [
    "code", "pubDate", "statDate", "currentRatio", "quickRatio", "cashRatio", "YOYLiability",
    "liabilityToAsset", "assetToEquity",
]
CASH_FLOW_FIELDS = [
    "code", "pubDate", "statDate", "CAToAsset", "NCAToAsset", "tangibleAssetToAsset",
    "ebitToInterest", "CFOToOR", "CFOToNP", "CFOToGr",
]
GROWTH_FIELDS = [
    "code", "pubDate", "statDate", "YOYEquity", "YOYAsset", "YOYNI", "YOYEPSBasic", "YOYPNI",
]
DIVIDEND_FIELDS = [
    "code", "dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate", "dividPlanDate",
    "dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate",
    "dividCashPsBeforeTax", "dividCashPsAfterTax", "dividStocksPs", "dividCashStock",
    "dividReserveToStockPs",
]


@dataclass(frozen=True)
class Profile:
    ticker: str
    name: str
    csrc: str
    """baostock CSRC label, e.g. ``J66货币金融服务``."""
    sw_code: str
    """Shenwan 2021 level-3 code (valid from 2021-07-30, or the IPO)."""
    ipo: date
    base_price: float
    shape: str
    sigma: float
    shares: float
    net_profit_2025: float
    profit_growth: float
    revenue_2025: float | None
    """Main-business revenue (``MBRevenue``); ``None`` for banks."""
    revenue_growth: float
    roe: tuple[float, float, float, float, float]
    """Annual ``roeAvg`` 2021..2025; 2026 periods reuse the 2025 value."""
    gross_margin: float | None
    net_margin: float
    debt_ratio: float
    dividends: tuple[tuple[date, float], ...] = ()
    """``(ex-date, pre-tax cash per share)``."""
    in_pool_from: date = POOL_START

    @property
    def code(self) -> str:
        symbol, exchange = self.ticker.split(".")
        return f"{exchange.lower()}.{symbol}"

    @property
    def symbol(self) -> str:
        return self.ticker.split(".")[0]

    def net_profit(self, year: int) -> float:
        return self.net_profit_2025 * (1 + self.profit_growth) ** (year - 2025)

    def revenue(self, year: int) -> float | None:
        if self.revenue_2025 is None:
            return None
        return self.revenue_2025 * (1 + self.revenue_growth) ** (year - 2025)

    def roe_of(self, year: int) -> float:
        return self.roe[min(max(year, 2021), 2025) - 2021]

    def book_value_per_share(self, year: int) -> float:
        roe = self.roe_of(year)
        equity = self.net_profit(year) / roe if roe > 0 else abs(self.net_profit(year)) * 8
        return equity / self.shares


def _ex(day: str, cash: float) -> tuple[date, float]:
    return date.fromisoformat(day), cash


PROFILES: tuple[Profile, ...] = (
    # Bank: cheap vs its own history, ~5% yield -> dividend_blue_chip.
    Profile(
        "601398.SH", "工商银行", "J66货币金融服务", "480201", date(2006, 10, 27),
        6.6, "value", 0.007, 3.564e11, 3.66e11, 0.01, None, 0.0,
        (0.120, 0.115, 0.110, 0.105, 0.100), None, 0.40, 0.92,
        (_ex("2025-07-14", 0.306), _ex("2026-01-09", 0.141), _ex("2026-07-14", 0.165)),
    ),
    # Bank: yield below the floor -> miss.
    Profile(
        "600036.SH", "招商银行", "J66货币金融服务", "480301", date(2002, 4, 9),
        38.0, "rally", 0.009, 2.522e10, 1.48e11, -0.01, None, 0.0,
        (0.160, 0.155, 0.150, 0.145, 0.140), None, 0.43, 0.91,
        (_ex("2025-07-11", 0.60), _ex("2026-07-10", 0.62)),
    ),
    # Utility: hydro power, stable, ~4% yield -> dividend_blue_chip.
    Profile(
        "600900.SH", "长江电力", "D44电力、热力生产和供应业", "410102", date(2003, 11, 18),
        28.0, "value", 0.008, 2.4468e10, 3.25e10, 0.05, 8.4e10, 0.04,
        (0.150, 0.152, 0.155, 0.158, 0.160), 0.58, 0.39, 0.55,
        (_ex("2025-07-18", 0.94), _ex("2026-07-17", 0.97)),
    ),
    # Utility: volatile ROE -> roe_stability miss.
    Profile(
        "600011.SH", "华能国际", "D44电力、热力生产和供应业", "410101", date(2001, 12, 6),
        7.0, "flat", 0.010, 1.5698e10, 1.0e10, 0.10, 2.4e11, 0.02,
        (-0.020, 0.020, 0.120, 0.050, 0.090), 0.18, 0.04, 0.68,
        (_ex("2026-07-20", 0.27),),
    ),
    # Cyclical: copper miner after a rally.
    Profile(
        "601899.SH", "紫金矿业", "B09有色金属矿采选业", "240302", date(2008, 4, 25),
        12.0, "rally", 0.016, 2.6577e10, 3.2e10, 0.25, 3.0e11, 0.10,
        (0.150, 0.190, 0.160, 0.200, 0.240), 0.20, 0.10, 0.56,
        (_ex("2026-07-03", 0.22),),
    ),
    # Cyclical loss-maker: negative profit, PE / growth not applicable.
    Profile(
        "601111.SH", "中国国航", "G56航空运输业", "421001", date(2006, 8, 18),
        7.5, "flat", 0.014, 1.7448e10, -2.4e9, 0.0, 1.6e11, 0.08,
        (-0.300, -0.250, -0.050, -0.020, -0.040), 0.05, -0.015, 0.86,
    ),
    # Growth manufacturing: battery maker, de-rated -> quality_growth.
    Profile(
        "300750.SZ", "宁德时代", "C38电气机械和器材制造业", "630701", date(2018, 6, 11),
        260.0, "decline", 0.012, 4.40e9, 5.1e10, 0.20, 3.6e11, 0.20,
        (0.230, 0.240, 0.235, 0.240, 0.245), 0.27, 0.14, 0.62,
        (_ex("2026-04-28", 4.55),),
    ),
    # Consumer: premium liquor, de-rated -> quality_growth.
    Profile(
        "600519.SH", "贵州茅台", "C15酒、饮料和精制茶制造业", "340501", date(2001, 8, 27),
        1500.0, "decline", 0.009, 1.2562e9, 8.62e10, 0.13, 1.74e11, 0.16,
        (0.300, 0.320, 0.330, 0.335, 0.330), 0.915, 0.50, 0.16,
        (_ex("2025-06-26", 27.673), _ex("2025-12-19", 23.957), _ex("2026-06-25", 28.0)),
    ),
    # Healthcare: steady but slower growth.
    Profile(
        "600276.SH", "恒瑞医药", "C27医药制造业", "370102", date(2000, 10, 18),
        48.0, "rally", 0.013, 6.38e9, 6.3e9, 0.08, 2.8e10, 0.07,
        (0.140, 0.120, 0.130, 0.140, 0.150), 0.86, 0.22, 0.12,
        (_ex("2026-06-19", 0.20),),
    ),
    # Recent IPO: listed 2026-04-20, joins the pool 2026-06-15; too little
    # history for 1y statistics and valuation percentiles.
    Profile(
        "001389.SZ", "新上市科技", "C39计算机、通信和其他电子设备制造业", "270106",
        date(2026, 4, 20), 60.0, "rally", 0.020, 4.0e8, 1.2e9, 0.30, 6.0e9, 0.30,
        (0.200, 0.210, 0.220, 0.230, 0.240), 0.35, 0.20, 0.40,
        in_pool_from=date(2026, 6, 15),
    ),
)
PROFILE_BY_CODE: dict[str, Profile] = {profile.code: profile for profile in PROFILES}
PROFILE_BY_TICKER: dict[str, Profile] = {profile.ticker: profile for profile in PROFILES}
TICKERS: tuple[str, ...] = tuple(sorted(PROFILE_BY_TICKER))


def shanghai_today() -> date:
    return datetime.now(UTC).astimezone(SHANGHAI_TZ).date()


def _shape(name: str, t: float) -> float:
    if name == "value":  # peaks mid-window, ends in the lower part of its range
        return 1.0 + 0.30 * math.sin(math.pi * t) - 0.12 * t
    if name == "decline":
        return 1.25 - 0.40 * t
    if name == "rally":
        return 0.75 + 0.50 * t
    return 1.0


def close_on(profile: Profile, day: date) -> float:
    span = (LAST_TRADE_DAY - HISTORY_START).days
    t = min(max((day - HISTORY_START).days / span, 0.0), 1.0)
    noise = random.Random(f"{profile.ticker}:{day.isoformat()}").gauss(0.0, profile.sigma)
    return round(profile.base_price * _shape(profile.shape, t) * math.exp(noise), 2)


def _quarter_end(year: int, quarter: int) -> date:
    month = quarter * 3
    return date(year, month, 30 if month in (6, 9) else 31)


def pub_date(profile: Profile, year: int, quarter: int) -> date:
    """Disclosure dates: Q1 04-25, H1 08-20, Q3 10-25, annual 04-20 next year.

    A recent IPO's pre-listing reports are published with its prospectus."""

    day = {
        1: date(year, 4, 25),
        2: date(year, 8, 20),
        3: date(year, 10, 25),
        4: date(year + 1, 4, 20),
    }[quarter]
    if day < profile.ipo:
        return profile.ipo - timedelta(days=10)
    return day


def latest_annual_year(profile: Profile, day: date) -> int:
    year = day.year - 1
    while pub_date(profile, year, 4) > day:
        year -= 1
    return year


def _fmt(value: float | None, digits: int = 6) -> str:
    return "" if value is None else f"{value:.{digits}f}"


class _Result:
    """A ``baostock.data.resultset.ResultData`` stand-in."""

    def __init__(self, fields: list[str], rows: list[list[str]]) -> None:
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = list(fields)
        self._rows = rows
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        row = self._rows[self._index]
        self._index += 1
        return row


@dataclass
class _Status:
    error_code: str = "0"
    error_msg: str = "success"


@dataclass
class SyntheticBaostock:
    """The raw baostock module surface; ``calls`` counts queries by method."""

    profiles: Mapping[str, Profile] = field(default_factory=lambda: dict(PROFILE_BY_CODE))
    calls: dict[str, int] = field(default_factory=dict)

    def _count(self, method: str) -> None:
        self.calls[method] = self.calls.get(method, 0) + 1

    def login(self) -> _Status:
        return _Status()

    def logout(self) -> _Status:
        return _Status()

    # -- reference ---------------------------------------------------------- #

    def query_hs300_stocks(self, date: str = "") -> _Result:  # noqa: A002
        self._count("query_hs300_stocks")
        day = _date(date)
        monday = day - timedelta(days=day.weekday())
        update = min(monday, LAST_TRADE_DAY - timedelta(days=LAST_TRADE_DAY.weekday()))
        fields = ["updateDate", "code", "code_name"]
        if update < POOL_START:
            return _Result(fields, [])
        rows = [
            [update.isoformat(), profile.code, profile.name]
            for profile in sorted(self.profiles.values(), key=lambda p: p.code)
            if profile.in_pool_from <= update
        ]
        return _Result(fields, rows)

    def query_sz50_stocks(self, date: str = "") -> _Result:  # noqa: A002
        return _Result(["updateDate", "code", "code_name"], [])

    def query_zz500_stocks(self, date: str = "") -> _Result:  # noqa: A002
        return _Result(["updateDate", "code", "code_name"], [])

    def query_stock_industry(self, code: str = "", date: str = "") -> _Result:  # noqa: A002
        self._count("query_stock_industry")
        day = _date(date) if date else shanghai_today()
        fields = ["updateDate", "code", "code_name", "industry", "industryClassification"]
        rows = [
            [
                max(profile.ipo, date_cls(2020, 1, 6)).isoformat(),
                profile.code,
                profile.name,
                profile.csrc,
                "证监会行业分类",
            ]
            for profile in sorted(self.profiles.values(), key=lambda p: p.code)
            if (not code or profile.code == code) and profile.ipo <= day
        ]
        return _Result(fields, rows)

    def query_all_stock(self, day: str = "") -> _Result:
        """Every security listed on ``day`` (mrd-intake D4): the pool stocks plus
        an index row the directory sync must drop; empty on weekends and after
        the last synthetic trade day, like the real answer for a non-trading day."""

        self._count("query_all_stock")
        asked = _date(day) if day else shanghai_today()
        fields = ["code", "tradeStatus", "code_name"]
        if asked.weekday() >= 5 or asked > LAST_TRADE_DAY:
            return _Result(fields, [])
        rows = [
            [profile.code, "1", profile.name]
            for profile in sorted(self.profiles.values(), key=lambda p: p.code)
            if profile.ipo <= asked
        ]
        rows.append(["sh.000300", "1", "沪深300指数"])
        return _Result(fields, rows)

    def query_stock_basic(self, code: str = "", code_name: str = "") -> _Result:
        self._count("query_stock_basic")
        fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        profile = self.profiles.get(code)
        if profile is None:
            return _Result(fields, [])
        return _Result(fields, [[code, profile.name, profile.ipo.isoformat(), "", "1", "1"]])

    # -- daily bars --------------------------------------------------------- #

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "d",
        adjustflag: str = "3",
    ) -> _Result:
        self._count("query_history_k_data_plus")
        names = fields.split(",")
        profile = self.profiles.get(code)
        if profile is None:
            return _Result(names, [])
        start = max(_date(start_date or "1990-01-01"), profile.ipo, HISTORY_START)
        end = min(_date(end_date or "2099-01-01"), LAST_TRADE_DAY, shanghai_today())
        factor = ADJUST_FACTOR if adjustflag == "1" else 1.0
        rows: list[list[str]] = []
        day = start
        while day <= end:
            if day.weekday() < 5:
                rows.append([str(self._bar(profile, day, factor, adjustflag).get(n, "")) for n in names])
            day += timedelta(days=1)
        return _Result(names, rows)

    def _bar(self, profile: Profile, day: date, factor: float, adjustflag: str) -> dict[str, Any]:
        close = close_on(profile, day)
        year = latest_annual_year(profile, day)
        eps = profile.net_profit(year) / profile.shares
        revenue = profile.revenue(year) or profile.net_profit(year) / profile.net_margin
        price = f"{close * factor:.4f}"
        return {
            "date": day.isoformat(),
            "code": profile.code,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": "1000000",
            "amount": f"{close * 1_000_000:.2f}",
            "adjustflag": adjustflag,
            "turn": "0.85",
            "tradestatus": "1",
            "peTTM": f"{close / eps:.6f}",
            "pbMRQ": f"{close / profile.book_value_per_share(year):.6f}",
            "psTTM": f"{close * profile.shares / abs(revenue):.6f}",
            "isST": "0",
        }

    # -- quarterly tables --------------------------------------------------- #

    def _quarterly(self, method: str, fields: list[str], code: str, year: int, quarter: int) -> _Result:
        self._count(method)
        profile = self.profiles.get(code)
        published = pub_date(profile, year, quarter) if profile else None
        if profile is None or published is None or published > shanghai_today() or year < 2018:
            return _Result(fields, [])
        share = quarter / 4
        net_profit = profile.net_profit(year) * share
        revenue = profile.revenue(year)
        values = {
            "code": code,
            "pubDate": published.isoformat(),
            "statDate": _quarter_end(year, quarter).isoformat(),
            "roeAvg": _fmt(profile.roe_of(year) * share),
            "npMargin": _fmt(profile.net_margin),
            "gpMargin": _fmt(profile.gross_margin),
            "netProfit": _fmt(net_profit, 2),
            "epsTTM": _fmt(profile.net_profit(year) / profile.shares),
            "MBRevenue": _fmt(None if revenue is None else revenue * share, 2),
            "totalShare": _fmt(profile.shares, 2),
            "liqaShare": _fmt(profile.shares, 2),
            "currentRatio": "1.500000",
            "quickRatio": "1.100000",
            "cashRatio": "0.400000",
            "YOYLiability": "0.050000",
            "liabilityToAsset": _fmt(profile.debt_ratio),
            "assetToEquity": _fmt(1 / (1 - profile.debt_ratio)),
            "CAToAsset": "0.400000",
            "NCAToAsset": "0.600000",
            "tangibleAssetToAsset": "0.800000",
            "ebitToInterest": "",
            "CFOToOR": "0.300000",
            "CFOToNP": "1.200000",
            "CFOToGr": "0.300000",
            "YOYEquity": "0.080000",
            "YOYAsset": "0.070000",
            "YOYNI": _fmt(profile.profit_growth),
            "YOYEPSBasic": _fmt(profile.profit_growth),
            "YOYPNI": _fmt(profile.profit_growth),
        }
        return _Result(fields, [[values[name] for name in fields]])

    def query_profit_data(self, code: str, year: int | None = None, quarter: int | None = None) -> _Result:
        return self._quarterly("query_profit_data", PROFIT_FIELDS, code, int(year or 0), int(quarter or 0))

    def query_balance_data(self, code: str, year: int | None = None, quarter: int | None = None) -> _Result:
        return self._quarterly("query_balance_data", BALANCE_FIELDS, code, int(year or 0), int(quarter or 0))

    def query_cash_flow_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> _Result:
        return self._quarterly(
            "query_cash_flow_data", CASH_FLOW_FIELDS, code, int(year or 0), int(quarter or 0)
        )

    def query_growth_data(self, code: str, year: int | None = None, quarter: int | None = None) -> _Result:
        return self._quarterly("query_growth_data", GROWTH_FIELDS, code, int(year or 0), int(quarter or 0))

    # -- dividends ---------------------------------------------------------- #

    def query_dividend_data(
        self, code: str, year: str | None = None, yearType: str = "report"  # noqa: N803
    ) -> _Result:
        self._count("query_dividend_data")
        profile = self.profiles.get(code)
        rows: list[list[str]] = []
        for ex_date, cash in profile.dividends if profile else ():
            if str(ex_date.year) != str(year) or ex_date > shanghai_today():
                continue
            values = {
                "code": code,
                "dividPlanAnnounceDate": (ex_date - timedelta(days=60)).isoformat(),
                "dividPlanDate": (ex_date - timedelta(days=7)).isoformat(),
                "dividRegistDate": (ex_date - timedelta(days=1)).isoformat(),
                "dividOperateDate": ex_date.isoformat(),
                "dividPayDate": ex_date.isoformat(),
                "dividCashPsBeforeTax": f"{cash:.4f}",
                "dividCashPsAfterTax": f"{cash * 0.9:.4f}",
                "dividStocksPs": "0.000000",
                "dividCashStock": f"10派{cash * 10:.2f}元（含税）",
            }
            rows.append([values.get(name, "") for name in DIVIDEND_FIELDS])
        return _Result(DIVIDEND_FIELDS, rows)


date_cls = date


def _date(text: str) -> date:
    return date.fromisoformat(text)


# --------------------------------------------------------------------------- #
# AkShare: Shenwan 2021 history and Eastmoney period summaries
# --------------------------------------------------------------------------- #


def shenwan_history() -> RawTable:
    rows = []
    for profile in PROFILES:
        start = max(profile.ipo, date(2021, 7, 30))
        rows.append(
            {
                "symbol": profile.symbol,
                "start_date": start.isoformat(),
                "industry_code": profile.sw_code,
                "update_time": start.isoformat(),
            }
        )
    return RawTable(("symbol", "start_date", "industry_code", "update_time"), tuple(rows))


@dataclass
class SyntheticEastmoney:
    """``EastmoneyBackend``: annual summaries of the pool; no cash-flow statements."""

    calls: list[str] = field(default_factory=list)

    def period_table(self, endpoint: SummaryEndpoint, period: str) -> RawTable | None:
        self.calls.append(f"{endpoint}:{period}")
        report = datetime.strptime(period, "%Y%m%d").date()
        if (report.month, report.day) != (12, 31):
            return None
        columns = SUMMARY_COLUMNS[endpoint]
        rows = []
        for index, profile in enumerate(PROFILES, start=1):
            notice = pub_date(profile, report.year, 4)
            if notice > shanghai_today():
                continue
            values = self._values(endpoint, profile, report.year)
            row = dict.fromkeys(columns)
            row.update(values)
            row.update(
                {
                    "序号": index,
                    "股票代码": profile.symbol,
                    "股票简称": profile.name,
                    "公告日期": notice.isoformat(),
                }
            )
            rows.append(row)
        return RawTable(columns, tuple(rows)) if rows else None

    def _values(self, endpoint: str, profile: Profile, year: int) -> dict[str, Any]:
        net_profit = profile.net_profit(year)
        revenue = profile.revenue(year) or net_profit / profile.net_margin
        if endpoint == "stock_lrb_em":
            # 归母净利润 slightly below the consolidated baostock netProfit.
            return {"净利润": round(net_profit * 0.97, 2), "营业总收入": round(revenue * 1.02, 2)}
        if endpoint == "stock_xjll_em":
            return {"经营性现金流-现金流量净额": round(net_profit * 1.2, 2)}
        equity = profile.book_value_per_share(year) * profile.shares
        assets = equity / (1 - profile.debt_ratio)
        liabilities = assets - equity
        return {
            "资产-总资产": round(assets, 2),
            "负债-总负债": round(liabilities, 2),
            "资产负债率": round(liabilities / assets * 100, 6),
            "股东权益合计": round(equity, 2),
        }

    def cash_flow_table(self, symbol: str) -> RawTable | None:
        self.calls.append(f"cash_flow:{symbol}")
        return None


# --------------------------------------------------------------------------- #
# LLM stub (rule drafts + research explanations)
# --------------------------------------------------------------------------- #


_BUY_FACTOR = re.compile(r"_(pe|pb)_")


@dataclass
class StubLLM:
    """Answers every chat completion with valid JSON for the task in the prompt."""

    templates: Mapping[str, RuleTemplate]
    requests: list[str] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        payload = json.loads(user)
        if isinstance(payload, dict) and payload.get("task") == "rule_draft":
            self.requests.append("rule_draft")
            content = self.rule_draft(payload)
        else:
            self.requests.append("explanation")
            content = self.explanation(payload)
        return httpx.Response(
            200,
            json={
                "id": f"stub-{len(self.requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(content, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
            },
        )

    def rule_draft(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Tighten the buy-zone valuation parameter halfway towards its minimum."""

        template = self.templates[payload["template"]["id"]]
        name = next(param for param in template.params if param.startswith("buy_"))
        match = _BUY_FACTOR.search(name)
        cited = f"{match.group(1) if match else 'pb'}_percentile_5y"
        factor = payload["factors"].get(cited) or {}
        default_param = next(iter(template.params))
        defaults = {
            "param": default_param,
            "choice": "default",
            "reason": "模板未校准，暂无依据调整，沿用默认值。",
            "cited_factors": [],
        }
        if factor.get("value") is None:
            return {
                "params": {},
                "rationale": [defaults],
                "overall_rationale": "沿用模板全部默认参数；模板未校准。",
                "watch_points": ["估值分位缺失，等待更长的历史后复核。"],
            }
        bounds = template.param_ranges[name]
        value = round((bounds.min + template.params[name]) / 2, 4)
        return {
            "params": {name: value},
            "rationale": [
                {
                    "param": name,
                    "choice": "adjusted",
                    "reason": f"{cited} 显示估值处于自身历史区间的位置，收紧 buy_zone 参数，"
                    "使候选区间只在估值更低时触发，属研究优先级假设。",
                    "cited_factors": [cited],
                },
                defaults,
            ],
            "overall_rationale": "沿用模板全部条件，只收紧 buy_zone 的估值分位参数；模板未校准。",
            "watch_points": [f"{cited} 回升到 drop 条件附近时需要复核。"],
        }

    def explanation(self, score_item: Mapping[str, Any]) -> dict[str, Any]:
        claim = {"text": "业务评分来自确定性内核。", "supporting_fields": ["business_score"]}
        return {
            "summary": "免费源快照模式的确定性评分解释（离线桩）。",
            "classification_rationale": [claim],
            "positive_evidence": [claim],
            "negative_evidence": [],
            "risk_factors": [],
            "data_quality_notes": [
                {"text": "缺失字段已列出。", "supporting_fields": ["missing_fields"]}
            ],
            "open_questions": ["自由现金流需要 iFinD 核对。"],
            "workflow_conclusion": "needs_review：先核对缺失字段。"
            if score_item.get("needs_agent_review")
            else "monitor：维持观察。",
        }


# --------------------------------------------------------------------------- #
# iFinD answers (verification)
# --------------------------------------------------------------------------- #

PROFITABILITY = "/financial/index:profitability"
CAPITAL = "/financial/index:capital_structure"
INCOME = "/financial/statements:income_statement"
CASH = "/financial/statements:cash_flow"


def ifind_answers(proxy: Any, profile: Profile, period: date, *, roe_override: float | None = None) -> None:
    """iFinD values agreeing with the free sources except an optional ROE."""

    year = period.year
    net_profit = profile.net_profit(year)
    revenue = profile.revenue(year) or net_profit / profile.net_margin
    roe = profile.roe_of(year) if roe_override is None else roe_override
    key = period.strftime("%Y%m%d")
    rows: dict[str, dict[str, str]] = {
        PROFITABILITY: {
            "ths_roe_stock": f"{roe * 100:.4f}",
            "ths_gross_selling_rate_stock": f"{(profile.gross_margin or 0) * 100:.4f}",
            "ths_net_sales_rate_stock": f"{profile.net_margin * 100:.4f}",
            "ths_eps_basic_stock": f"{net_profit / profile.shares:.4f}",
            "ths_nav_ps_stock": f"{profile.book_value_per_share(year):.4f}",
            "ths_revenue_ps_stock": f"{revenue / profile.shares:.4f}",
        },
        CAPITAL: {"ths_asset_liab_ratio_stock": f"{profile.debt_ratio * 100:.4f}"},
        INCOME: {
            "ths_operating_total_revenue_stock": f"{revenue * 1.02:.2f}",
            "ths_np_atoopc_stock": f"{net_profit * 0.97:.2f}",
        },
        CASH: {
            "ths_ncf_from_oa_stock": f"{net_profit * 1.2:.2f}",
            "ths_cash_paid_for_assets_stock": f"{net_profit * 0.3:.2f}",
        },
    }
    for endpoint, values in rows.items():
        proxy.financial.setdefault((endpoint, key), {})[profile.ticker] = values
    proxy.total_shares[profile.ticker] = profile.shares
    proxy.base_close[profile.ticker] = close_on(profile, LAST_TRADE_DAY) - (
        LAST_TRADE_DAY - date(2026, 1, 1)
    ).days


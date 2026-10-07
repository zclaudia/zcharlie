"""Capture baostock fixtures for offline adapter tests (P1-A1, P1-A7).

Runs the real :class:`BaostockReferenceClient` against the live baostock
service through a recording backend and writes the raw query answers to
``tests/fixtures/baostock/``. Offline tests replay these files through a fake
backend, so the adapter's parsing, ticker mapping and look-ahead guard are
exercised against genuine payloads without network access.

Files: ``constituents.json`` and ``industry.json`` (reference data),
``daily.json`` (raw + backward daily bars incl. suspended stocks, a suspended
*ST stock and a 2026 IPO) and ``financial.json`` (profit / balance /
cash-flow / growth answers incl. an undisclosed period and a bank) and
``dividend.json`` (``query_dividend_data`` by ex-date year: duplicated
preliminary rows, bonus / capitalisation issues and a year without dividends).

Usage::

    .venv/bin/python tools/capture_baostock_fixtures.py [reference] [daily] [financial] [dividend]

baostock logs in anonymously; no credentials exist. Only query answers
(``error_code``, ``error_msg``, ``fields``, ``rows`` and the paging counters)
are written; the login answer (which carries the session user id) is not.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from investment_agent.providers.baostock_market import (  # noqa: E402
    BaostockDataBackend,
    BaostockMarketClient,
)
from investment_agent.providers.baostock_reference import (  # noqa: E402
    BaostockReferenceClient,
    BaostockResultSet,
    BaostockStatus,
    load_default_backend,
)

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "baostock"

CONSTITUENT_REQUESTS: tuple[tuple[str, date], ...] = (
    ("CSI300", date(2026, 9, 30)),
    ("CSI300", date(2025, 6, 30)),
    ("CSI300", date(2004, 1, 2)),  # before the index was published -> empty
    ("CSI300", date(2027, 1, 1)),  # future date -> baostock answers its latest
    ("SSE50", date(2026, 9, 30)),
    ("CSI500", date(2026, 9, 30)),
)

INDUSTRY_AS_OF = date(2026, 9, 30)
INDUSTRY_SYMBOLS: tuple[str, ...] = (
    "600519.SH",  # 贵州茅台 (liquor)
    "000858.SZ",  # 五粮液
    "601398.SH",  # 工商银行 (bank)
    "600036.SH",  # 招商银行 (bank)
    "000001.SZ",  # 平安银行 (bank)
    "601318.SH",  # 中国平安 (insurer)
    "601628.SH",  # 中国人寿 (insurer)
    "600030.SH",  # 中信证券 (broker)
    "600900.SH",  # 长江电力 (utility)
    "600011.SH",  # 华能国际 (utility)
    "601088.SH",  # 中国神华 (coal)
    "601857.SH",  # 中国石油
    "600028.SH",  # 中国石化
    "300750.SZ",  # 宁德时代
    "002594.SZ",  # 比亚迪
    "000333.SZ",  # 美的集团
    "600276.SH",  # 恒瑞医药
    "002415.SZ",  # 海康威视
    "000002.SZ",  # 万科A
    "688981.SH",  # 中芯国际
    "600001.SH",  # 邯郸钢铁 (delisted: empty industry)
)
# Pre-2012 rows use the code-less hierarchical CSRC label.
LEGACY_INDUSTRY_REQUESTS: tuple[tuple[str, date], ...] = (("600519.SH", date(2010, 1, 4)),)

DAILY_SYMBOLS: tuple[str, ...] = (
    "600519.SH",  # 贵州茅台
    "000001.SZ",  # 平安银行
    "601398.SH",  # 工商银行
    "300750.SZ",  # 宁德时代
    "601059.SH",  # 信达证券: suspended through 2026-09 (tradestatus=0)
    "000016.SZ",  # *ST康佳A: suspended and ST
    "002813.SZ",  # 路畅科技: trading, then suspended on 2026-09-30
)
DAILY_WINDOW = (date(2026, 9, 1), date(2026, 9, 30))
DAILY_EXTRA: tuple[tuple[str, date, date, str], ...] = (
    ("001248.SZ", date(2026, 6, 20), date(2026, 7, 10), "none"),  # 华润新能源, IPO 2026-07-02
    ("001248.SZ", date(2026, 6, 20), date(2026, 7, 10), "backward"),
    ("600519.SH", date(2026, 9, 30), date(2026, 10, 9), "none"),  # holiday: ends 09-30
)
FINANCIAL_REQUESTS: tuple[tuple[str, int, int], ...] = (
    ("600519.SH", 2026, 2),
    ("600519.SH", 2025, 4),
    ("600519.SH", 2026, 3),  # not yet disclosed -> empty
    ("000001.SZ", 2026, 2),
    ("601398.SH", 2026, 1),  # bank: no gross margin / MBRevenue
    ("601398.SH", 2025, 4),
    ("300750.SZ", 2026, 2),
    ("001248.SZ", 2026, 2),  # recent IPO
    ("001248.SZ", 2025, 4),
    ("000016.SZ", 2026, 2),
)

DIVIDEND_REQUESTS: tuple[tuple[str, int], ...] = (
    ("601398.SH", 2025),  # 工商银行: preliminary + final rows for 2025-01-07
    ("601398.SH", 2026),
    ("600900.SH", 2025),  # 长江电力
    ("600900.SH", 2026),
    ("600941.SH", 2025),  # 中国移动
    ("600941.SH", 2026),
    ("600519.SH", 2025),  # 贵州茅台
    ("600519.SH", 2026),
    ("300750.SZ", 2023),  # 宁德时代: 10转8派25.2 (capitalisation issue)
    ("603288.SH", 2021),  # 海天味业: 10转2送1派10.3 (bonus + capitalisation)
    ("000002.SZ", 2025),  # 万科A: no dividend
    ("300760.SZ", 2025),  # 迈瑞医疗: two distributions on 2025-05-29 (summed)
    ("000651.SZ", 2020),  # 格力电器: a copy without the numeric cash on 2020-11-12
)


class _Replay:
    """Drained copy of a baostock result set, returned to the adapter."""

    def __init__(self, record: dict[str, Any]) -> None:
        self.error_code: str = record["error_code"]
        self.error_msg: str = record["error_msg"]
        self.fields: list[str] = list(record["fields"])
        self.cur_page_num: str | None = record.get("cur_page_num")
        self.per_page_count: str | None = record.get("per_page_count")
        self._rows: list[list[str]] = [list(row) for row in record["rows"]]
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        row = self._rows[self._index]
        self._index += 1
        return row


class RecordingBackend:
    def __init__(self, real: BaostockDataBackend) -> None:
        self._real = real
        self.records: list[dict[str, Any]] = []

    def login(self) -> BaostockStatus:
        return self._real.login()

    def logout(self) -> BaostockStatus:
        return self._real.logout()

    def _record(self, method: str, params: dict[str, str], result: BaostockResultSet) -> _Replay:
        rows: list[list[str]] = []
        while result.error_code == "0" and result.next():
            rows.append(list(result.get_row_data()))
        record: dict[str, Any] = {
            "method": method,
            "params": params,
            "error_code": result.error_code,
            "error_msg": result.error_msg,
            "fields": list(result.fields or []),
            "cur_page_num": _opt_str(getattr(result, "cur_page_num", None)),
            "per_page_count": _opt_str(getattr(result, "per_page_count", None)),
            "rows": rows,
        }
        self.records.append(record)
        return _Replay(record)

    def query_hs300_stocks(self, date: str = "") -> BaostockResultSet:
        return self._record("query_hs300_stocks", {"date": date}, self._real.query_hs300_stocks(date=date))

    def query_sz50_stocks(self, date: str = "") -> BaostockResultSet:
        return self._record("query_sz50_stocks", {"date": date}, self._real.query_sz50_stocks(date=date))

    def query_zz500_stocks(self, date: str = "") -> BaostockResultSet:
        return self._record("query_zz500_stocks", {"date": date}, self._real.query_zz500_stocks(date=date))

    def query_stock_industry(self, code: str = "", date: str = "") -> BaostockResultSet:
        return self._record(
            "query_stock_industry",
            {"code": code, "date": date},
            self._real.query_stock_industry(code=code, date=date),
        )

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "d",
        adjustflag: str = "3",
    ) -> BaostockResultSet:
        params = {
            "code": code,
            "fields": fields,
            "start_date": start_date or "",
            "end_date": end_date or "",
            "frequency": frequency,
            "adjustflag": adjustflag,
        }
        result = self._real.query_history_k_data_plus(
            code,
            fields,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjustflag=adjustflag,
        )
        return self._record("query_history_k_data_plus", params, result)

    def _quarterly(
        self, method: str, code: str, year: int | None, quarter: int | None
    ) -> BaostockResultSet:
        params = {"code": code, "year": str(year), "quarter": str(quarter)}
        result = getattr(self._real, method)(code=code, year=year, quarter=quarter)
        return self._record(method, params, result)

    def query_profit_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> BaostockResultSet:
        return self._quarterly("query_profit_data", code, year, quarter)

    def query_balance_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> BaostockResultSet:
        return self._quarterly("query_balance_data", code, year, quarter)

    def query_cash_flow_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> BaostockResultSet:
        return self._quarterly("query_cash_flow_data", code, year, quarter)

    def query_growth_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> BaostockResultSet:
        return self._quarterly("query_growth_data", code, year, quarter)

    def query_dividend_data(
        self, code: str, year: str | None = None, yearType: str = "report"  # noqa: N803
    ) -> BaostockResultSet:
        params = {"code": code, "year": str(year), "yearType": yearType}
        result = self._real.query_dividend_data(code=code, year=year, yearType=yearType)
        return self._record("query_dividend_data", params, result)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _write(name: str, records: list[dict[str, Any]], captured_at: datetime) -> Path:
    import baostock

    path = FIXTURE_DIR / name
    payload = {
        "captured_at": captured_at.isoformat(),
        "baostock_version": str(getattr(baostock, "__version__", "unknown")),
        "requests": records,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


GROUPS = ("reference", "daily", "financial", "dividend")


async def main(groups: tuple[str, ...] = GROUPS) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now(UTC).replace(microsecond=0)
    if "reference" in groups:
        await capture_reference(captured_at)
    if "daily" in groups:
        await capture_daily(captured_at)
    if "financial" in groups:
        await capture_financial(captured_at)
    if "dividend" in groups:
        await capture_dividend(captured_at)


async def capture_reference(captured_at: datetime) -> None:

    constituents = RecordingBackend(load_default_backend())
    client = BaostockReferenceClient(constituents)
    for index_code, as_of in CONSTITUENT_REQUESTS:
        snapshot = await client.fetch_constituents(index_code, as_of)  # type: ignore[arg-type]
        print(f"{index_code} {as_of}: {len(snapshot.members)} members, effective {snapshot.effective_date}")
    print(_write("constituents.json", constituents.records, captured_at))

    industry = RecordingBackend(load_default_backend())
    client = BaostockReferenceClient(industry)
    for symbol in INDUSTRY_SYMBOLS:
        result = await client.fetch_industry(INDUSTRY_AS_OF, symbol)
        print(f"{symbol}: {[(e.industry_code, e.industry_name) for e in result.entries]}")
    for symbol, as_of in LEGACY_INDUSTRY_REQUESTS:
        result = await client.fetch_industry(as_of, symbol)
        print(f"{symbol} {as_of}: {[(e.industry_code, e.industry_name) for e in result.entries]}")
    print(_write("industry.json", industry.records, captured_at))


async def capture_daily(captured_at: datetime) -> None:
    daily = RecordingBackend(load_default_backend())  # type: ignore[arg-type]
    market = BaostockMarketClient(daily)
    async with market.session():
        for symbol in DAILY_SYMBOLS:
            for adjust in ("none", "backward"):
                series = await market.fetch_daily_bars(symbol, *DAILY_WINDOW, adjust)  # type: ignore[arg-type]
                print(f"{symbol} {adjust}: {len(series.bars)} bars, skipped {series.skipped}")
        for symbol, start, end, adjust in DAILY_EXTRA:
            series = await market.fetch_daily_bars(symbol, start, end, adjust)  # type: ignore[arg-type]
            print(f"{symbol} {adjust} {start}..{end}: {len(series.bars)} bars")
    print(_write("daily.json", daily.records, captured_at))


async def capture_financial(captured_at: datetime) -> None:
    financial = RecordingBackend(load_default_backend())  # type: ignore[arg-type]
    market = BaostockMarketClient(financial)
    async with market.session():
        for symbol, year, quarter in FINANCIAL_REQUESTS:
            record = await market.fetch_quarterly_financials(symbol, year, quarter)
            print(f"{symbol} {year}Q{quarter}: {record.pub_date if record else 'undisclosed'}")
    print(_write("financial.json", financial.records, captured_at))


async def capture_dividend(captured_at: datetime) -> None:
    dividend = RecordingBackend(load_default_backend())  # type: ignore[arg-type]
    market = BaostockMarketClient(dividend)
    async with market.session():
        for symbol, year in DIVIDEND_REQUESTS:
            answer = await market.fetch_dividends(symbol, year)
            print(
                f"{symbol} {year}: {[(e.ex_date.isoformat(), str(e.cash_per_share)) for e in answer.events]}"
                f" duplicates {answer.duplicates}"
            )
    print(_write("dividend.json", dividend.records, captured_at))


if __name__ == "__main__":
    selected = tuple(arg for arg in sys.argv[1:] if arg in GROUPS) or GROUPS
    asyncio.run(main(selected))

"""Synthetic baostock history for the backfill tests (P1-A4).

Extends the fixture replay backend with:

* weekly constituent snapshots (``updateDate`` = Monday) from a change list;
* whole-market CSRC industry answers from per-ticker histories;
* ``query_stock_basic`` from IPO dates;
* daily bars for every weekday from a ticker's first trading day (synthetic
  prices; recorded fixture rows are not used for these tickers);
* quarterly tables for configured ``(ticker, period) -> pubDate``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from tests.unit._baostock_fake import FakeBaostockData, ReplayResult

LAST_UPDATE = date(2026, 10, 5)


def _code(ticker: str) -> str:
    symbol, suffix = ticker.split(".")
    return f"{suffix.lower()}.{symbol}"


def _ticker(code: str) -> str:
    exchange, symbol = code.split(".")
    return f"{symbol}.{exchange.upper()}"


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


@dataclass
class FakeBaostockHistory(FakeBaostockData):
    constituent_changes: list[tuple[date, set[str]]] = field(default_factory=list)
    """``(effective Monday, members)``; published weekly until :data:`LAST_UPDATE`."""
    industry: dict[str, list[tuple[date, str]]] = field(default_factory=dict)
    ipo: dict[str, date] = field(default_factory=dict)
    financials: dict[tuple[str, date], date] = field(default_factory=dict)
    """``(ticker, report period) -> pubDate``."""

    def _members_on(self, day: date) -> set[str]:
        current: set[str] = set()
        for effective, members in self.constituent_changes:
            if effective <= day:
                current = members
        return current

    def query_hs300_stocks(self, date: str = "") -> ReplayResult:  # noqa: A002
        failed = self._check("query_hs300_stocks", {"date": date})
        if failed is not None:
            return failed
        day = type(LAST_UPDATE).fromisoformat(date)
        update = min(_monday(day), LAST_UPDATE)
        first = self.constituent_changes[0][0] if self.constituent_changes else update
        if update < first:
            return ReplayResult(["updateDate", "code", "code_name"], [])
        rows = [
            [update.isoformat(), _code(ticker), f"name-{ticker}"]
            for ticker in sorted(self._members_on(update))
        ]
        return ReplayResult(["updateDate", "code", "code_name"], rows)

    def query_stock_industry(self, code: str = "", date: str = "") -> ReplayResult:  # noqa: A002
        failed = self._check("query_stock_industry", {"code": code, "date": date})
        if failed is not None:
            return failed
        day = type(LAST_UPDATE).fromisoformat(date)
        fields = ["updateDate", "code", "code_name", "industry", "industryClassification"]
        rows = []
        for ticker, history in sorted(self.industry.items()):
            if code and _code(ticker) != code:
                continue
            ipo = self.ipo.get(ticker)
            if ipo is not None and ipo > day:
                continue
            known = [(effective, label) for effective, label in history if effective <= day]
            if not known:
                continue
            effective, label = known[-1]
            rows.append(
                [effective.isoformat(), _code(ticker), f"name-{ticker}", label, "证监会行业分类"]
            )
        return ReplayResult(fields, rows)

    def query_stock_basic(self, code: str = "", code_name: str = "") -> ReplayResult:
        failed = self._check("query_stock_basic", {"code": code})
        if failed is not None:
            return failed
        fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]
        ticker = _ticker(code)
        if ticker not in self.ipo:
            return ReplayResult(fields, [])
        return ReplayResult(
            fields, [[code, f"name-{ticker}", self.ipo[ticker].isoformat(), "", "1", "1"]]
        )

    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "d",
        adjustflag: str = "3",
    ) -> ReplayResult:
        ticker = _ticker(code)
        if ticker not in self.ipo:
            return super().query_history_k_data_plus(
                code, fields, start_date, end_date, frequency, adjustflag
            )
        params = {
            "code": code,
            "start_date": start_date,
            "end_date": end_date,
            "adjustflag": adjustflag,
        }
        failed = self._check("query_history_k_data_plus", params)
        if failed is not None:
            return failed
        names = fields.split(",")
        start = max(date.fromisoformat(start_date or "1990-01-01"), self.ipo[ticker])
        end = min(date.fromisoformat(end_date or "2099-01-01"), date(2026, 9, 30))
        factor = 10 if adjustflag == "1" else 1
        rows: list[list[str]] = []
        day = start
        while day <= end:
            if day.weekday() < 5:
                price = f"{(100 + day.toordinal() % 50) * factor:.4f}"
                values: dict[str, Any] = {
                    "date": day.isoformat(),
                    "code": code,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": "1000",
                    "amount": "100000",
                    "adjustflag": adjustflag,
                    "turn": "0.5",
                    "tradestatus": "1",
                    "peTTM": "20",
                    "pbMRQ": "3",
                    "psTTM": "5",
                    "isST": "0",
                }
                rows.append([str(values.get(name, "")) for name in names])
            day += timedelta(days=1)
        return ReplayResult(names, rows)

    def _quarterly(
        self, method: str, code: str, year: int | None, quarter: int | None
    ) -> ReplayResult:
        ticker = _ticker(code)
        assert year is not None and quarter is not None
        period = date(year, quarter * 3, 30 if quarter in (2, 3) else 31)
        pub = self.financials.get((ticker, period))
        if pub is None:
            return super()._quarterly(method, code, year, quarter)
        params = {"code": code, "year": year, "quarter": quarter}
        failed = self._check(method, params)
        if failed is not None:
            return failed
        fields = self.quarterly_fields[method]
        row = []
        for name in fields:
            if name == "code":
                row.append(code)
            elif name == "pubDate":
                row.append(pub.isoformat())
            elif name == "statDate":
                row.append(period.isoformat())
            else:
                row.append("1.5")
        return ReplayResult(fields, [row])

"""baostock breadth data: daily bars and quarterly financials (P1-A7, D6).

Reuses the session / lock / retry / transient-classification machinery of
:class:`~investment_agent.providers.baostock_reference.BaostockReferenceClient`;
wrap many calls in ``async with client.session():`` so one login serves them.

Daily bars (``query_history_k_data_plus``): ``adjust="none"`` maps to
``adjustflag="3"`` (raw), ``"backward"`` to ``"1"`` (后复权). Forward-adjusted
prices (``"2"``) are never requested. Every row carries ``peTTM`` / ``pbMRQ``
/ ``psTTM`` (times), ``turn`` (percent, converted to a fraction here),
``tradestatus`` (1 trading, 0 suspended) and ``isST``. A suspended day is a
real row with OHLC = previous close and empty volume / amount / turnover; it
is kept with volume 0 and ``trade_status=0``.

Quarterly financials: profit, balance, cash-flow and growth answers for one
``(year, quarter)`` are merged into one canonical record using the baostock
entries of the field registry (:mod:`investment_agent.research.ifind_mapping`).
An undisclosed period answers with no rows -> ``None``. Empty strings mean
"missing" throughout.

Point-in-time guards: a bar dated after the capture date (Asia/Shanghai) or a
``pubDate`` after the capture date raises :class:`LookaheadDataError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

import structlog

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.providers.baostock_reference import (
    PROVIDER,
    BaostockBackend,
    BaostockReferenceClient,
    BaostockResultSet,
    _row_ticker,
    to_baostock_code,
)
from investment_agent.providers.errors import LookaheadDataError, ProviderDataError
from investment_agent.research.ifind_mapping import BAOSTOCK_FIELD_MAPPINGS, FieldMapping

log = structlog.get_logger(__name__)

AdjustKind = Literal["none", "backward"]
ADJUST_FLAGS: dict[AdjustKind, str] = {"none": "3", "backward": "1"}
# Flags baostock may label rows with for each requested adjustment. A backward
# (后复权) series is anchored at the listing date, so bars before the first
# corporate action carry factor 1 and come back labelled "3" (unadjusted) --
# they are numerically identical to the raw bars. Forward ("2") never is valid.
ACCEPTED_ROW_FLAGS: dict[AdjustKind, frozenset[str]] = {
    "none": frozenset({"3"}),
    "backward": frozenset({"1", "3"}),
}

KDATA_FIELDS: tuple[str, ...] = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adjustflag",
    "turn",
    "tradestatus",
    "peTTM",
    "pbMRQ",
    "psTTM",
    "isST",
)
KDATA_OPERATION = "query_history_k_data_plus"

FinancialTable = Literal["profit", "balance", "cash_flow", "growth"]
FINANCIAL_QUERIES: dict[FinancialTable, str] = {
    "profit": "query_profit_data",
    "balance": "query_balance_data",
    "cash_flow": "query_cash_flow_data",
    "growth": "query_growth_data",
}
_QUARTER_ENDS = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_PERCENT_MARKER = "percent"


class BaostockDataBackend(BaostockBackend, Protocol):
    def query_history_k_data_plus(
        self,
        code: str,
        fields: str,
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "d",
        adjustflag: str = "3",
    ) -> BaostockResultSet: ...

    def query_profit_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> BaostockResultSet: ...

    def query_balance_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> BaostockResultSet: ...

    def query_cash_flow_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> BaostockResultSet: ...

    def query_growth_data(
        self, code: str, year: int | None = None, quarter: int | None = None
    ) -> BaostockResultSet: ...

    def query_stock_basic(self, code: str = "", code_name: str = "") -> BaostockResultSet: ...


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


STOCK_BASIC_OPERATION = "query_stock_basic"


@dataclass(frozen=True)
class StockBasic:
    """``query_stock_basic``: listing (IPO) and delisting dates of one security."""

    ticker: str
    name: str
    ipo_date: date | None
    out_date: date | None
    """Delisting date; ``None`` while listed."""
    listed: bool


@dataclass(frozen=True)
class DailyBar:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal | None
    turnover: Decimal | None
    """Fraction of float shares traded (baostock ``turn`` percent ÷ 100)."""
    trade_status: int | None
    is_st: bool | None
    pe_ttm: Decimal | None
    pb_mrq: Decimal | None
    ps_ttm: Decimal | None

    @property
    def suspended(self) -> bool:
        return self.trade_status == 0


@dataclass(frozen=True)
class DailyBarSeries:
    ticker: str
    adjust: AdjustKind
    start: date
    end: date
    bars: tuple[DailyBar, ...]
    skipped: tuple[str, ...]
    """Dates of rows that could not be parsed (missing OHLC)."""
    captured_at: datetime
    raw_rows: tuple[Mapping[str, str], ...] = field(repr=False)
    source: str = PROVIDER


@dataclass(frozen=True)
class QuarterlyFinancials:
    ticker: str
    report_period: date
    pub_date: date
    """Actual disclosure date (latest ``pubDate`` among the four tables)."""
    stat_date: date
    fields: dict[str, float | None]
    """Canonical fields from the baostock registry entries."""
    source_values: dict[str, str]
    """Every raw value as ``<table>.<field>`` (empty string = missing)."""
    tables: tuple[FinancialTable, ...]
    """Tables that answered with a row."""
    captured_at: datetime
    raw_rows: Mapping[str, Mapping[str, str]] = field(repr=False)
    source: str = PROVIDER

    @property
    def missing_fields(self) -> list[str]:
        return sorted(name for name, value in self.fields.items() if value is None)


def report_period_for(year: int, quarter: int) -> date:
    if quarter not in _QUARTER_ENDS:
        raise ValueError(f"quarter must be 1-4: {quarter!r}")
    month, day = _QUARTER_ENDS[quarter]
    return date(year, month, day)


def quarter_of(report_period: date) -> tuple[int, int]:
    for quarter, month_day in _QUARTER_ENDS.items():
        if (report_period.month, report_period.day) == month_day:
            return report_period.year, quarter
    raise ValueError(f"not a quarter end: {report_period.isoformat()}")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _decimal(raw: str | None) -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _float(raw: str | None) -> float | None:
    value = _decimal(raw)
    return float(value) if value is not None else None


def _int(raw: str | None) -> int | None:
    value = _decimal(raw)
    return int(value) if value is not None else None


def _parse_date(raw: str, *, operation: str, name: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ProviderDataError(
            f"baostock {operation} returned an invalid {name} {raw!r}", provider=PROVIDER
        ) from exc


def parse_daily_bar(row: Mapping[str, str]) -> DailyBar | None:
    """One k-data row -> :class:`DailyBar`; ``None`` when OHLC is missing."""

    trade_date = _parse_date(row.get("date", ""), operation=KDATA_OPERATION, name="date")
    prices = [_decimal(row.get(name)) for name in ("open", "high", "low", "close")]
    if any(price is None for price in prices):
        return None
    open_, high, low, close = (price for price in prices if price is not None)
    trade_status = _int(row.get("tradestatus"))
    volume = _decimal(row.get("volume"))
    if volume is None:
        if trade_status != 0:
            return None
        volume = Decimal(0)  # suspended: baostock leaves volume empty
    turn = _decimal(row.get("turn"))
    is_st = _int(row.get("isST"))
    return DailyBar(
        trade_date=trade_date,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=_decimal(row.get("amount")),
        turnover=turn / 100 if turn is not None else None,
        trade_status=trade_status,
        is_st=bool(is_st) if is_st is not None else None,
        pe_ttm=_decimal(row.get("peTTM")),
        pb_mrq=_decimal(row.get("pbMRQ")),
        ps_ttm=_decimal(row.get("psTTM")),
    )


def _convert(mapping: FieldMapping, raw: str | None) -> float | None:
    value = _float(raw)
    if value is None:
        return None
    return value / 100.0 if _PERCENT_MARKER in mapping.unit_or_formula else value


def canonical_baostock_financials(
    rows: Mapping[FinancialTable, Mapping[str, str]],
    mappings: tuple[FieldMapping, ...] = BAOSTOCK_FIELD_MAPPINGS,
) -> dict[str, float | None]:
    """Registry-driven extraction of the canonical financial fields."""

    by_query = {query: table for table, query in FINANCIAL_QUERIES.items()}
    fields: dict[str, float | None] = {}
    for mapping in mappings:
        if mapping.payload_type != "financial" or mapping.status != "direct":
            continue
        table = by_query[mapping.endpoint]
        (source_field,) = mapping.source_fields
        fields[mapping.canonical_field] = _convert(mapping, rows.get(table, {}).get(source_field))
    return fields


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class BaostockMarketClient(BaostockReferenceClient):
    """Reference data plus daily bars and quarterly financials."""

    async def fetch_daily_bars(
        self, symbol: str, start: date, end: date, adjust: AdjustKind
    ) -> DailyBarSeries:
        """Daily bars of ``symbol`` in ``[start, end]`` (one query, any length)."""

        if adjust not in ADJUST_FLAGS:
            raise ValueError(f"unsupported adjust {adjust!r}; use 'none' or 'backward'")
        if start > end:
            raise ValueError(f"start {start} is after end {end}")
        code = to_baostock_code(symbol)
        flag = ADJUST_FLAGS[adjust]

        def query(backend: BaostockBackend) -> BaostockResultSet:
            data_backend: BaostockDataBackend = backend  # type: ignore[assignment]
            return data_backend.query_history_k_data_plus(
                code,
                ",".join(KDATA_FIELDS),
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag=flag,
            )

        raw = await self._run(KDATA_OPERATION, query)
        captured_at = self._now()
        capture_date = captured_at.astimezone(SHANGHAI_TZ).date()

        bars: dict[date, DailyBar] = {}
        skipped: list[str] = []
        for row in raw.rows:
            if _row_ticker(row, operation=KDATA_OPERATION) != symbol.upper():
                raise ProviderDataError(
                    f"baostock {KDATA_OPERATION} answered {row.get('code')!r} for {symbol}",
                    provider=PROVIDER,
                )
            if row.get("adjustflag", flag) not in ACCEPTED_ROW_FLAGS[adjust]:
                raise ProviderDataError(
                    f"baostock {KDATA_OPERATION} answered adjustflag {row.get('adjustflag')!r} "
                    f"for requested {flag}",
                    provider=PROVIDER,
                )
            bar = parse_daily_bar(row)
            if bar is None:
                skipped.append(row.get("date", ""))
                continue
            if bar.trade_date > capture_date:
                raise LookaheadDataError(
                    f"baostock {KDATA_OPERATION} returned a bar dated "
                    f"{bar.trade_date.isoformat()} after the capture date "
                    f"{capture_date.isoformat()}",
                    provider=PROVIDER,
                )
            if start <= bar.trade_date <= end:
                bars[bar.trade_date] = bar
        log.debug(
            "baostock_daily_bars_fetched",
            symbol=symbol,
            adjust=adjust,
            start=start.isoformat(),
            end=end.isoformat(),
            bars=len(bars),
            skipped=len(skipped),
        )
        return DailyBarSeries(
            ticker=symbol.upper(),
            adjust=adjust,
            start=start,
            end=end,
            bars=tuple(bars[day] for day in sorted(bars)),
            skipped=tuple(skipped),
            captured_at=captured_at,
            raw_rows=raw.rows,
        )

    async def fetch_quarterly_financials(
        self, symbol: str, year: int, quarter: int
    ) -> QuarterlyFinancials | None:
        """Merged profit / balance / cash-flow / growth answer; ``None`` if undisclosed.

        The profit table is queried first: when it is empty the period is not
        disclosed and the other three tables are not requested.
        """

        report_period = report_period_for(year, quarter)
        code = to_baostock_code(symbol)
        rows: dict[FinancialTable, Mapping[str, str]] = {}
        async with self.session():
            for table, method in FINANCIAL_QUERIES.items():

                def query(backend: BaostockBackend, method: str = method) -> BaostockResultSet:
                    return getattr(backend, method)(code=code, year=year, quarter=quarter)  # type: ignore[no-any-return]

                raw = await self._run(method, query)
                if len(raw.rows) > 1:
                    raise ProviderDataError(
                        f"baostock {method} returned {len(raw.rows)} rows for "
                        f"{symbol} {year}Q{quarter}",
                        provider=PROVIDER,
                    )
                if raw.rows:
                    rows[table] = raw.rows[0]
                elif table == "profit":
                    return None
        captured_at = self._now()
        capture_date = captured_at.astimezone(SHANGHAI_TZ).date()

        pub_dates: set[date] = set()
        for table, row in rows.items():
            operation = FINANCIAL_QUERIES[table]
            if _row_ticker(row, operation=operation) != symbol.upper():
                raise ProviderDataError(
                    f"baostock {operation} answered {row.get('code')!r} for {symbol}",
                    provider=PROVIDER,
                )
            stat_date = _parse_date(row.get("statDate", ""), operation=operation, name="statDate")
            if stat_date != report_period:
                raise ProviderDataError(
                    f"baostock {operation} statDate {stat_date.isoformat()} does not match "
                    f"the requested period {report_period.isoformat()}",
                    provider=PROVIDER,
                )
            pub_date = _parse_date(row.get("pubDate", ""), operation=operation, name="pubDate")
            if pub_date > capture_date:
                raise LookaheadDataError(
                    f"baostock {operation} pubDate {pub_date.isoformat()} is after the "
                    f"capture date {capture_date.isoformat()}",
                    provider=PROVIDER,
                )
            pub_dates.add(pub_date)
        if len(pub_dates) > 1:
            log.warning(
                "baostock_financial_pubdate_mismatch",
                symbol=symbol,
                period=report_period.isoformat(),
                pub_dates=sorted(d.isoformat() for d in pub_dates),
            )
        source_values = {
            f"{table}.{name}": value
            for table, row in rows.items()
            for name, value in row.items()
            if name not in {"code", "pubDate", "statDate"}
        }
        return QuarterlyFinancials(
            ticker=symbol.upper(),
            report_period=report_period,
            # Conservative: a table published later than the others gates the record.
            pub_date=max(pub_dates),
            stat_date=report_period,
            fields=canonical_baostock_financials(rows),
            source_values=dict(sorted(source_values.items())),
            tables=tuple(rows),
            captured_at=captured_at,
            raw_rows={table: dict(row) for table, row in rows.items()},
        )

    async def fetch_stock_basic(self, symbol: str) -> StockBasic | None:
        """IPO / delisting dates of ``symbol``; ``None`` when baostock does not know it."""

        code = to_baostock_code(symbol)

        def query(backend: BaostockBackend) -> BaostockResultSet:
            data_backend: BaostockDataBackend = backend  # type: ignore[assignment]
            return data_backend.query_stock_basic(code=code)

        raw = await self._run(STOCK_BASIC_OPERATION, query)
        if not raw.rows:
            return None
        if len(raw.rows) > 1:
            raise ProviderDataError(
                f"baostock {STOCK_BASIC_OPERATION} returned {len(raw.rows)} rows for {symbol}",
                provider=PROVIDER,
            )
        row = raw.rows[0]
        if _row_ticker(row, operation=STOCK_BASIC_OPERATION) != symbol.upper():
            raise ProviderDataError(
                f"baostock {STOCK_BASIC_OPERATION} answered {row.get('code')!r} for {symbol}",
                provider=PROVIDER,
            )

        def optional_date(name: str) -> date | None:
            text = row.get(name, "").strip()
            return (
                _parse_date(text, operation=STOCK_BASIC_OPERATION, name=name) if text else None
            )

        return StockBasic(
            ticker=symbol.upper(),
            name=row.get("code_name", "").strip(),
            ipo_date=optional_date("ipoDate"),
            out_date=optional_date("outDate"),
            listed=row.get("status", "").strip() == "1",
        )

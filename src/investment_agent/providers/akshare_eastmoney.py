"""AkShare adapter: Eastmoney (东方财富) financial data (P1-A11, D6 rev 2).

Capabilities (docs/product/p1-plan.md 2.2 / 2.3):

* :meth:`AkshareEastmoneyClient.fetch_period_summaries` - the whole market
  for one report period from three calls (6-9 s each): ``stock_lrb_em``
  (income), ``stock_xjll_em`` (cash flow), ``stock_zcfz_em`` (balance).
* :meth:`AkshareEastmoneyClient.fetch_cash_flow_statement` - one listing's
  full cash-flow statement history (``stock_cash_flow_sheet_by_report_em``,
  16-24 s, ~80-100 periods per call).

Columns and units (verified live 2026-10-07, akshare 1.19.1; locked by
:data:`SUMMARY_COLUMNS` / :data:`CASH_FLOW_COLUMNS`):

==========================  ===================================  =====================
canonical field             source                               unit
==========================  ===================================  =====================
``net_profit_parent``       ``stock_lrb_em`` 净利润               CNY (yuan); 归母净利润
                                                                 (= PARENT_NETPROFIT:
                                                                 600519 FY2024 86.23 bn,
                                                                 601318 126.6 bn)
``revenue_total``           ``stock_lrb_em`` 营业总收入           CNY; banks: 营业收入
``operating_cash_flow``     ``stock_xjll_em``                    CNY
                            经营性现金流-现金流量净额
``debt_ratio``              ``stock_zcfz_em`` 资产负债率          **percent** -> ÷ 100
                                                                 (= 负债-总负债 / 资产-
                                                                 总资产 × 100, checked on
                                                                 all 5243 FY2024 rows)
``total_assets``            ``stock_zcfz_em`` 资产-总资产         CNY
``total_liabilities``       ``stock_zcfz_em`` 负债-总负债         CNY
``total_equity``            ``stock_zcfz_em`` 股东权益合计        CNY, incl. minority
``operating_cash_flow``     cash-flow statement NETCASH_OPERATE  CNY
``capital_expenditure``     CONSTRUCT_LONG_ASSET                 CNY (购建固定资产、无形
                                                                 资产和其他长期资产支付
                                                                 的现金)
``free_cash_flow``          NETCASH_OPERATE - CONSTRUCT_LONG_ASSET  CNY
==========================  ===================================  =====================

Dates. The summaries' 公告日期 is the **latest** notice that touched the
row, not the original disclosure: 600519 FY2024 shows 2026-04-17 (the FY2025
annual report, which restated the comparatives). The per-stock statement has
both ``NOTICE_DATE`` (original disclosure) and ``UPDATE_DATE`` (last
restatement, e.g. 600519 2025H1 NOTICE 2025-08-13, UPDATE 2026-08-15).
Values are always the latest restated figures - **not point-in-time**; the
collector stores every capture as a new version.

Financial stocks (601398 bank, 601318 insurer, 600030 broker, 000001 bank;
FY2024 and 2026H1): all four summary fields and the statement's
NETCASH_OPERATE / CONSTRUCT_LONG_ASSET are present; only the expense
breakdown columns (销售费用 / 管理费用 / 应收账款 / 存货 ...) are empty, and
they are not used. A field that comes back empty is ``None`` (missing), never
an error. Beijing Stock Exchange listings are excluded by Eastmoney from the
summaries (``TRADE_MARKET_CODE != 069001017``).

Errors: :class:`EastmoneyTransientError` (network, timeout, HTTP 5xx / 429)
is retried; :class:`EastmoneySchemaError` (columns changed, unparsable
payload, inconsistent debt ratio) and :class:`EastmoneyError` (HTTP 4xx) are
terminal. "No data" answers (an undisclosed period: ``result: null``, code
9201; an unknown symbol: no ``hidctype`` on the F10 page) are not errors:
the client returns empty results. Point in time: a notice date after the
capture date (Asia/Shanghai) raises :class:`LookaheadDataError`.

Throttle: calls are serialized and spaced at least ``min_interval_seconds``
(default 1 s) apart. ``akshare`` is imported lazily inside
:class:`AkshareEastmoneyBackend`, so offline tests never import it.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from types import ModuleType
from typing import Any, Literal, Protocol

import structlog

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.providers.akshare_shenwan import (
    RawTable,
    _json_value,
    _parse_day,
    _symbol_text,
    _table,
    to_canonical_ticker,
)
from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.errors import (
    LookaheadDataError,
    ProviderDataError,
    ProviderError,
    ProviderTransientError,
    ProviderUnavailableError,
)
from investment_agent.providers.ledger import CallOutcome, ProviderCallEvent, ProviderCallHook

log = structlog.get_logger(__name__)

PROVIDER = "akshare"

SummaryEndpoint = Literal["stock_lrb_em", "stock_xjll_em", "stock_zcfz_em"]
SUMMARY_ENDPOINTS: tuple[SummaryEndpoint, ...] = ("stock_lrb_em", "stock_xjll_em", "stock_zcfz_em")
CASH_FLOW_ENDPOINT = "stock_cash_flow_sheet_by_report_em"

# Exact column lists of akshare 1.19.1. AkShare renames Eastmoney's columns by
# position, so any drift (new / reordered upstream columns, another akshare
# version) must stop the collector instead of mislabelling numbers.
SUMMARY_COLUMNS: dict[SummaryEndpoint, tuple[str, ...]] = {
    "stock_lrb_em": (
        "序号", "股票代码", "股票简称", "净利润", "净利润同比", "营业总收入", "营业总收入同比",
        "营业总支出-营业支出", "营业总支出-销售费用", "营业总支出-管理费用",
        "营业总支出-财务费用", "营业总支出-营业总支出", "营业利润", "利润总额", "公告日期",
    ),
    "stock_xjll_em": (
        "序号", "股票代码", "股票简称", "净现金流-净现金流", "净现金流-同比增长",
        "经营性现金流-现金流量净额", "经营性现金流-净现金流占比", "投资性现金流-现金流量净额",
        "投资性现金流-净现金流占比", "融资性现金流-现金流量净额", "融资性现金流-净现金流占比",
        "公告日期",
    ),
    "stock_zcfz_em": (
        "序号", "股票代码", "股票简称", "资产-货币资金", "资产-应收账款", "资产-存货",
        "资产-总资产", "资产-总资产同比", "负债-应付账款", "负债-预收账款", "负债-总负债",
        "负债-总负债同比", "资产负债率", "股东权益合计", "公告日期",
    ),
}
SUMMARY_CODE_COLUMN = "股票代码"
SUMMARY_NOTICE_COLUMN = "公告日期"


@dataclass(frozen=True)
class SummaryField:
    canonical: str
    endpoint: SummaryEndpoint
    column: str
    percent: bool = False


# Canonical fields of the period summaries (the only extraction table).
SUMMARY_FIELDS: tuple[SummaryField, ...] = (
    SummaryField("net_profit_parent", "stock_lrb_em", "净利润"),
    SummaryField("revenue_total", "stock_lrb_em", "营业总收入"),
    SummaryField("operating_cash_flow", "stock_xjll_em", "经营性现金流-现金流量净额"),
    SummaryField("debt_ratio", "stock_zcfz_em", "资产负债率", percent=True),
    SummaryField("total_assets", "stock_zcfz_em", "资产-总资产"),
    SummaryField("total_liabilities", "stock_zcfz_em", "负债-总负债"),
    SummaryField("total_equity", "stock_zcfz_em", "股东权益合计"),
)
# Key summary fields (P1-A13 "free source missing" trigger when absent).
SUMMARY_KEY_FIELDS: tuple[str, ...] = (
    "net_profit_parent",
    "revenue_total",
    "operating_cash_flow",
    "debt_ratio",
)

CASH_FLOW_COLUMNS = frozenset(
    {
        "SECUCODE",
        "SECURITY_CODE",
        "REPORT_DATE",
        "NOTICE_DATE",
        "UPDATE_DATE",
        "CURRENCY",
        "NETCASH_OPERATE",
        "CONSTRUCT_LONG_ASSET",
    }
)
CASH_FLOW_KEPT_COLUMNS: tuple[str, ...] = (
    "SECUCODE",
    "SECURITY_CODE",
    "REPORT_DATE",
    "REPORT_TYPE",
    "NOTICE_DATE",
    "UPDATE_DATE",
    "CURRENCY",
    "NETCASH_OPERATE",
    "CONSTRUCT_LONG_ASSET",
)

# Share of rows whose 资产负债率 must match 负债-总负债 / 资产-总资产 × 100
# (within DEBT_RATIO_CHECK_POINTS percentage points): a positional mislabel
# shows up here first.
DEBT_RATIO_CHECK_SHARE = 0.95
DEBT_RATIO_CHECK_POINTS = 0.5

# AkShare modules whose module-level ``requests`` is patched during a call.
_AKSHARE_MODULES = (
    "akshare.stock_feature.stock_report_em",
    "akshare.stock_feature.stock_three_report_em",
)
_PATCH_LOCK = threading.Lock()
_F10_INDEX_PATH = "NewFinanceAnalysis/Index"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class EastmoneyError(ProviderError):
    """Terminal AkShare / Eastmoney failure (HTTP 4xx)."""


class EastmoneyTransientError(ProviderTransientError):
    """Network / timeout / HTTP 5xx / 429; worth retrying later."""


class EastmoneySchemaError(ProviderDataError):
    """The answer no longer has the expected shape (site or akshare change)."""


class _NoData(Exception):  # noqa: N818 - internal control flow, never escapes
    """Eastmoney answered "nothing for this request" (not an error)."""


# --------------------------------------------------------------------------- #
# Ticker mapping (shared with the Shenwan adapter)
# --------------------------------------------------------------------------- #


def to_eastmoney_symbol(ticker: str) -> str:
    """``600519.SH`` -> ``SH600519`` (F10 statement pages)."""

    code, _, exchange = ticker.strip().upper().partition(".")
    if len(code) != 6 or not code.isdigit() or exchange not in {"SH", "SZ", "BJ"}:
        raise ValueError(f"not a canonical A-share ticker: {ticker!r}")
    return f"{exchange}{code}"


# --------------------------------------------------------------------------- #
# requests shim (timeouts, typed errors, "no data" detection)
# --------------------------------------------------------------------------- #


class _Response:
    def __init__(self, real: Any, url: str) -> None:
        self._real = real
        self._url = url

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    @property
    def text(self) -> str:
        text = str(self._real.text)
        if _F10_INDEX_PATH in self._url and "hidctype" not in text:
            raise _NoData(f"Eastmoney F10 has no company page for {self._url}")
        return text

    def json(self, *args: Any, **kwargs: Any) -> Any:
        try:
            payload = self._real.json(*args, **kwargs)
        except ValueError as exc:
            raise EastmoneySchemaError(
                f"Eastmoney answered non-JSON for {self._url}: {exc}", provider=PROVIDER
            ) from exc
        if (
            isinstance(payload, dict)
            and "result" in payload
            and payload.get("result") is None
            and payload.get("success") is False
        ):
            # datacenter-web: {"result": null, "success": false, "message": "返回数据为空",
            # "code": 9201} for a period nobody has disclosed yet.
            raise _NoData(str(payload.get("message") or "no data"))
        return payload


class _EastmoneyRequests:
    """Stand-in for the ``requests`` module inside the patched AkShare modules."""

    def __init__(self, real: ModuleType, *, timeout: float) -> None:
        self._real = real
        self._timeout = timeout

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def get(self, url: str, *args: Any, **kwargs: Any) -> _Response:
        kwargs.setdefault("timeout", self._timeout)
        exceptions = self._real.exceptions
        try:
            response = self._real.get(url, *args, **kwargs)
        except (exceptions.ConnectionError, exceptions.Timeout) as exc:
            raise EastmoneyTransientError(
                f"network error fetching {url}: {exc}", provider=PROVIDER
            ) from exc
        except exceptions.RequestException as exc:
            raise EastmoneyError(f"request to {url} failed: {exc}", provider=PROVIDER) from exc
        status = int(getattr(response, "status_code", 200))
        if status >= 500 or status == 429:
            raise EastmoneyTransientError(
                f"HTTP {status} from {url}", provider=PROVIDER, error_code=str(status)
            )
        if status >= 400:
            raise EastmoneyError(f"HTTP {status} from {url}", provider=PROVIDER, error_code=str(status))
        return _Response(response, url)


@contextmanager
def eastmoney_requests(modules: Sequence[ModuleType], *, timeout: float = 30.0) -> Iterator[None]:
    """Route ``requests.get`` of ``modules`` through :class:`_EastmoneyRequests`."""

    with _PATCH_LOCK:
        originals = [(module, module.__dict__["requests"]) for module in modules]
        try:
            for module, real in originals:
                setattr(module, "requests", _EastmoneyRequests(real, timeout=timeout))  # noqa: B010
            yield
        finally:
            for module, real in originals:
                setattr(module, "requests", real)  # noqa: B010


# --------------------------------------------------------------------------- #
# Backend (real AkShare or an offline fake)
# --------------------------------------------------------------------------- #


class EastmoneyBackend(Protocol):
    def period_table(self, endpoint: SummaryEndpoint, period: str) -> RawTable | None:
        """``period`` as ``YYYYMMDD``; ``None`` when Eastmoney has no rows for it."""
        ...

    def cash_flow_table(self, symbol: str) -> RawTable | None:
        """``symbol`` as ``SH600519``; ``None`` for an unknown symbol."""
        ...


class AkshareEastmoneyBackend:
    """Calls AkShare inside :func:`eastmoney_requests` (blocking; run in a thread)."""

    def __init__(self, *, http_timeout: float = 30.0) -> None:
        self._http_timeout = http_timeout

    def _call(self, func_name: str, **kwargs: Any) -> RawTable | None:
        import importlib

        import akshare

        modules = [importlib.import_module(name) for name in _AKSHARE_MODULES]
        func = getattr(akshare, func_name)
        with eastmoney_requests(modules, timeout=self._http_timeout):
            try:
                frame = func(**kwargs)
            except _NoData as exc:
                log.info("eastmoney_no_data", operation=func_name, detail=str(exc), **kwargs)
                return None
            except ProviderError:
                raise
            except (ValueError, KeyError, AttributeError, IndexError, TypeError) as exc:
                raise EastmoneySchemaError(
                    f"akshare {func_name} could not parse the answer: {exc!r}",
                    provider=PROVIDER,
                ) from exc
        return _table(frame)

    def period_table(self, endpoint: SummaryEndpoint, period: str) -> RawTable | None:
        return self._call(endpoint, date=period)

    def cash_flow_table(self, symbol: str) -> RawTable | None:
        table = self._call(CASH_FLOW_ENDPOINT, symbol=symbol)
        if table is None:
            return None
        # Keep only what the collector uses (the statement has 250-320 columns).
        kept = [column for column in CASH_FLOW_KEPT_COLUMNS if column in table.columns]
        return RawTable(
            columns=table.columns,
            rows=tuple({column: row.get(column) for column in kept} for row in table.rows),
        )


# --------------------------------------------------------------------------- #
# Results and parsing (pure)
# --------------------------------------------------------------------------- #


def _number(raw: Any) -> float | None:
    value = _json_value(raw)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text or text in {"-", "--", "None", "nan", "NaN"}:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _day(raw: Any, *, field_name: str) -> date | None:
    try:
        return _parse_day(_json_value(raw), field_name=field_name, required=False)
    except ProviderDataError as exc:
        raise EastmoneySchemaError(str(exc), provider=PROVIDER) from exc


@dataclass(frozen=True)
class PeriodSummary:
    ticker: str
    report_period: date
    notice_date: date | None
    """Latest Eastmoney notice touching the row (see module doc)."""
    fields: dict[str, float | None]
    source_values: dict[str, float | None]
    """``<endpoint>.<column>`` -> raw number (percent as published)."""
    endpoints: tuple[SummaryEndpoint, ...]
    """Endpoints that had a row for this listing."""

    @property
    def missing_fields(self) -> list[str]:
        return sorted(name for name, value in self.fields.items() if value is None)


@dataclass(frozen=True)
class PeriodSummaries:
    report_period: date
    captured_at: datetime
    summaries: Mapping[str, PeriodSummary]
    """Canonical ticker -> summary."""
    empty_endpoints: tuple[SummaryEndpoint, ...] = ()
    """Endpoints that answered "no data" (period not disclosed by anybody)."""
    skipped_symbols: tuple[str, ...] = ()
    raw: Mapping[str, RawTable] = field(default_factory=dict, repr=False)

    def raw_rows_for(self, tickers: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """Raw rows of ``tickers`` per endpoint (artifact)."""

        codes = {ticker.split(".")[0] for ticker in tickers}
        return {
            endpoint: [
                dict(row)
                for row in table.rows
                if _symbol_text(row.get(SUMMARY_CODE_COLUMN)) in codes
            ]
            for endpoint, table in self.raw.items()
        }


def _check_columns(table: RawTable, endpoint: SummaryEndpoint) -> None:
    expected = SUMMARY_COLUMNS[endpoint]
    if tuple(table.columns) != expected:
        missing = sorted(set(expected) - set(table.columns))
        extra = sorted(set(table.columns) - set(expected))
        raise EastmoneySchemaError(
            f"akshare {endpoint} columns changed (missing {missing}, unexpected {extra}); "
            f"got {list(table.columns)}",
            provider=PROVIDER,
        )


def _check_debt_ratio(table: RawTable) -> None:
    checked = agreeing = 0
    for row in table.rows:
        ratio = _number(row.get("资产负债率"))
        assets = _number(row.get("资产-总资产"))
        liabilities = _number(row.get("负债-总负债"))
        if ratio is None or assets is None or liabilities is None or assets <= 0:
            continue
        checked += 1
        if abs(liabilities / assets * 100 - ratio) <= DEBT_RATIO_CHECK_POINTS:
            agreeing += 1
    if checked and agreeing / checked < DEBT_RATIO_CHECK_SHARE:
        raise EastmoneySchemaError(
            f"stock_zcfz_em 资产负债率 disagrees with 负债-总负债 / 资产-总资产 on "
            f"{checked - agreeing}/{checked} rows: column mapping drift?",
            provider=PROVIDER,
        )


def parse_period_summaries(
    tables: Mapping[SummaryEndpoint, RawTable | None],
    *,
    report_period: date,
    captured_at: datetime,
) -> PeriodSummaries:
    """Validate the three summary tables and merge them per listing (pure)."""

    capture_date = captured_at.astimezone(SHANGHAI_TZ).date()
    by_ticker: dict[str, dict[SummaryEndpoint, Mapping[str, Any]]] = {}
    notices: dict[str, date | None] = {}
    skipped: set[str] = set()
    empty: list[SummaryEndpoint] = []
    raw: dict[str, RawTable] = {}
    for endpoint in SUMMARY_ENDPOINTS:
        table = tables.get(endpoint)
        if table is None or not table.rows:
            empty.append(endpoint)
            continue
        _check_columns(table, endpoint)
        if endpoint == "stock_zcfz_em":
            _check_debt_ratio(table)
        raw[endpoint] = table
        for row in table.rows:
            symbol = _symbol_text(row.get(SUMMARY_CODE_COLUMN))
            ticker = to_canonical_ticker(symbol)
            if ticker is None:
                skipped.add(symbol)
                continue
            notice = _day(row.get(SUMMARY_NOTICE_COLUMN), field_name=f"{endpoint}.公告日期")
            if notice is not None and notice > capture_date:
                raise LookaheadDataError(
                    f"akshare {endpoint}: {ticker} notice date {notice.isoformat()} is after "
                    f"the capture date {capture_date.isoformat()}",
                    provider=PROVIDER,
                )
            rows = by_ticker.setdefault(ticker, {})
            previous = rows.get(endpoint)
            if previous is not None:
                previous_notice = _day(
                    previous.get(SUMMARY_NOTICE_COLUMN), field_name=f"{endpoint}.公告日期"
                )
                if (previous_notice or date.min) > (notice or date.min):
                    continue  # duplicate code: the later notice wins
            rows[endpoint] = row
            current = notices.get(ticker)
            if notice is not None and (current is None or notice > current):
                notices[ticker] = notice
            else:
                notices.setdefault(ticker, current)
    summaries: dict[str, PeriodSummary] = {}
    for ticker, rows in sorted(by_ticker.items()):
        fields: dict[str, float | None] = {}
        source_values: dict[str, float | None] = {}
        for spec in SUMMARY_FIELDS:
            endpoint_row = rows.get(spec.endpoint)
            raw_value = (
                _number(endpoint_row.get(spec.column)) if endpoint_row is not None else None
            )
            source_values[f"{spec.endpoint}.{spec.column}"] = raw_value
            if raw_value is not None and spec.percent:
                raw_value = raw_value / 100.0
            fields[spec.canonical] = raw_value
        summaries[ticker] = PeriodSummary(
            ticker=ticker,
            report_period=report_period,
            notice_date=notices.get(ticker),
            fields=fields,
            source_values=source_values,
            endpoints=tuple(endpoint for endpoint in SUMMARY_ENDPOINTS if endpoint in rows),
        )
    if skipped:
        log.info("eastmoney_symbols_skipped", count=len(skipped), sample=sorted(skipped)[:10])
    return PeriodSummaries(
        report_period=report_period,
        captured_at=captured_at,
        summaries=summaries,
        empty_endpoints=tuple(empty),
        skipped_symbols=tuple(sorted(skipped)),
        raw=raw,
    )


@dataclass(frozen=True)
class CashFlowPeriod:
    report_period: date
    notice_date: date | None
    """Original disclosure of the period."""
    update_date: date | None
    """Last restatement (a later report updated the comparatives)."""
    operating_cash_flow: float | None
    capital_expenditure: float | None
    currency: str | None

    @property
    def free_cash_flow(self) -> float | None:
        if self.operating_cash_flow is None or self.capital_expenditure is None:
            return None
        return self.operating_cash_flow - self.capital_expenditure

    @property
    def restated(self) -> bool:
        return (
            self.notice_date is not None
            and self.update_date is not None
            and self.update_date > self.notice_date
        )

    def fields(self) -> dict[str, float | None]:
        return {
            "operating_cash_flow": self.operating_cash_flow,
            "capital_expenditure": self.capital_expenditure,
            "free_cash_flow": self.free_cash_flow,
        }


@dataclass(frozen=True)
class CashFlowStatement:
    ticker: str
    captured_at: datetime
    periods: Mapping[date, CashFlowPeriod]
    raw_rows: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)

    def raw_rows_for(self, periods: Sequence[date]) -> list[dict[str, Any]]:
        wanted = {period.isoformat() for period in periods}
        return [
            dict(row)
            for row in self.raw_rows
            if str(row.get("REPORT_DATE") or "")[:10] in wanted
        ]


_QUARTER_ENDS = frozenset({(3, 31), (6, 30), (9, 30), (12, 31)})


def parse_cash_flow_statement(
    table: RawTable, *, ticker: str, captured_at: datetime
) -> CashFlowStatement:
    """Validate ``stock_cash_flow_sheet_by_report_em`` rows of one listing (pure)."""

    operation = CASH_FLOW_ENDPOINT
    missing = CASH_FLOW_COLUMNS - set(table.columns)
    if missing:
        raise EastmoneySchemaError(
            f"akshare {operation} answer lacks columns {sorted(missing)}", provider=PROVIDER
        )
    capture_date = captured_at.astimezone(SHANGHAI_TZ).date()
    code = ticker.split(".")[0]
    periods: dict[date, CashFlowPeriod] = {}
    for row in table.rows:
        security = _symbol_text(row.get("SECURITY_CODE"))
        if security != code:
            raise EastmoneySchemaError(
                f"akshare {operation}: asked for {ticker}, got a row of {security}",
                provider=PROVIDER,
            )
        report_period = _day(row.get("REPORT_DATE"), field_name="REPORT_DATE")
        if report_period is None or (report_period.month, report_period.day) not in _QUARTER_ENDS:
            raise EastmoneySchemaError(
                f"akshare {operation}: invalid REPORT_DATE {row.get('REPORT_DATE')!r}",
                provider=PROVIDER,
            )
        notice = _day(row.get("NOTICE_DATE"), field_name="NOTICE_DATE")
        update = _day(row.get("UPDATE_DATE"), field_name="UPDATE_DATE")
        for name, day in (("NOTICE_DATE", notice), ("UPDATE_DATE", update)):
            if day is not None and day > capture_date:
                raise LookaheadDataError(
                    f"akshare {operation}: {ticker} {report_period.isoformat()} {name} "
                    f"{day.isoformat()} is after the capture date {capture_date.isoformat()}",
                    provider=PROVIDER,
                )
        currency = row.get("CURRENCY")
        periods[report_period] = CashFlowPeriod(
            report_period=report_period,
            notice_date=notice,
            update_date=update,
            operating_cash_flow=_number(row.get("NETCASH_OPERATE")),
            capital_expenditure=_number(row.get("CONSTRUCT_LONG_ASSET")),
            currency=str(currency) if currency else None,
        )
    return CashFlowStatement(
        ticker=ticker,
        captured_at=captured_at,
        periods=dict(sorted(periods.items())),
        raw_rows=table.rows,
    )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AkshareEastmoneyClient:
    """Async facade: one AkShare call at a time, spaced and retried."""

    def __init__(
        self,
        backend: EastmoneyBackend | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        min_interval_seconds: float = 1.0,
        timeout_seconds: float = 180.0,
        max_attempts: int = 2,
        retry_backoff_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        circuit: CircuitBreaker | None = None,
        on_call: ProviderCallHook | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        self._backend = backend
        self._circuit = circuit
        self._on_call = on_call
        self._clock = clock
        self.min_interval_seconds = min_interval_seconds
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock: asyncio.Lock | None = None
        self._last_call: float | None = None
        self.calls = 0

    async def fetch_period_summaries(self, report_period: date) -> PeriodSummaries:
        """Whole-market summaries of ``report_period`` (three calls)."""

        backend = self._resolve_backend()
        period = report_period.strftime("%Y%m%d")
        tables: dict[SummaryEndpoint, RawTable | None] = {}
        for endpoint in SUMMARY_ENDPOINTS:
            tables[endpoint] = await self._call(
                f"{endpoint}({period})", partial(backend.period_table, endpoint, period)
            )
        result = parse_period_summaries(
            tables, report_period=report_period, captured_at=self._now()
        )
        log.info(
            "eastmoney_period_summaries_fetched",
            report_period=report_period.isoformat(),
            listings=len(result.summaries),
            empty_endpoints=list(result.empty_endpoints),
        )
        return result

    async def fetch_cash_flow_statement(self, ticker: str) -> CashFlowStatement | None:
        """All reported periods of one listing; ``None`` when Eastmoney has no page."""

        backend = self._resolve_backend()
        symbol = to_eastmoney_symbol(ticker)
        table = await self._call(
            f"{CASH_FLOW_ENDPOINT}({symbol})",
            partial(backend.cash_flow_table, symbol),
            ticker_count=1,
        )
        if table is None or not table.rows:
            return None
        return parse_cash_flow_statement(table, ticker=ticker, captured_at=self._now())

    # -- plumbing ---------------------------------------------------------- #

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    def _resolve_backend(self) -> EastmoneyBackend:
        if self._backend is None:
            self._backend = AkshareEastmoneyBackend()
        return self._backend

    async def _throttle(self) -> None:
        if self._last_call is not None and self.min_interval_seconds > 0:
            wait = self._last_call + self.min_interval_seconds - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
        self._last_call = self._monotonic()

    async def _call[T](
        self, operation: str, call: Callable[[], T], *, ticker_count: int = 0
    ) -> T:
        """One logical call: throttled, retried on transient errors, circuit-guarded.

        The circuit breaker is consulted once per logical call (before the
        first attempt) and told the final outcome once, so in-call retries
        stay as they are and the breaker counts calls that failed for good.
        ``on_call`` (the provider ledger) gets one event per logical call:
        ``ok`` / ``transient`` / ``terminal`` / ``skipped_open``.
        """

        if self._lock is None:
            self._lock = asyncio.Lock()
        endpoint = operation.split("(", 1)[0]
        async with self._lock:
            started = self._monotonic()
            outcome: CallOutcome = "terminal"
            try:
                if self._circuit is not None:
                    self._circuit.check(PROVIDER, endpoint)
                result = await self._attempts(operation, call)
            except ProviderUnavailableError:
                outcome = "skipped_open"
                raise
            except BaseException as exc:
                outcome = "transient" if isinstance(exc, ProviderTransientError) else "terminal"
                if self._circuit is not None:
                    self._circuit.record_failure(PROVIDER, endpoint, exc)
                raise
            else:
                outcome = "ok"
                if self._circuit is not None:
                    self._circuit.record_success(PROVIDER, endpoint)
                return result
            finally:
                await self._report(endpoint, outcome, started, ticker_count)

    async def _report(
        self, endpoint: str, outcome: CallOutcome, started: float, ticker_count: int
    ) -> None:
        if self._on_call is None:
            return
        duration_ms = (
            None
            if outcome == "skipped_open"
            else max(int((self._monotonic() - started) * 1000), 0)
        )
        await self._on_call(
            ProviderCallEvent(PROVIDER, endpoint, outcome, duration_ms, ticker_count)
        )

    async def _attempts[T](self, operation: str, call: Callable[[], T]) -> T:
        attempt = 0
        while True:
            attempt += 1
            await self._throttle()
            self.calls += 1
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(call), timeout=self._timeout_seconds
                )
            except TimeoutError as exc:
                error: EastmoneyTransientError = EastmoneyTransientError(
                    f"akshare {operation} timed out after {self._timeout_seconds}s",
                    provider=PROVIDER,
                )
                error.__cause__ = exc
            except EastmoneyTransientError as exc:
                error = exc
            finally:
                self._last_call = self._monotonic()
            if attempt >= self._max_attempts:
                raise error
            log.warning(
                "akshare_transient_retry",
                operation=operation,
                attempt=attempt,
                error=str(error),
            )
            await self._sleep(self._retry_backoff_seconds * attempt)

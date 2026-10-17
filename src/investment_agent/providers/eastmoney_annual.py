"""Eastmoney annual financial statement adapter (industry peers WP7).

One company-year at a time, from the two Eastmoney endpoints validated in the
research run (``research/industry-data-validation/scripts/round2_extra.py`` /
``round2_analyze.py``; the research scripts' global caches and fixed date
lists are deliberately **not** copied):

* F10 full statements (host ``emweb.securities.eastmoney.com``):
  ``/PC_HSF10/NewFinanceAnalysis/{lrb,zcfzb,xjllb}AjaxNew`` with
  ``companyType`` / ``reportDateType=0`` / ``reportType=1`` / an explicit
  annual ``dates`` value / ``code=SH600519``. ``companyType`` (ctype) comes
  from the F10 page's ``hidctype`` metadata
  (``/PC_HSF10/NewFinanceAnalysis/Index?type=web&code=sh600519``) or from
  caller-supplied stored metadata; when it cannot be confirmed the company
  fails with :class:`CompanyTypeUnknownError` - no default type is guessed and
  no AKShare private function is called.
* Main indicators (host ``datacenter.eastmoney.com``):
  ``/securities/api/data/v1/get`` with ``reportName=RPT_F10_FINANCE_MAINFINADATA``
  and a ``SECUCODE`` + ``REPORT_DATE`` filter, one annual date.

Full statements are requested per explicit annual date (REPORT_DATE filtered)
- never the whole market, never a company's full history. Only these two hosts
are allowed: both the collector (by construction) and the HTTP source
(:class:`HttpEastmoneyAnnualSource.fetch`, defence in depth for injected URLs)
refuse anything else.

Field mapping (docs/product/industry-peer-technical-design.md, 补采字段与接口适配;
locked by :data:`ANNUAL_METRICS`):

=========================  ============================  =====================
canonical metric           source table.field            unit
=========================  ============================  =====================
``operating_revenue``      income ``OPERATE_INCOME``     CNY (yuan)
``net_profit_parent``      income ``PARENT_NETPROFIT``   CNY
``revenue_total_check``    income ``TOTAL_OPERATE_INCOME`` CNY, check-only
``equity_parent``          balance ``TOTAL_PARENT_EQUITY`` CNY
``contract_liabilities``   balance ``CONTRACT_LIAB``     CNY
``operating_cash_flow``    cashflow ``NETCASH_OPERATE``   CNY
``roe_weighted_annual``    main ``ROEJQ``                fraction (percent
                                                         ÷ 100, once; the raw
                                                         percent is kept)
=========================  ============================  =====================

Amounts are CNY and parsed as :class:`decimal.Decimal` straight from the JSON
text (``parse_float=Decimal``) - never through ``float`` - and carried as
decimal strings. ``NaN`` / ``Infinity`` literals are refused at parse time.

Data discipline:

* Missing declared columns (a mapped field's column absent from the row
  structure), HTTP 200 with an HTML body, truncated pagination
  (``result.pages > 1``) and duplicate report periods are
  :class:`EastmoneyAnnualDataError` - **never retried**. Structure damage is
  :class:`EastmoneyAnnualSchemaError`, a subclass, so callers can tell schema
  information apart from value facts.
* An upstream explicit ``null`` is a normal fact: the field is missing with
  ``missing_reason="upstream_null"``. A period nobody disclosed (empty row
  set) makes the table's fields missing with ``"no_statement_row"`` - an
  annual report arriving late is a gap, not an error.
* A ``CURRENCY`` other than CNY is refused (amounts must not be labelled CNY).
* Revision dates travel as plain metadata (``SourceRowMeta.update_date``):
  a different REPORT_DATE/UPDATE_DATE is not a version conflict - the
  adapter carries the source version marks through and the upper layers
  (WP4/WP8) decide; different versions are never stitched together.

Behaviour (docs/product/industry-peer-implementation-plan.md §WP7): initial
timeout 30 s (``DEFAULT_TIMEOUT_SECONDS``); one callable per company (serial
scheduling across companies belongs to WP8); transient failures (network /
timeout / 5xx) retried at most ``max_retries`` = 2 times (``settings.peer_collect_retries``);
HTTP 429 sleeps the ``Retry-After`` it announced (seconds or HTTP date,
otherwise the backoff) before retrying; every attempt - and every reused
saved response - is recorded in :attr:`EastmoneyAnnualCollector.request_log`
(url, params, status, outcome, response sha256, Retry-After), and every
logical request is reported once to ``on_call`` (the provider-call ledger
convention) and guarded by the optional :class:`CircuitBreaker`. Before each
request an injected :class:`AnnualResponseCache` may return an already saved
response body for the identical planned request; it is then parsed without
any HTTP call (WP8 owns the coverage/version check behind it).

Tests inject a fake :class:`EastmoneyAnnualSource`; nothing here imports
akshare or reaches the network in the suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import structlog

from investment_agent.providers.akshare_eastmoney import to_eastmoney_symbol
from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.errors import (
    ProviderDataError,
    ProviderError,
    ProviderTransientError,
)
from investment_agent.providers.ledger import ProviderCallHook
from investment_agent.providers.transport import (
    Attempt,
    ProviderCallPipeline,
    RetryPolicy,
    Reused,
    Throttle,
    classify_errors,
)

log = structlog.get_logger(__name__)

PROVIDER = "eastmoney"
ADAPTER_VERSION = "1.0"

DEFAULT_TIMEOUT_SECONDS = 30.0
"""Initial request timeout (plan §WP7)."""
DEFAULT_MAX_RETRIES = 2
"""Transient retries per logical request (``settings.peer_collect_retries``)."""

F10_HOST = "emweb.securities.eastmoney.com"
DATACENTER_HOST = "datacenter.eastmoney.com"
ALLOWED_HOSTS = frozenset({F10_HOST, DATACENTER_HOST})
"""The only hosts this adapter may talk to (hard allow-list)."""

F10_BASE_URL = f"https://{F10_HOST}"
DATACENTER_BASE_URL = f"https://{DATACENTER_HOST}"
CTYPE_PATH = "/PC_HSF10/NewFinanceAnalysis/Index"
MAIN_FINADATA_PATH = "/securities/api/data/v1/get"
MAIN_FINADATA_REPORT = "RPT_F10_FINANCE_MAINFINADATA"

SourceTable = Literal["income", "balance", "cashflow", "main"]
STATEMENT_PATHS: dict[SourceTable, str] = {
    "income": "/PC_HSF10/NewFinanceAnalysis/lrbAjaxNew",
    "balance": "/PC_HSF10/NewFinanceAnalysis/zcfzbAjaxNew",
    "cashflow": "/PC_HSF10/NewFinanceAnalysis/xjllbAjaxNew",
}
STATEMENT_TABLES: tuple[SourceTable, ...] = ("income", "balance", "cashflow")

CURRENCY_CNY = "CNY"
PERCENT = Decimal(100)

OPERATION_CTYPE = "f10_ctype"
OPERATIONS: dict[SourceTable, str] = {
    "income": "f10_income",
    "balance": "f10_balance",
    "cashflow": "f10_cashflow",
    "main": "main_fina",
}


@dataclass(frozen=True)
class AnnualMetricSpec:
    """One line of the design's field-mapping table."""

    metric: str
    source_table: SourceTable
    source_field: str
    unit: str
    percent: bool = False
    """Upstream publishes a percent; the canonical value is percent ÷ 100 (once)."""
    check_only: bool = False
    """Consistency-check value only (never a ratio denominator upstream of WP5)."""


ANNUAL_METRICS: tuple[AnnualMetricSpec, ...] = (
    AnnualMetricSpec("operating_revenue", "income", "OPERATE_INCOME", "CNY"),
    AnnualMetricSpec("net_profit_parent", "income", "PARENT_NETPROFIT", "CNY"),
    AnnualMetricSpec(
        "revenue_total_check", "income", "TOTAL_OPERATE_INCOME", "CNY", check_only=True
    ),
    AnnualMetricSpec("equity_parent", "balance", "TOTAL_PARENT_EQUITY", "CNY"),
    AnnualMetricSpec("contract_liabilities", "balance", "CONTRACT_LIAB", "CNY"),
    AnnualMetricSpec("operating_cash_flow", "cashflow", "NETCASH_OPERATE", "CNY"),
    AnnualMetricSpec("roe_weighted_annual", "main", "ROEJQ", "fraction", percent=True),
)

METRICS_BY_TABLE: dict[SourceTable, tuple[AnnualMetricSpec, ...]] = {
    table: tuple(spec for spec in ANNUAL_METRICS if spec.source_table == table)
    for table in ("income", "balance", "cashflow", "main")
}

_ROW_META_COLUMNS = ("SECUCODE", "REPORT_DATE", "CURRENCY")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class EastmoneyAnnualError(ProviderError):
    """Terminal Eastmoney annual failure (HTTP 4xx, foreign host, misuse)."""


class EastmoneyAnnualTransientError(ProviderTransientError):
    """Network / timeout / HTTP 5xx / 429; worth retrying."""


class EastmoneyAnnualDataError(ProviderDataError):
    """The answer cannot be trusted as data (duplicates, bad values, currency)."""


class EastmoneyAnnualSchemaError(EastmoneyAnnualDataError):
    """The declared structure is damaged: 200-with-HTML, a missing declared
    column, truncated pagination, a row for the wrong company or period.

    Distinct from a value fact: the caller can tell "the schema says this
    column should exist and it does not" apart from "the column exists and the
    upstream left it empty". Never retried.
    """


class CompanyTypeUnknownError(ProviderError):
    """The company's ``ctype`` could not be confirmed: this company is not
    collectible. No default type is guessed; the caller may retry with stored,
    sourced metadata."""

    def __init__(self, message: str, *, ticker: str) -> None:
        super().__init__(message, provider=PROVIDER, error_code="company_type_unknown")
        self.ticker = ticker


# --------------------------------------------------------------------------- #
# Raw source protocol (injectable fake) and the HTTP implementation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawAnnualResponse:
    """Transport-level facts of one HTTP answer; policy lives in the collector."""

    status: int
    body: bytes
    retry_after: str | None = None
    """Raw ``Retry-After`` header (seconds or HTTP date) when upstream sent one."""


class EastmoneyAnnualSource(Protocol):
    """The replaceable, fake-injectable transport of one HTTP GET."""

    async def fetch(self, url: str, params: Mapping[str, str]) -> RawAnnualResponse:
        """GET ``url`` with ``params``; the allow-list check happens here too.

        Network / timeout failures raise :class:`EastmoneyAnnualTransientError`;
        HTTP statuses are returned as-is for the collector's retry policy.
        """
        ...


USER_AGENT = "Mozilla/5.0"


class HttpEastmoneyAnnualSource:
    """Real transport: ``httpx`` with the 30 s initial timeout, allow-listed."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def fetch(self, url: str, params: Mapping[str, str]) -> RawAnnualResponse:
        host = (urlsplit(url).hostname or "").lower()
        if host not in ALLOWED_HOSTS:
            raise EastmoneyAnnualError(
                f"eastmoney annual adapter only allows {sorted(ALLOWED_HOSTS)}; "
                f"refusing {host!r} ({url})",
                provider=PROVIDER,
                error_code="host_not_allowed",
            )
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise EastmoneyAnnualTransientError(
                f"eastmoney annual request timed out after {self._timeout_seconds}s: {url}",
                provider=PROVIDER,
            ) from exc
        except httpx.HTTPError as exc:
            raise EastmoneyAnnualTransientError(
                f"eastmoney annual request failed: {type(exc).__name__}: {exc}",
                provider=PROVIDER,
            ) from exc
        return RawAnnualResponse(
            status=response.status_code,
            body=response.content,
            retry_after=response.headers.get("Retry-After"),
        )


class AnnualResponseCache(Protocol):
    """Reuse of already saved raw responses (plan §WP7: don't re-request when a
    saved response covers the same request at the same source version).

    Implementations decide coverage/version identity; the collector only
    requires that a returned body belongs to the byte-identical planned
    request. WP8 provides the real implementation over saved artifacts.
    """

    def saved_response(self, operation: str, params: Mapping[str, str]) -> bytes | None:
        """Saved body for this exact planned request, or ``None`` to fetch."""
        ...


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #


def _reject_constant(name: str) -> None:
    raise EastmoneyAnnualDataError(
        f"eastmoney annual answer contains the non-finite literal {name}",
        provider=PROVIDER,
    )


def parse_json_body(body: bytes, *, operation: str) -> Any:
    """Decode the answer as JSON, decimals kept exact (never through float)."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation} answered non-UTF-8 bytes: {exc}", provider=PROVIDER
        ) from exc
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except ValueError as exc:
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation} answered non-JSON (an HTML page?): {exc}",
            provider=PROVIDER,
        ) from exc


def _row_date(raw: Any, *, field_name: str, operation: str) -> date:
    text = str(raw).strip() if raw is not None else ""
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise EastmoneyAnnualSchemaError(
                f"eastmoney {operation}: invalid {field_name} {raw!r}", provider=PROVIDER
            ) from exc


def _optional_row_date(raw: Any, *, field_name: str, operation: str) -> date | None:
    if raw is None or str(raw).strip() == "":
        return None
    return _row_date(raw, field_name=field_name, operation=operation)


def _decimal_value(raw: Any, *, field_name: str, operation: str) -> Decimal | None:
    """One amount/percent cell: Decimal (exact) or ``None`` (upstream null)."""

    if raw is None:
        return None
    # bools are ints; floats have already been through float() - refuse both.
    if isinstance(raw, bool | float) or not isinstance(raw, int | Decimal):
        raise EastmoneyAnnualDataError(
            f"eastmoney {operation}: {field_name} is not an exact number: {raw!r}",
            provider=PROVIDER,
        )
    return Decimal(raw)


def _decimal_string(value: Decimal) -> str:
    """Exact decimal string of a parsed value (no float round trip)."""

    return str(value)


@dataclass(frozen=True)
class SourceRowMeta:
    """One source row's identity and revision marks, carried through as-is."""

    source_table: SourceTable
    report_period: date
    notice_date: date | None
    update_date: date | None
    """Last restatement date; a different value here is metadata, not a conflict."""
    currency: str
    response_hash: str
    source_version: str | None = None
    """Opaque upstream version, when supplied; never inferred from fetch time."""


@dataclass(frozen=True)
class ParsedStatement:
    """The one wanted row of one table for one annual period (or its absence)."""

    row: SourceRowMeta | None
    values: Mapping[str, Decimal | None] = field(default_factory=dict)
    """Mapped source field -> exact value; ``None`` = upstream explicit null."""


NO_ROW = ParsedStatement(row=None)


def _check_row_identity(
    row: Mapping[str, Any],
    *,
    secucode: str,
    wanted: date,
    required_columns: Sequence[str],
    operation: str,
) -> date:
    missing = [column for column in (*_ROW_META_COLUMNS, *required_columns) if column not in row]
    if missing:
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: declared columns are missing {sorted(missing)}; "
            f"the row structure is damaged (got {len(row)} columns)",
            provider=PROVIDER,
        )
    if str(row.get("SECUCODE") or "").strip() != secucode:
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: asked for {secucode}, got a row of {row.get('SECUCODE')!r}",
            provider=PROVIDER,
        )
    report_date = _row_date(row.get("REPORT_DATE"), field_name="REPORT_DATE", operation=operation)
    if report_date != wanted:
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: asked for {wanted.isoformat()}, got a row of "
            f"{report_date.isoformat()}",
            provider=PROVIDER,
        )
    currency = str(row.get("CURRENCY") or "").strip()
    if currency != CURRENCY_CNY:
        raise EastmoneyAnnualDataError(
            f"eastmoney {operation}: {secucode} {wanted.isoformat()} CURRENCY is "
            f"{row.get('CURRENCY')!r}, not {CURRENCY_CNY}; amounts would be mislabelled",
            provider=PROVIDER,
        )
    return report_date


def _parse_common_row(
    row: Mapping[str, Any],
    *,
    source_table: SourceTable,
    response_hash: str,
    secucode: str,
    wanted: date,
    operation: str,
) -> tuple[SourceRowMeta, dict[str, Decimal | None]]:
    report_date = _check_row_identity(
        row,
        secucode=secucode,
        wanted=wanted,
        required_columns=[spec.source_field for spec in METRICS_BY_TABLE[source_table]],
        operation=operation,
    )
    meta = SourceRowMeta(
        source_table=source_table,
        report_period=report_date,
        notice_date=_optional_row_date(
            row.get("NOTICE_DATE"), field_name="NOTICE_DATE", operation=operation
        ),
        update_date=_optional_row_date(
            row.get("UPDATE_DATE"), field_name="UPDATE_DATE", operation=operation
        ),
        currency=CURRENCY_CNY,
        response_hash=response_hash,
    )
    values = {
        spec.source_field: _decimal_value(
            row.get(spec.source_field), field_name=spec.source_field, operation=operation
        )
        for spec in METRICS_BY_TABLE[source_table]
    }
    return meta, values


def _iter_wanted_row(
    rows: list[Any],
    *,
    wanted: date,
    operation: str,
) -> Mapping[str, Any] | None:
    """Validate every row of one answer and return the wanted period's row.

    Every row must be an object dated exactly ``wanted`` (the request named
    explicit dates; a different date means the filter was ignored) and
    duplicate periods are a data error - checked across the whole answer,
    not only until the wanted row is found.
    """

    seen: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise EastmoneyAnnualSchemaError(
                f"eastmoney {operation}: row is not an object: {row!r}", provider=PROVIDER
            )
        report_date = _row_date(
            row.get("REPORT_DATE"), field_name="REPORT_DATE", operation=operation
        )
        if report_date in seen:
            raise EastmoneyAnnualDataError(
                f"eastmoney {operation}: duplicate report period "
                f"{report_date.isoformat()} in one answer",
                provider=PROVIDER,
            )
        if report_date != wanted:
            raise EastmoneyAnnualSchemaError(
                f"eastmoney {operation}: asked for {wanted.isoformat()}, got a row of "
                f"{report_date.isoformat()}",
                provider=PROVIDER,
            )
        seen[report_date] = row
    return seen.get(wanted)


def parse_statement(
    body: bytes,
    *,
    source_table: SourceTable,
    secucode: str,
    wanted: date,
    response_hash: str,
) -> ParsedStatement:
    """One F10 statement answer (``data`` rows) reduced to the wanted row."""

    operation = OPERATIONS[source_table]
    payload = parse_json_body(body, operation=operation)
    if not isinstance(payload, dict):
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: answer is not an object ({str(payload)[:120]!r})",
            provider=PROVIDER,
        )
    data = payload.get("data")
    if data is not None and not isinstance(data, list):
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: no statement data in the answer ({str(payload)[:120]!r})",
            provider=PROVIDER,
        )
    row = _iter_wanted_row(data or [], wanted=wanted, operation=operation)
    if row is None:
        return NO_ROW
    meta, values = _parse_common_row(
        row,
        source_table=source_table,
        response_hash=response_hash,
        secucode=secucode,
        wanted=wanted,
        operation=operation,
    )
    return ParsedStatement(row=meta, values=values)


def parse_main_indicators(
    body: bytes,
    *,
    secucode: str,
    wanted: date,
    response_hash: str,
) -> ParsedStatement:
    """One datacenter ``RPT_F10_FINANCE_MAINFINADATA`` answer, completeness checked."""

    operation = OPERATIONS["main"]
    payload = parse_json_body(body, operation=operation)
    if not isinstance(payload, dict):
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: answer is not an object", provider=PROVIDER
        )
    if payload.get("success") is not True or payload.get("result") is None:
        # datacenter's "nothing for this filter" (undisclosed period): a gap.
        return NO_ROW
    result = payload.get("result")
    if not isinstance(result, dict):
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: result is not an object: {result!r}", provider=PROVIDER
        )
    pages = result.get("pages")
    if not isinstance(pages, int) or isinstance(pages, bool):
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: pagination information missing ({pages!r})",
            provider=PROVIDER,
        )
    if pages > 1:
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: answer is truncated ({pages} pages for one "
            f"annual date); refusing partial data",
            provider=PROVIDER,
        )
    data = result.get("data")
    if not isinstance(data, list):
        raise EastmoneyAnnualSchemaError(
            f"eastmoney {operation}: result.data is not a list: {data!r}", provider=PROVIDER
        )
    row = _iter_wanted_row(data, wanted=wanted, operation=operation)
    if row is None:
        return NO_ROW
    meta, values = _parse_common_row(
        row,
        source_table="main",
        response_hash=response_hash,
        secucode=secucode,
        wanted=wanted,
        operation=operation,
    )
    return ParsedStatement(row=meta, values=values)


_CTYPE_INPUT = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_CTYPE_VALUE = re.compile(r"""\bvalue\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def parse_ctype(html: str) -> str | None:
    """The F10 page's ``hidctype`` company-type metadata, or ``None``."""

    for tag in _CTYPE_INPUT.findall(html):
        if "hidctype" not in tag.lower():
            continue
        match = _CTYPE_VALUE.search(tag)
        if match:
            value = match.group(1).strip()
            return value if value.isdigit() else None
    return None


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnnualField:
    """One mapped metric of one company-year (see the module docstring)."""

    metric: str
    source_table: SourceTable
    source_field: str
    value: Decimal | None
    """Canonical value; ``None`` only with ``missing_reason`` (never 0-for-missing)."""
    unit: str
    """``CNY`` for amounts, ``fraction`` for the saved ROE ratio."""
    currency: str | None
    missing_reason: str | None = None
    raw_value: str | None = None
    """As the source stated it (ROE keeps its percent)."""
    raw_unit: str | None = None
    check_only: bool = False
    response_hash: str | None = None
    """sha256 of the HTTP answer the value came from; ``None`` for absent rows."""


@dataclass(frozen=True)
class AnnualRequestRecord:
    """One attempt (or reuse) of one planned request, in order."""

    at: datetime
    operation: str
    url: str
    params: Mapping[str, str]
    attempt: int
    """1-based HTTP attempt; 0 for a reused saved response."""
    status: int | None
    """HTTP status; ``None`` for a network failure or a reused response."""
    outcome: Literal["ok", "reused", "transient", "terminal"]
    response_sha256: str | None
    retry_after_seconds: float | None
    """Parsed Retry-After the collector obeyed (429 only)."""
    duration_ms: int | None
    error: str | None = None


@dataclass(frozen=True)
class CompanyAnnualCollection:
    """Everything one company-year collection learned (WP8 saves this)."""

    ticker: str
    secucode: str
    report_period: date
    company_type: str
    company_type_source: Literal["f10_metadata", "caller_provided", "bank_main_metadata"]
    captured_at: datetime
    provider: str = PROVIDER
    adapter_version: str = ADAPTER_VERSION
    fields: Mapping[str, AnnualField] = field(default_factory=dict)
    source_rows: Mapping[SourceTable, SourceRowMeta] = field(default_factory=dict)
    """Per table that actually had a row; revision marks included."""
    requests: tuple[AnnualRequestRecord, ...] = ()

    def missing_metrics(self) -> tuple[str, ...]:
        return tuple(metric for metric, item in self.fields.items() if item.value is None)


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _parse_retry_after(raw: str | None, now: datetime) -> float | None:
    """``Retry-After`` as seconds to wait (HTTP-date form resolved against ``now``)."""

    if not raw:
        return None
    try:
        return max(float(raw.strip()), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - now).total_seconds(), 0.0)


class EastmoneyAnnualCollector:
    """Per-company annual collection: throttled, retried, recorded, mapped.

    The caller (WP8) schedules companies serially; this class only ever works
    on one company-year per :meth:`collect_company_annual` call. The request
    log (:attr:`request_log`) survives failures so a checkpointing caller can
    still save every attempt.
    """

    def __init__(
        self,
        source: EastmoneyAnnualSource | None = None,
        *,
        cache: AnnualResponseCache | None = None,
        clock: Callable[[], datetime] = _utc_now,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = 1.0,
        min_interval_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        circuit: CircuitBreaker | None = None,
        on_call: ProviderCallHook | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        self._source = source
        self._cache = cache
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self.request_log: list[AnnualRequestRecord] = []
        self._throttle = Throttle(
            min_interval_seconds=min_interval_seconds, sleep=sleep, monotonic=monotonic
        )
        self._pipeline = ProviderCallPipeline(
            provider=PROVIDER,
            retry=RetryPolicy(max_attempts=max_retries + 1, backoff_seconds=retry_backoff_seconds),
            circuit=circuit,
            on_call=on_call,
            classify=classify_errors(EastmoneyAnnualTransientError),
            throttle=self._throttle,
            monotonic=monotonic,
            sleep=sleep,
        )

    @property
    def min_interval_seconds(self) -> float:
        """Minimum spacing between requests (the shared :class:`Throttle` owns it)."""

        return self._throttle.min_interval_seconds

    # -- public ------------------------------------------------------------ #

    async def collect_company_annual(
        self,
        ticker: str,
        report_period: date,
        *,
        company_type: str | None = None,
    ) -> CompanyAnnualCollection:
        """Collect one company's annual facts for the annual ``report_period``.

        ``company_type`` is caller-supplied stored metadata with its own
        provenance; when omitted, the F10 page's ctype metadata decides and an
        unconfirmable company fails with :class:`CompanyTypeUnknownError`
        before any statement is requested.
        """

        symbol = to_eastmoney_symbol(ticker)  # ValueError for a non-canonical ticker
        secucode = ticker.strip().upper()
        if (report_period.month, report_period.day) != (12, 31):
            raise ValueError(
                f"not an annual report period: {report_period.isoformat()} (want 12-31)"
            )
        captured_at = self._now()
        log_start = len(self.request_log)
        ctype, ctype_source = await self._resolve_company_type(symbol, company_type)

        parsed: dict[SourceTable, ParsedStatement] = {}
        for table in STATEMENT_TABLES:
            parsed[table] = await self._fetch_statement(
                table, symbol=symbol, secucode=secucode, ctype=ctype, wanted=report_period
            )
        parsed["main"] = await self._fetch_main(secucode=secucode, wanted=report_period)

        fields: dict[str, AnnualField] = {}
        source_rows: dict[SourceTable, SourceRowMeta] = {}
        for spec in ANNUAL_METRICS:
            fields[spec.metric] = self._map_field(spec, parsed[spec.source_table])
            row = parsed[spec.source_table].row
            if row is not None:
                source_rows[spec.source_table] = row
        result = CompanyAnnualCollection(
            ticker=secucode,
            secucode=secucode,
            report_period=report_period,
            company_type=ctype,
            company_type_source=ctype_source,
            captured_at=captured_at,
            fields=fields,
            source_rows=source_rows,
            requests=tuple(self.request_log[log_start:]),
        )
        log.info(
            "eastmoney_annual_collected",
            ticker=secucode,
            report_period=report_period.isoformat(),
            metrics=len(fields),
            missing=list(result.missing_metrics()),
        )
        return result

    # -- steps ------------------------------------------------------------- #

    async def _resolve_company_type(
        self, symbol: str, company_type: str | None
    ) -> tuple[str, Literal["f10_metadata", "caller_provided"]]:
        if company_type is not None:
            if not company_type.strip() or not company_type.strip().isdigit():
                raise CompanyTypeUnknownError(
                    f"caller-provided company type for {symbol} is not usable: {company_type!r}",
                    ticker=symbol,
                )
            return company_type.strip(), "caller_provided"
        params = {"type": "web", "code": symbol.lower()}

        def parse_ctype_page(response: RawAnnualResponse) -> str:
            ctype = parse_ctype(response.body.decode("utf-8", errors="replace"))
            if ctype is None:
                raise CompanyTypeUnknownError(
                    f"company type (ctype) of {symbol} cannot be confirmed from the F10 "
                    "metadata; the company is not collectible (no default type is guessed)",
                    ticker=symbol,
                )
            return ctype

        try:
            ctype = await self._logical_step(
                OPERATION_CTYPE, self._ctype_url(), params, parse_ctype_page
            )
        except CompanyTypeUnknownError:
            raise
        except EastmoneyAnnualError as exc:
            # A terminal HTTP refusal of the F10 page (404 for an unknown
            # symbol, ...) is the same verdict: this company is not collectible.
            raise CompanyTypeUnknownError(
                f"company type (ctype) of {symbol} cannot be confirmed ({exc}); "
                "the company is not collectible (no default type is guessed)",
                ticker=symbol,
            ) from exc
        return ctype, "f10_metadata"

    async def _fetch_statement(
        self,
        table: SourceTable,
        *,
        symbol: str,
        secucode: str,
        ctype: str,
        wanted: date,
    ) -> ParsedStatement:
        params = {
            "companyType": ctype,
            "reportDateType": "0",
            "reportType": "1",
            "dates": wanted.isoformat(),
            "code": symbol,
        }

        def parse(response: RawAnnualResponse) -> ParsedStatement:
            return parse_statement(
                response.body,
                source_table=table,
                secucode=secucode,
                wanted=wanted,
                response_hash=_sha256(response.body),
            )

        return await self._logical_step(
            OPERATIONS[table], f"{F10_BASE_URL}{STATEMENT_PATHS[table]}", params, parse
        )

    async def _fetch_main(self, *, secucode: str, wanted: date) -> ParsedStatement:
        params = {
            "reportName": MAIN_FINADATA_REPORT,
            "columns": "ALL",
            "filter": f"(SECUCODE=\"{secucode}\")(REPORT_DATE in ('{wanted.isoformat()}'))",
            "pageSize": "100",
            "pageNumber": "1",
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
            "source": "HSF10",
            "client": "PC",
        }

        def parse(response: RawAnnualResponse) -> ParsedStatement:
            return parse_main_indicators(
                response.body,
                secucode=secucode,
                wanted=wanted,
                response_hash=_sha256(response.body),
            )

        return await self._logical_step(
            OPERATIONS["main"], f"{DATACENTER_BASE_URL}{MAIN_FINADATA_PATH}", params, parse
        )

    def _map_field(self, spec: AnnualMetricSpec, parsed: ParsedStatement) -> AnnualField:
        if parsed.row is None:
            return AnnualField(
                metric=spec.metric,
                source_table=spec.source_table,
                source_field=spec.source_field,
                value=None,
                unit=spec.unit,
                currency=None,
                missing_reason="no_statement_row",
                raw_unit="percent" if spec.percent else spec.unit,
                check_only=spec.check_only,
            )
        raw = parsed.values[spec.source_field]
        currency = None if spec.percent else CURRENCY_CNY
        if raw is None:
            return AnnualField(
                metric=spec.metric,
                source_table=spec.source_table,
                source_field=spec.source_field,
                value=None,
                unit=spec.unit,
                currency=currency,
                missing_reason="upstream_null",
                raw_unit="percent" if spec.percent else spec.unit,
                check_only=spec.check_only,
                response_hash=parsed.row.response_hash,
            )
        value = raw / PERCENT if spec.percent else raw
        return AnnualField(
            metric=spec.metric,
            source_table=spec.source_table,
            source_field=spec.source_field,
            value=value,
            unit=spec.unit,
            currency=currency,
            raw_value=_decimal_string(raw),
            raw_unit="percent" if spec.percent else spec.unit,
            check_only=spec.check_only,
            response_hash=parsed.row.response_hash,
        )

    # -- plumbing ---------------------------------------------------------- #

    def _ctype_url(self) -> str:
        return f"{F10_BASE_URL}{CTYPE_PATH}"

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    def _record(
        self,
        *,
        operation: str,
        url: str,
        params: Mapping[str, str],
        attempt: int,
        status: int | None,
        outcome: Literal["ok", "reused", "transient", "terminal"],
        response_sha256: str | None,
        retry_after_seconds: float | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        self.request_log.append(
            AnnualRequestRecord(
                at=self._now(),
                operation=operation,
                url=url,
                params=dict(params),
                attempt=attempt,
                status=status,
                outcome=outcome,
                response_sha256=response_sha256,
                retry_after_seconds=retry_after_seconds,
                duration_ms=duration_ms,
                error=error,
            )
        )

    async def _logical_step[T](
        self,
        operation: str,
        url: str,
        params: Mapping[str, str],
        parse: Callable[[RawAnnualResponse], T],
    ) -> T:
        """One logical call - request **and** parse - with one ledger event.

        The shared pipeline (``transport.py``) owns the circuit, the retries,
        the 429 handling and the single ledger event. This method keeps the
        two eastmoney-specific side channels: the saved-response reuse check
        (before the circuit - a reused answer never touches the provider) and
        the ``request_log`` records, which the attempt itself writes.
        """

        started = self._monotonic()

        def reuse() -> Reused[T] | None:
            if self._cache is None:
                return None
            saved = self._cache.saved_response(operation, params)
            if saved is None:
                return None
            self._record(
                operation=operation,
                url=url,
                params=params,
                attempt=0,
                status=None,
                outcome="reused",
                response_sha256=_sha256(saved),
            )
            return Reused(parse(RawAnnualResponse(status=200, body=saved)))

        async def attempt(context: Attempt) -> T:
            return await self._attempt(operation, url, params, parse, context)

        return await self._pipeline.run(
            operation, attempt, before=reuse, started=started, ticker_count=1
        )

    async def _attempt[T](
        self,
        operation: str,
        url: str,
        params: Mapping[str, str],
        parse: Callable[[RawAnnualResponse], T],
        context: Attempt,
    ) -> T:
        """One attempt: fetch (recording every request), classify HTTP, parse.

        The parse runs inside the attempt - as it always has - so a schema /
        data failure is reported once as ``terminal`` (never retried, never a
        circuit opener) and cannot escape the logical call.
        """

        started = self._monotonic()
        try:
            response = await self._source_for(operation).fetch(url, params)
        except EastmoneyAnnualTransientError as exc:
            self._record(
                operation=operation,
                url=url,
                params=params,
                attempt=context.number,
                status=None,
                outcome="transient",
                response_sha256=None,
                duration_ms=self._duration(started),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        hash_hex = _sha256(response.body)
        retry_after = (
            _parse_retry_after(response.retry_after, self._now())
            if response.status == 429
            else None
        )
        duration_ms = self._duration(started)
        if response.status == 200:
            self._record(
                operation=operation,
                url=url,
                params=params,
                attempt=context.number,
                status=200,
                outcome="ok",
                response_sha256=hash_hex,
                duration_ms=duration_ms,
            )
            return parse(RawAnnualResponse(status=200, body=response.body))
        if response.status == 429 or response.status >= 500:
            self._record(
                operation=operation,
                url=url,
                params=params,
                attempt=context.number,
                status=response.status,
                outcome="transient",
                response_sha256=hash_hex,
                retry_after_seconds=retry_after,
                duration_ms=duration_ms,
                error=f"HTTP {response.status}",
            )
            context.retry_after_seconds = retry_after
            if context.last:
                raise EastmoneyAnnualTransientError(
                    f"eastmoney {operation} HTTP {response.status} after {context.number} attempts",
                    provider=PROVIDER,
                    error_code=str(response.status),
                )
            raise EastmoneyAnnualTransientError(
                f"eastmoney {operation} HTTP {response.status}",
                provider=PROVIDER,
                error_code=str(response.status),
            )
        self._record(
            operation=operation,
            url=url,
            params=params,
            attempt=context.number,
            status=response.status,
            outcome="terminal",
            response_sha256=hash_hex,
            duration_ms=duration_ms,
            error=f"HTTP {response.status}",
        )
        raise EastmoneyAnnualError(
            f"eastmoney {operation} HTTP {response.status}",
            provider=PROVIDER,
            error_code=str(response.status),
        )

    def _source_for(self, operation: str) -> EastmoneyAnnualSource:
        if self._source is None:
            self._source = HttpEastmoneyAnnualSource(timeout_seconds=self._timeout_seconds)
        return self._source

    def _duration(self, started: float) -> int:
        return max(int((self._monotonic() - started) * 1000), 0)


__all__ = [
    "ADAPTER_VERSION",
    "ALLOWED_HOSTS",
    "ANNUAL_METRICS",
    "AnnualField",
    "AnnualMetricSpec",
    "AnnualRequestRecord",
    "AnnualResponseCache",
    "CompanyAnnualCollection",
    "CompanyTypeUnknownError",
    "DATACENTER_BASE_URL",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "EastmoneyAnnualCollector",
    "EastmoneyAnnualDataError",
    "EastmoneyAnnualError",
    "EastmoneyAnnualSchemaError",
    "EastmoneyAnnualSource",
    "EastmoneyAnnualTransientError",
    "F10_BASE_URL",
    "HttpEastmoneyAnnualSource",
    "MAIN_FINADATA_PATH",
    "MAIN_FINADATA_REPORT",
    "PROVIDER",
    "ParsedStatement",
    "RawAnnualResponse",
    "SourceRowMeta",
    "SourceTable",
    "parse_ctype",
    "parse_json_body",
    "parse_main_indicators",
    "parse_statement",
]

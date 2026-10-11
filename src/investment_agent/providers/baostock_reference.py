"""baostock reference-data adapter: index constituents and CSRC industries.

baostock is a free, token-less, *synchronous* socket library that keeps one
process-global session (``login`` / ``logout``). Every public call here runs
``login -> query -> logout`` inside a worker thread (``asyncio.to_thread``)
while holding a process-wide lock, so concurrent coroutines never interleave
on the shared socket.

A login costs about one second, so collectors issuing hundreds of queries
open one :meth:`BaostockReferenceClient.session` instead: it holds the
process-wide lock and a single login for the whole block, every query inside
it skips its own login/logout, and a transient failure re-logs in before the
retry. The market / financial queries live in the sibling module
:mod:`investment_agent.providers.baostock_market`, which reuses this client.
:meth:`BaostockReferenceClient.fetch_all_stocks` (``query_all_stock``) feeds
the local stock directory (mrd-intake D4).

Point-in-time rules (see ``docs/product/funnel-design.md`` 15.1):

* the effective date of a snapshot is baostock's ``updateDate``, never the
  requested date;
* baostock silently answers future dates with its latest snapshot, so any
  result whose ``updateDate`` is later than the capture date (or the requested
  ``as_of``) is rejected with :class:`LookaheadDataError`;
* an empty constituent result (before an index was published, ~2005) is an
  empty snapshot, not an error.

This module is independent from the iFinD research chain on purpose.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal, Protocol, cast

import structlog

from investment_agent.clock import SHANGHAI_TZ
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

PROVIDER = "baostock"
INDUSTRY_SYSTEM_CSRC = "csrc"

IndexCode = Literal["CSI300", "SSE50", "CSI500"]
INDEX_CODES: tuple[IndexCode, ...] = ("CSI300", "SSE50", "CSI500")

_INDEX_QUERIES: dict[IndexCode, str] = {
    "CSI300": "query_hs300_stocks",
    "SSE50": "query_sz50_stocks",
    "CSI500": "query_zz500_stocks",
}

_SUCCESS = "0"
# 10001001 = not logged in (session dropped); 10002xxx = socket/network errors.
_TRANSIENT_CODES = frozenset({"10001001"})
_TRANSIENT_CODE_PREFIX = "10002"

_BAOSTOCK_CODE = re.compile(r"^(?P<exchange>sh|sz|bj)\.(?P<symbol>\d{6})$")
_CANONICAL_TICKER = re.compile(r"^(?P<symbol>\d{6})\.(?P<suffix>SH|SZ|BJ)$")
_CSRC_INDUSTRY = re.compile(r"^(?P<code>[A-Z]\d{2})(?P<name>\S.*)$")
# ``query_all_stock`` lists every security (indices ``sh.000``, funds ``sh.5`` /
# ``sz.1``, Shenzhen indices ``sz.399``); these prefixes are the A-share stocks (main board, STAR,
# ChiNext, BSE - baostock answers no ``bj.`` rows as of 2026-10, kept for later).
_STOCK_CODE_PREFIXES = ("sh.6", "sz.00", "sz.30", "bj.")

# One baostock session per process: the library keeps its socket and user id
# in module globals, so every login/query/logout sequence must be serialized.
# ``_SESSION_LOCK`` owns a whole login..logout span (one call, or one
# ``session()`` block); ``_IO_LOCK`` serializes the individual socket
# operations, so a thread still running after an asyncio timeout can never
# interleave with the retry that follows it.
_SESSION_LOCK = threading.Lock()
_IO_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Backend protocol (the real ``baostock`` module or an offline fake)
# --------------------------------------------------------------------------- #


class BaostockStatus(Protocol):
    error_code: str
    error_msg: str


class BaostockResultSet(Protocol):
    error_code: str
    error_msg: str
    fields: list[str]

    def next(self) -> bool: ...

    def get_row_data(self) -> list[str]: ...


class BaostockBackend(Protocol):
    def login(self) -> BaostockStatus: ...

    def logout(self) -> BaostockStatus: ...

    def query_hs300_stocks(self, date: str = "") -> BaostockResultSet: ...

    def query_sz50_stocks(self, date: str = "") -> BaostockResultSet: ...

    def query_zz500_stocks(self, date: str = "") -> BaostockResultSet: ...

    def query_stock_industry(self, code: str = "", date: str = "") -> BaostockResultSet: ...

    def query_all_stock(self, day: str = "") -> BaostockResultSet: ...

    def query_trade_dates(self, start_date: str = "", end_date: str = "") -> BaostockResultSet: ...


def load_default_backend() -> BaostockBackend:
    """Import the real ``baostock`` module lazily (it pulls in pandas)."""

    import baostock

    return cast(BaostockBackend, baostock)


# --------------------------------------------------------------------------- #
# Errors and ticker mapping
# --------------------------------------------------------------------------- #


class BaostockError(ProviderError):
    """Non-success answer from baostock that retrying will not fix."""


class BaostockTransientError(ProviderTransientError):
    """Network / session / timeout failure talking to baostock."""


def is_transient_code(error_code: str) -> bool:
    return error_code in _TRANSIENT_CODES or error_code.startswith(_TRANSIENT_CODE_PREFIX)


def _raise_for_code(error_code: str, error_msg: str, *, operation: str) -> None:
    if error_code == _SUCCESS:
        return
    message = f"baostock {operation} failed: {error_code} {error_msg}".strip()
    if is_transient_code(error_code):
        raise BaostockTransientError(message, provider=PROVIDER, error_code=error_code)
    raise BaostockError(message, provider=PROVIDER, error_code=error_code)


def to_canonical_ticker(baostock_code: str) -> str:
    """``sh.600000`` -> ``600000.SH`` (also ``sz.`` -> ``.SZ``, ``bj.`` -> ``.BJ``)."""

    match = _BAOSTOCK_CODE.fullmatch(baostock_code.strip().lower())
    if match is None:
        raise ValueError(f"not a baostock A-share code: {baostock_code!r}")
    return f"{match.group('symbol')}.{match.group('exchange').upper()}"


def to_baostock_code(ticker: str) -> str:
    """``600000.SH`` -> ``sh.600000`` (inverse of :func:`to_canonical_ticker`)."""

    match = _CANONICAL_TICKER.fullmatch(ticker.strip().upper())
    if match is None:
        raise ValueError(f"not a canonical A-share ticker: {ticker!r}")
    return f"{match.group('suffix').lower()}.{match.group('symbol')}"


def is_stock_code(baostock_code: str) -> bool:
    """``sh.600519`` -> True; indices (``sh.000001``) and funds (``sh.510300``) -> False."""

    code = baostock_code.strip().lower()
    return code.startswith(_STOCK_CODE_PREFIXES) and _BAOSTOCK_CODE.fullmatch(code) is not None


def parse_csrc_industry(raw: str) -> tuple[str | None, str | None]:
    """Split ``C15酒、饮料和精制茶制造业`` into ``("C15", "酒、饮料和精制茶制造业")``.

    Empty input means "no classification" -> ``(None, None)``. Pre-2012 rows
    use a code-less hierarchical label (``制造业-饮料制造业-...``); those keep
    the full label as the name with a ``None`` code.
    """

    text = raw.strip()
    if not text:
        return None, None
    match = _CSRC_INDUSTRY.fullmatch(text)
    if match is None:
        return None, text
    return match.group("code"), match.group("name").strip()


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConstituentSnapshot:
    index_code: IndexCode
    as_of: date
    effective_date: date | None
    """baostock ``updateDate``; ``None`` for an empty (pre-publication) result."""
    members: tuple[str, ...]
    """Canonical tickers (``600000.SH``), sorted."""
    member_names: Mapping[str, str]
    captured_at: datetime
    raw_rows: tuple[Mapping[str, str], ...] = field(repr=False)
    source: str = PROVIDER

    @property
    def is_empty(self) -> bool:
        return not self.members


@dataclass(frozen=True)
class IndustryEntry:
    ticker: str
    company_name: str
    industry_code: str | None
    industry_name: str | None
    effective_date: date
    raw_industry: str
    classification_label: str


@dataclass(frozen=True)
class IndustrySnapshot:
    as_of: date
    effective_date: date | None
    """Latest ``updateDate`` among entries; ``None`` when the result is empty."""
    entries: tuple[IndustryEntry, ...]
    captured_at: datetime
    raw_rows: tuple[Mapping[str, str], ...] = field(repr=False)
    system: str = INDUSTRY_SYSTEM_CSRC
    source: str = PROVIDER

    def by_ticker(self) -> dict[str, IndustryEntry]:
        return {entry.ticker: entry for entry in self.entries}


@dataclass(frozen=True)
class DirectoryEntry:
    ticker: str
    name: str
    trading: bool
    """baostock ``tradeStatus`` ``1``; ``0`` = suspended on ``as_of``."""


@dataclass(frozen=True)
class DirectorySnapshot:
    """Every A-share stock listed on ``as_of`` (``query_all_stock``).

    The answer carries no date: ``as_of`` is the requested day, which the PIT
    guard keeps on or before the capture date. baostock answers an empty list
    for a non-trading day and for today before the open; that is an empty
    snapshot, not an error (callers step back to the previous trading day).
    """

    as_of: date
    entries: tuple[DirectoryEntry, ...]
    """Sorted by ticker; indices / funds already filtered out."""
    captured_at: datetime
    raw_rows: tuple[Mapping[str, str], ...] = field(repr=False)
    source: str = PROVIDER

    @property
    def is_empty(self) -> bool:
        return not self.entries


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _RawResult:
    fields: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BaostockReferenceClient:
    """Async facade over baostock for index constituents and CSRC industries."""

    def __init__(
        self,
        backend: BaostockBackend | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timeout_seconds: float = 180.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
        circuit: CircuitBreaker | None = None,
        on_call: ProviderCallHook | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._backend = backend
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._circuit = circuit
        self._on_call = on_call
        self._session_depth = 0

    # -- session ----------------------------------------------------------- #

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        """Keep one baostock login open for every query issued inside the block.

        Re-entrant per client. The process-wide session lock is held for the
        whole block, so other clients (e.g. a reference sync) wait for it.
        """

        if self._session_depth:
            self._session_depth += 1
            try:
                yield
            finally:
                self._session_depth -= 1
            return
        backend = self._resolve_backend()
        await _acquire_session_lock()
        try:
            await self._with_retries("login", lambda: _login(backend), relogin=None)
            self._session_depth = 1
            try:
                yield
            finally:
                self._session_depth = 0
                await asyncio.to_thread(_logout_quietly, backend)
        finally:
            _SESSION_LOCK.release()

    @property
    def in_session(self) -> bool:
        return self._session_depth > 0

    # -- public API -------------------------------------------------------- #

    async def fetch_constituents(
        self, index_code: IndexCode, as_of: date
    ) -> ConstituentSnapshot:
        method = _INDEX_QUERIES.get(index_code)
        if method is None:
            raise ValueError(f"unsupported index code: {index_code!r}")
        day = as_of.isoformat()

        def query(backend: BaostockBackend) -> BaostockResultSet:
            if index_code == "CSI300":
                return backend.query_hs300_stocks(date=day)
            if index_code == "SSE50":
                return backend.query_sz50_stocks(date=day)
            return backend.query_zz500_stocks(date=day)

        raw = await self._run(method, query)
        captured_at = self._now()
        capture_date = captured_at.astimezone(SHANGHAI_TZ).date()

        update_dates = {_parse_update_date(row, operation=method) for row in raw.rows}
        if len(update_dates) > 1:
            raise ProviderDataError(
                f"baostock {method} returned mixed updateDate values: "
                f"{sorted(d.isoformat() for d in update_dates)}",
                provider=PROVIDER,
            )
        effective_date = next(iter(update_dates)) if update_dates else None
        if effective_date is not None:
            _guard_lookahead(
                effective_date, as_of=as_of, capture_date=capture_date, operation=method
            )

        names: dict[str, str] = {}
        for row in raw.rows:
            ticker = _row_ticker(row, operation=method)
            names[ticker] = row.get("code_name", "").strip()
        members = tuple(sorted(names))
        log.info(
            "baostock_constituents_fetched",
            index_code=index_code,
            as_of=day,
            effective_date=effective_date.isoformat() if effective_date else None,
            members=len(members),
        )
        return ConstituentSnapshot(
            index_code=index_code,
            as_of=as_of,
            effective_date=effective_date,
            members=members,
            member_names=names,
            captured_at=captured_at,
            raw_rows=raw.rows,
        )

    async def fetch_industry(
        self, as_of: date, symbol: str | None = None
    ) -> IndustrySnapshot:
        """CSRC industry per stock as of ``as_of``; ``symbol`` limits it to one ticker."""

        code = to_baostock_code(symbol) if symbol is not None else ""
        day = as_of.isoformat()
        operation = "query_stock_industry"

        def query(backend: BaostockBackend) -> BaostockResultSet:
            return backend.query_stock_industry(code=code, date=day)

        raw = await self._run(operation, query)
        captured_at = self._now()
        capture_date = captured_at.astimezone(SHANGHAI_TZ).date()

        entries: list[IndustryEntry] = []
        for row in raw.rows:
            update_date = _parse_update_date(row, operation=operation)
            _guard_lookahead(
                update_date, as_of=as_of, capture_date=capture_date, operation=operation
            )
            raw_industry = row.get("industry", "")
            industry_code, industry_name = parse_csrc_industry(raw_industry)
            entries.append(
                IndustryEntry(
                    ticker=_row_ticker(row, operation=operation),
                    company_name=row.get("code_name", "").strip(),
                    industry_code=industry_code,
                    industry_name=industry_name,
                    effective_date=update_date,
                    raw_industry=raw_industry,
                    classification_label=row.get("industryClassification", ""),
                )
            )
        entries.sort(key=lambda entry: entry.ticker)
        effective_date = max((entry.effective_date for entry in entries), default=None)
        log.info(
            "baostock_industry_fetched",
            as_of=day,
            symbol=symbol,
            effective_date=effective_date.isoformat() if effective_date else None,
            entries=len(entries),
        )
        return IndustrySnapshot(
            as_of=as_of,
            effective_date=effective_date,
            entries=tuple(entries),
            captured_at=captured_at,
            raw_rows=raw.rows,
        )

    async def fetch_all_stocks(self, day: date) -> DirectorySnapshot:
        """All A-share stocks listed on ``day`` (stocks only; see :func:`is_stock_code`)."""

        operation = "query_all_stock"
        capture_date = self._now().astimezone(SHANGHAI_TZ).date()
        if day > capture_date:
            raise LookaheadDataError(
                f"baostock {operation} day {day.isoformat()} is after the capture date "
                f"{capture_date.isoformat()}",
                provider=PROVIDER,
            )
        day_text = day.isoformat()

        def query(backend: BaostockBackend) -> BaostockResultSet:
            return backend.query_all_stock(day=day_text)

        raw = await self._run(operation, query)
        captured_at = self._now()
        entries: dict[str, DirectoryEntry] = {}
        for row in raw.rows:
            code = row.get("code", "")
            if not is_stock_code(code):
                continue
            ticker = _row_ticker(row, operation=operation)
            status = row.get("tradeStatus", "").strip()
            if status not in ("0", "1"):
                raise ProviderDataError(
                    f"baostock {operation} returned an invalid tradeStatus {status!r} for {code}",
                    provider=PROVIDER,
                )
            entries[ticker] = DirectoryEntry(
                ticker=ticker, name=row.get("code_name", "").strip(), trading=status == "1"
            )
        log.info(
            "baostock_directory_fetched",
            as_of=day_text,
            rows=len(raw.rows),
            stocks=len(entries),
        )
        return DirectorySnapshot(
            as_of=day,
            entries=tuple(entries[ticker] for ticker in sorted(entries)),
            captured_at=captured_at,
            raw_rows=raw.rows,
        )

    async def fetch_trade_dates(self, start: date, end: date) -> dict[date, bool]:
        """Exchange calendar answers for every day in ``[start, end]``.

        Returns ``{date: is_trading_day}`` covering *every* calendar day of the
        range (weekends included), so a fully answered year is recognizable by
        day coverage alone. This is the one query that deliberately answers the
        future - the exchange publishes the holiday schedule months ahead - so
        the point-of-time guards of the other methods do not apply. Until the
        schedule is published the range comes back empty.
        """

        if end < start:
            raise ValueError("end must be on or after start")
        start_text = start.isoformat()
        end_text = end.isoformat()
        operation = "query_trade_dates"

        def query(backend: BaostockBackend) -> BaostockResultSet:
            return backend.query_trade_dates(start_date=start_text, end_date=end_text)

        raw = await self._run(operation, query)
        answers: dict[date, bool] = {}
        for row in raw.rows:
            raw_day = row.get("calendar_date", "")
            try:
                day = date.fromisoformat(raw_day)
            except ValueError as exc:
                raise ProviderDataError(
                    f"baostock {operation} returned an invalid calendar_date {raw_day!r}",
                    provider=PROVIDER,
                ) from exc
            flag = row.get("is_trading_day", "").strip()
            if flag not in ("0", "1"):
                raise ProviderDataError(
                    f"baostock {operation} returned an invalid is_trading_day {flag!r} "
                    f"for {raw_day}",
                    provider=PROVIDER,
                )
            answers[day] = flag == "1"
        log.info(
            "baostock_trade_dates_fetched",
            start=start_text,
            end=end_text,
            days=len(answers),
            trading=sum(answers.values()),
        )
        return answers

    # -- plumbing ---------------------------------------------------------- #

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    def _resolve_backend(self) -> BaostockBackend:
        if self._backend is None:
            self._backend = load_default_backend()
        return self._backend

    async def _run(
        self,
        operation: str,
        query: Callable[[BaostockBackend], BaostockResultSet],
        *,
        ticker_count: int = 0,
    ) -> _RawResult:
        backend = self._resolve_backend()
        if self._session_depth:
            return await self._with_retries(
                operation,
                lambda: _locked_query(backend, operation, query),
                relogin=lambda: _relogin(backend),
                ticker_count=ticker_count,
            )
        return await self._with_retries(
            operation,
            lambda: _session_query(backend, operation, query),
            relogin=None,
            ticker_count=ticker_count,
        )

    async def _with_retries[T](
        self,
        operation: str,
        call: Callable[[], T],
        *,
        relogin: Callable[[], None] | None,
        ticker_count: int = 0,
    ) -> T:
        """Run blocking ``call`` in a thread with a timeout and transient retries.

        The circuit breaker (when wired) is checked once before the first
        attempt and told the final outcome once: in-call retries are unchanged
        and the breaker counts logical calls that failed for good. ``on_call``
        (the provider ledger) gets one event per logical call.
        """

        started = time.monotonic()
        outcome: CallOutcome = "terminal"
        try:
            if self._circuit is not None:
                self._circuit.check(PROVIDER, operation)
            result = await self._attempts(operation, call, relogin=relogin)
        except ProviderUnavailableError:
            outcome = "skipped_open"
            raise
        except BaseException as exc:
            outcome = "transient" if isinstance(exc, ProviderTransientError) else "terminal"
            if self._circuit is not None:
                self._circuit.record_failure(PROVIDER, operation, exc)
            raise
        else:
            outcome = "ok"
            if self._circuit is not None:
                self._circuit.record_success(PROVIDER, operation)
            return result
        finally:
            if self._on_call is not None:
                duration_ms = (
                    None
                    if outcome == "skipped_open"
                    else max(int((time.monotonic() - started) * 1000), 0)
                )
                await self._on_call(
                    ProviderCallEvent(PROVIDER, operation, outcome, duration_ms, ticker_count)
                )

    async def _attempts[T](
        self,
        operation: str,
        call: Callable[[], T],
        *,
        relogin: Callable[[], None] | None,
    ) -> T:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(call), timeout=self._timeout_seconds
                )
            except TimeoutError as exc:
                error: BaostockTransientError = BaostockTransientError(
                    f"baostock {operation} timed out after {self._timeout_seconds}s",
                    provider=PROVIDER,
                )
                error.__cause__ = exc
            except BaostockTransientError as exc:
                error = exc
            if attempt >= self._max_attempts:
                raise error
            log.warning(
                "baostock_transient_retry",
                operation=operation,
                attempt=attempt,
                error=str(error),
            )
            await asyncio.sleep(self._retry_backoff_seconds * attempt)
            if relogin is not None:
                # The session may be gone (10001001 / dropped socket): log in
                # again before retrying; a failed re-login uses up an attempt.
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(relogin), timeout=self._timeout_seconds
                    )
                except (TimeoutError, BaostockTransientError) as exc:
                    log.warning("baostock_relogin_failed", operation=operation, error=str(exc))


async def _acquire_session_lock() -> None:
    """Take the process-wide session lock without blocking the event loop.

    If the waiting coroutine is cancelled, the lock is released as soon as the
    helper thread obtains it, so cancellation can never leak the lock.
    """

    waiter = asyncio.ensure_future(asyncio.to_thread(_SESSION_LOCK.acquire))
    try:
        await asyncio.shield(waiter)
    except asyncio.CancelledError:

        def release(future: asyncio.Future[bool]) -> None:
            if not future.cancelled() and future.exception() is None and future.result():
                _SESSION_LOCK.release()

        waiter.add_done_callback(release)
        raise


def _login(backend: BaostockBackend) -> None:
    with _IO_LOCK:
        try:
            status = backend.login()
        except OSError as exc:
            raise BaostockTransientError(
                f"baostock login failed: {exc}", provider=PROVIDER
            ) from exc
        _raise_for_code(status.error_code, status.error_msg, operation="login")


def _logout_quietly(backend: BaostockBackend) -> None:
    with _IO_LOCK:
        try:
            backend.logout()
        except Exception as exc:  # logout failure must not mask the result
            log.warning("baostock_logout_failed", error=str(exc))


def _relogin(backend: BaostockBackend) -> None:
    _logout_quietly(backend)
    _login(backend)


def _locked_query(
    backend: BaostockBackend,
    operation: str,
    query: Callable[[BaostockBackend], BaostockResultSet],
) -> _RawResult:
    """One query inside an already open session (sync)."""

    with _IO_LOCK:
        try:
            return _collect(query(backend), operation=operation)
        except OSError as exc:
            raise BaostockTransientError(
                f"baostock {operation} failed: {exc}", provider=PROVIDER
            ) from exc


def _session_query(
    backend: BaostockBackend,
    operation: str,
    query: Callable[[BaostockBackend], BaostockResultSet],
) -> _RawResult:
    """login -> query -> logout under the process-wide session lock (sync)."""

    with _SESSION_LOCK:
        _login(backend)
        try:
            return _locked_query(backend, operation, query)
        finally:
            _logout_quietly(backend)


def _collect(result: BaostockResultSet, *, operation: str) -> _RawResult:
    _raise_for_code(result.error_code, result.error_msg, operation=operation)
    fields = tuple(result.fields)
    rows: list[Mapping[str, str]] = []
    while result.next():
        row = result.get_row_data()
        if len(row) != len(fields):
            raise ProviderDataError(
                f"baostock {operation} row has {len(row)} values for {len(fields)} fields",
                provider=PROVIDER,
            )
        rows.append(dict(zip(fields, row, strict=True)))
    # A failed next-page request leaves error_code set or (on a dropped socket)
    # silently stops paging; both must not look like a complete answer.
    _raise_for_code(result.error_code, result.error_msg, operation=operation)
    _guard_truncated_paging(result, len(rows), operation=operation)
    return _RawResult(fields=fields, rows=tuple(rows))


def _guard_truncated_paging(
    result: BaostockResultSet, row_count: int, *, operation: str
) -> None:
    page = getattr(result, "cur_page_num", None)
    per_page = getattr(result, "per_page_count", None)
    if page is None or per_page is None:
        return
    try:
        page_number, page_size = int(page), int(per_page)
    except (TypeError, ValueError):
        return
    if row_count and page_size and row_count == page_number * page_size:
        # The last page was full but no follow-up page was acknowledged:
        # the next-page request was lost (baostock returns False silently).
        raise BaostockTransientError(
            f"baostock {operation} paging stopped after a full page "
            f"({row_count} rows); result is likely truncated",
            provider=PROVIDER,
        )


def _parse_update_date(row: Mapping[str, str], *, operation: str) -> date:
    raw = row.get("updateDate", "")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ProviderDataError(
            f"baostock {operation} returned an invalid updateDate {raw!r}",
            provider=PROVIDER,
        ) from exc


def _row_ticker(row: Mapping[str, str], *, operation: str) -> str:
    raw = row.get("code", "")
    try:
        return to_canonical_ticker(raw)
    except ValueError as exc:
        raise ProviderDataError(
            f"baostock {operation} returned an invalid code {raw!r}", provider=PROVIDER
        ) from exc


def _guard_lookahead(
    effective_date: date, *, as_of: date, capture_date: date, operation: str
) -> None:
    if effective_date > capture_date:
        raise LookaheadDataError(
            f"baostock {operation} updateDate {effective_date.isoformat()} is after "
            f"the capture date {capture_date.isoformat()}",
            provider=PROVIDER,
        )
    if effective_date > as_of:
        raise LookaheadDataError(
            f"baostock {operation} updateDate {effective_date.isoformat()} is after "
            f"the requested as_of {as_of.isoformat()}",
            provider=PROVIDER,
        )


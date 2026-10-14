"""Tencent batch quote client (mrd-intake B1): the same-day close at 15:10.

Only the **close snapshot** is read (decision D1, no intraday polling): the
``close_quote`` job calls :meth:`TencentQuoteClient.fetch_close` after the
session ends and stores the day's bar as ``market_snapshot`` rows with
``provider="tencent"``, bridging the gap until the baostock bars arrive in
the evening (decision D2: baostock first, Tencent as the fallback).

源链与口径 (source chain and units; calibration records live in
``config/field_sources.yaml``, ``2026-10-09.2``):

* Chain: ``GET https://qt.gtimg.cn/q=<code>[,<code>...]`` (an unofficial
  public endpoint of the Tencent finance site; needs a browser-like
  ``User-Agent``), GBK-encoded body, one ``v_<code>="…";`` line per code,
  fields separated by ``~``. Up to :data:`MAX_CODES_PER_REQUEST` codes per
  request (mrd ``server/lib/tencent-urls.cjs``). A code the feed does not
  know is simply absent from the answer (``v_pv_none_match="1"``), never an
  error.
* Codes: ``600519.SH`` -> ``sh600519``, ``000001.SZ`` -> ``sz000001``,
  ``920748.BJ`` -> ``bj920748``.
* Field positions (0-based, checked live 2026-10-09 06:46 CST against the
  2026-10-08 close of 600519 / 000001 / 920748 / 000002, see
  ``tests/fixtures/tencent/quotes_sample.txt``):

  ====  ==============================  ====================================
  pos   meaning                         unit / note
  ====  ==============================  ====================================
  1     name                            ``ST`` / ``*ST`` prefix marks ST
  3     last price (= close after 15:00) CNY/share
  4     previous close                  CNY/share
  5     open                            CNY/share
  6     volume                          **手** (lots of 100 shares) -> ×100
  30    quote time ``YYYYMMDDHHMMSS``   Asia/Shanghai
  31/32 change / change percent         CNY / %
  33/34 high / low                      CNY/share
  35    ``price/volume/amount``         amount in **CNY** (used only to
                                        cross-check field 37)
  37    amount                          **万元** -> ×10 000 = CNY
  38    turnover                        **percent** -> ÷ 100 = fraction
  39    PE (TTM)                        ratio (times); negative when loss
  43    amplitude                       %
  46    PB                              ratio (not stored: uncalibrated)
  ====  ==============================  ====================================

  600519 on 2026-10-08: close 1255.79, volume 25168 手, amount 314541 万元
  (field 35 says 3 145 413 157 CNY: the 万元 figure is rounded), turnover
  0.20 %, PE 19.28 - all consistent with the baostock bar of the same day.
* Point in time: field 30 is the time of the quote. A date **after** the
  capture date raises :class:`LookaheadDataError`; a quote dated before the
  requested trade date, or on the trade date but before
  :data:`SESSION_CLOSE` (15:00:00), is ``pending`` (the session is not over
  or the day has not been quoted yet) and the collector tries again later.
  A quote dated after the trade date (but not after the capture date) is
  ``missing``: the feed only serves the latest session.
* Suspension: volume 0 on the trade date, or the status flag ``S`` in
  field 40, means the stock did not trade. Live rows of a suspended stock
  (601059.SH on 2026-10-09) carry the previous close as the last price,
  open / high / low 0, volume 0 and ``S``; a last price of 0 also occurs.
  The bar is stored like baostock stores suspended days (``trade_status=0``,
  OHLC = previous close, volume / amount 0).
* Errors: network / timeouts / HTTP 5xx and 429 ->
  :class:`ProviderTransientError`; other HTTP 4xx, undecodable or
  unparsable bodies -> :class:`ProviderDataError` (terminal: schema drift
  must stop the collector, not be stored). Every logical request (retries
  included) is reported once to ``on_call`` (the provider ledger, A2) and
  consulted / recorded with the circuit breaker (A1) under
  ``tencent:q``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from datetime import time as time_of_day
from decimal import Decimal, InvalidOperation

import httpx
import structlog

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.providers.circuit import CircuitBreaker
from investment_agent.providers.errors import (
    LookaheadDataError,
    ProviderDataError,
    ProviderTransientError,
)
from investment_agent.providers.ledger import ProviderCallHook
from investment_agent.providers.transport import Attempt, ProviderCallPipeline, RetryPolicy

log = structlog.get_logger(__name__)

PROVIDER = "tencent"
ENDPOINT = "q"
BASE_URL = "https://qt.gtimg.cn"
USER_AGENT = "Mozilla/5.0"
MAX_CODES_PER_REQUEST = 60
SESSION_CLOSE = time_of_day(15, 0)
"""A-share continuous trading ends at 15:00 (closing call auction included)."""
ENCODING = "gb18030"
"""GBK superset: decodes every GBK byte sequence the feed may emit."""

# Field positions (see the module docstring).
_F_NAME = 1
_F_PRICE = 3
_F_PREV_CLOSE = 4
_F_OPEN = 5
_F_VOLUME = 6
_F_TIME = 30
_F_HIGH = 33
_F_LOW = 34
_F_AMOUNT = 37
_F_TURNOVER = 38
_F_PE = 39
_F_STATUS = 40
"""Trading status flag: ``S`` = suspended (停牌); empty while trading."""
_MIN_FIELDS = 44
"""A row needs at least this many fields for every position read here."""

_TICKER = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_LINE = re.compile(r'^v_(?P<code>[a-z]{2}\d{6})="(?P<body>[^"]*)";?\s*$')
_CODE = re.compile(r"^(sh|sz|bj)(\d{6})$")

LOTS_TO_SHARES = Decimal(100)
WAN_TO_CNY = Decimal(10_000)
PERCENT = Decimal(100)


def to_tencent_code(ticker: str) -> str:
    """``600519.SH`` -> ``sh600519`` (``SZ`` -> ``sz``, ``BJ`` -> ``bj``)."""

    match = _TICKER.fullmatch(ticker.strip().upper())
    if match is None:
        raise ValueError(f"not a canonical A-share ticker: {ticker!r}")
    return f"{match.group(2).lower()}{match.group(1)}"


def from_tencent_code(code: str) -> str:
    """``sh600519`` -> ``600519.SH``; raises for anything else."""

    match = _CODE.fullmatch(code.strip().lower())
    if match is None:
        raise ValueError(f"not a Tencent A-share code: {code!r}")
    return f"{match.group(2)}.{match.group(1).upper()}"


@dataclass(frozen=True)
class ClosedQuote:
    """One listing's closed session as the Tencent feed reports it."""

    ticker: str
    trade_date: date
    name: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prev_close: Decimal
    volume_shares: Decimal
    """Shares (field 6 手 × 100)."""
    amount_cny: Decimal
    """CNY (field 37 万元 × 10 000)."""
    turnover: Decimal | None
    """Fraction of float shares traded (field 38 percent ÷ 100)."""
    pe: Decimal | None
    """PE TTM (field 39), ratio; ``None`` when the feed leaves it blank."""
    time: datetime
    """Quote time (field 30), Asia/Shanghai."""
    trade_status: int = 1
    """1 traded, 0 suspended (no trade on the day: OHLC = previous close)."""
    is_st: bool = False

    @property
    def suspended(self) -> bool:
        return self.trade_status == 0


@dataclass(frozen=True)
class QuoteBatch:
    """What :meth:`TencentQuoteClient.fetch_close` learned for one ticker list."""

    trade_date: date
    captured_at: datetime
    quotes: dict[str, ClosedQuote] = field(default_factory=dict)
    pending: dict[str, str] = field(default_factory=dict)
    """Ticker -> reason: the day is not closed / not quoted yet; try later."""
    missing: dict[str, str] = field(default_factory=dict)
    """Ticker -> reason: the feed has no usable quote for the trade date."""
    requests: int = 0
    raw: tuple[str, ...] = ()
    """Decoded response bodies, one per request (for the run artifact)."""


class PendingQuoteError(Exception):
    """The quote is not a closed ``trade_date`` session (reason in ``args``)."""


class MissingQuoteError(Exception):
    """The feed cannot serve ``trade_date`` for this ticker."""


def parse_quote_lines(text: str) -> dict[str, list[str]]:
    """``v_<code>="…";`` lines -> ``{code: fields}``; other lines are ignored.

    A non-empty body without a single quote line is a
    :class:`ProviderDataError` (the feed changed, or an error page came back).
    """

    rows: dict[str, list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _LINE.fullmatch(line)
        if match is None:
            continue
        body = match.group("body")
        if not body:
            continue  # ``v_sh600000="";`` - unknown code, treated as absent
        rows[match.group("code")] = body.split("~")
    if not rows and text.strip():
        raise ProviderDataError(
            f"tencent quote response has no quote lines: {text.strip()[:120]!r}",
            provider=PROVIDER,
        )
    return rows


def _decimal(fields: list[str], position: int, *, ticker: str) -> Decimal:
    text = fields[position].strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ProviderDataError(
            f"tencent quote {ticker}: field {position} is not a number: {text!r}",
            provider=PROVIDER,
        ) from None
    if not value.is_finite():
        raise ProviderDataError(
            f"tencent quote {ticker}: field {position} is not finite: {text!r}", provider=PROVIDER
        )
    return value


def _optional_decimal(fields: list[str], position: int, *, ticker: str) -> Decimal | None:
    if not fields[position].strip():
        return None
    return _decimal(fields, position, ticker=ticker)


def _quote_time(fields: list[str], *, ticker: str) -> datetime:
    text = fields[_F_TIME].strip()
    try:
        naive = datetime.strptime(text, "%Y%m%d%H%M%S")
    except ValueError:
        raise ProviderDataError(
            f"tencent quote {ticker}: field {_F_TIME} is not a YYYYMMDDHHMMSS time: {text!r}",
            provider=PROVIDER,
        ) from None
    return naive.replace(tzinfo=SHANGHAI_TZ)


def _is_st(name: str) -> bool:
    return name.strip().upper().startswith(("ST", "*ST"))


def closed_quote(
    ticker: str, fields: Sequence[str], *, trade_date: date, capture_date: date
) -> ClosedQuote:
    """Parse one quote row as the closed ``trade_date`` session (pure).

    Raises :class:`LookaheadDataError` when the quote is dated after
    ``capture_date``, :class:`PendingQuoteError` when the session is not closed yet
    (or the day not quoted yet), :class:`MissingQuoteError` when the feed already
    moved on to a later session.
    """

    row = list(fields)
    if len(row) < _MIN_FIELDS:
        raise ProviderDataError(
            f"tencent quote {ticker}: expected >= {_MIN_FIELDS} fields, got {len(row)}",
            provider=PROVIDER,
        )
    quote_time = _quote_time(row, ticker=ticker)
    quote_date = quote_time.date()
    if quote_date > capture_date:
        raise LookaheadDataError(
            f"tencent quote {ticker} is dated {quote_date.isoformat()}, after the capture "
            f"date {capture_date.isoformat()}",
            provider=PROVIDER,
        )
    if quote_date < trade_date:
        raise PendingQuoteError(
            f"latest quote is dated {quote_date.isoformat()}; "
            f"{trade_date.isoformat()} not quoted yet"
        )
    if quote_date > trade_date:
        raise MissingQuoteError(
            f"feed already serves {quote_date.isoformat()}; "
            f"{trade_date.isoformat()} is no longer available"
        )
    name = row[_F_NAME].strip()
    price = _decimal(row, _F_PRICE, ticker=ticker)
    prev_close = _decimal(row, _F_PREV_CLOSE, ticker=ticker)
    volume_lots = _decimal(row, _F_VOLUME, ticker=ticker)
    if volume_lots == 0 or row[_F_STATUS].strip().upper() == "S":
        # No trade on the day (suspended): store it the way baostock does. The last
        # price is the previous close (or 0), so it must not decide on its own.
        return ClosedQuote(
            ticker=ticker,
            trade_date=trade_date,
            name=name,
            open=prev_close,
            high=prev_close,
            low=prev_close,
            close=prev_close,
            prev_close=prev_close,
            volume_shares=Decimal(0),
            amount_cny=Decimal(0),
            turnover=Decimal(0),
            pe=_optional_decimal(row, _F_PE, ticker=ticker),
            time=quote_time,
            trade_status=0,
            is_st=_is_st(name),
        )
    if quote_time.time() < SESSION_CLOSE:
        raise PendingQuoteError(
            f"session not closed: latest quote at {quote_time.strftime('%H:%M:%S')} "
            f"(close {SESSION_CLOSE.strftime('%H:%M:%S')})"
        )
    turnover = _optional_decimal(row, _F_TURNOVER, ticker=ticker)
    return ClosedQuote(
        ticker=ticker,
        trade_date=trade_date,
        name=name,
        open=_decimal(row, _F_OPEN, ticker=ticker),
        high=_decimal(row, _F_HIGH, ticker=ticker),
        low=_decimal(row, _F_LOW, ticker=ticker),
        close=price,
        prev_close=prev_close,
        volume_shares=volume_lots * LOTS_TO_SHARES,
        amount_cny=_decimal(row, _F_AMOUNT, ticker=ticker) * WAN_TO_CNY,
        turnover=None if turnover is None else turnover / PERCENT,
        pe=_optional_decimal(row, _F_PE, ticker=ticker),
        time=quote_time,
        trade_status=1,
        is_st=_is_st(name),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TencentQuoteClient:
    """Async batch client: one GET per :attr:`chunk_size` codes.

    ``clock`` gives ``captured_at`` (and the capture date for the lookahead
    guard, in Asia/Shanghai). ``transport`` / ``base_url`` exist for tests
    (``respx`` or an ``httpx.MockTransport``); the client never hits the
    network in the test suite.
    """

    def __init__(
        self,
        clock: Callable[[], datetime] = _utc_now,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_call: ProviderCallHook | None = None,
        circuit: CircuitBreaker | None = None,
        timeout_seconds: float = 8.0,
        chunk_size: int = MAX_CODES_PER_REQUEST,
        base_url: str = BASE_URL,
        max_attempts: int = 2,
        retry_backoff_seconds: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= chunk_size <= MAX_CODES_PER_REQUEST:
            raise ValueError(f"chunk_size must be in 1..{MAX_CODES_PER_REQUEST}")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._clock = clock
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self.chunk_size = chunk_size
        self._base_url = base_url.rstrip("/")
        self.requests = 0
        # No throttle here (deliberate): the quote feed answers chunks
        # back-to-back, and the client never updated a ``_last_call`` either.
        self._pipeline = ProviderCallPipeline(
            provider=PROVIDER,
            retry=RetryPolicy(max_attempts=max_attempts, backoff_seconds=retry_backoff_seconds),
            circuit=circuit,
            on_call=on_call,
            monotonic=monotonic,
            sleep=sleep,
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    async def fetch_close(self, tickers: Sequence[str], trade_date: date) -> QuoteBatch:
        """Closed ``trade_date`` quotes of ``tickers`` (deduplicated, request order kept).

        Raises on the first request that fails for good (transient after the
        in-call retries, or terminal); the caller decides what a failed chunk
        means. A quote dated after the capture date raises
        :class:`LookaheadDataError` for the whole batch.
        """

        ordered = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers))
        codes = {ticker: to_tencent_code(ticker) for ticker in ordered}
        captured_at = self._now()
        capture_date = captured_at.astimezone(SHANGHAI_TZ).date()
        batch = QuoteBatch(trade_date=trade_date, captured_at=captured_at)
        raw: list[str] = []
        requests = 0
        for index in range(0, len(ordered), self.chunk_size):
            chunk = ordered[index : index + self.chunk_size]
            text = await self._request([codes[ticker] for ticker in chunk])
            requests += 1
            raw.append(text)
            rows = parse_quote_lines(text)
            for ticker in chunk:
                fields = rows.get(codes[ticker])
                if fields is None:
                    batch.missing[ticker] = "not in the tencent answer (unknown or delisted code)"
                    continue
                try:
                    batch.quotes[ticker] = closed_quote(
                        ticker, fields, trade_date=trade_date, capture_date=capture_date
                    )
                except PendingQuoteError as pending:
                    batch.pending[ticker] = str(pending)
                except MissingQuoteError as missing:
                    batch.missing[ticker] = str(missing)
        return QuoteBatch(
            trade_date=trade_date,
            captured_at=captured_at,
            quotes=batch.quotes,
            pending=batch.pending,
            missing=batch.missing,
            requests=requests,
            raw=tuple(raw),
        )

    # -- plumbing ---------------------------------------------------------- #

    async def _request(self, codes: Sequence[str]) -> str:
        """One logical request: circuit-guarded, retried once on transient errors.

        The shared pipeline (``transport.py``) owns the circuit check before
        the first attempt, the retry/backoff loop and the one ledger event per
        logical request; the attempt below stays native-async httpx.
        """

        async def attempt(_context: Attempt) -> str:
            return await self._attempt(codes)

        def log_retry(context: Attempt, exc: BaseException) -> None:
            log.warning(
                "tencent_quote.retry",
                attempt=context.number,
                codes=len(codes),
                error=f"{type(exc).__name__}: {exc}",
            )

        return await self._pipeline.run(
            ENDPOINT, attempt, ticker_count=len(codes), on_retry=log_retry
        )

    async def _attempt(self, codes: Sequence[str]) -> str:
        self.requests += 1
        return await self._get(codes)

    async def _get(self, codes: Sequence[str]) -> str:
        url = f"{self._base_url}/q={','.join(codes)}"
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(
                f"tencent quote request timed out after {self._timeout_seconds}s",
                provider=PROVIDER,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderTransientError(
                f"tencent quote request failed: {type(exc).__name__}: {exc}", provider=PROVIDER
            ) from exc
        status = response.status_code
        if status >= 500 or status == 429:
            raise ProviderTransientError(
                f"tencent quote HTTP {status}", provider=PROVIDER, error_code=str(status)
            )
        if status >= 400:
            raise ProviderDataError(
                f"tencent quote HTTP {status}", provider=PROVIDER, error_code=str(status)
            )
        try:
            return response.content.decode(ENCODING)
        except UnicodeDecodeError as exc:
            raise ProviderDataError(
                f"tencent quote response is not {ENCODING}: {exc}", provider=PROVIDER
            ) from exc


__all__ = [
    "BASE_URL",
    "ENDPOINT",
    "MAX_CODES_PER_REQUEST",
    "PROVIDER",
    "SESSION_CLOSE",
    "ClosedQuote",
    "QuoteBatch",
    "TencentQuoteClient",
    "closed_quote",
    "from_tencent_code",
    "parse_quote_lines",
    "to_tencent_code",
]

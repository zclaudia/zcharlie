"""AkShare adapter: Shenwan 2021 (申万 2021) industry history and code directory (P1-A9).

Capabilities (docs/product/p1-plan.md 2.2):

* :meth:`AkshareShenwanClient.fetch_industry_history` -
  ``stock_industry_clf_hist_sw`` (one ~1 MB spreadsheet from swsresearch.com,
  the whole market): per stock an ordered list of ``(start_date, 6-digit
  code)``, i.e. point-in-time industry history.
* :meth:`AkshareShenwanClient.fetch_code_directory` - the code -> name
  directory from Shenwan's own code table ``SwClassCode_2021.xls`` (same
  host, same TLS scope; AkShare has no wrapper for it): 511 codes, levels 1-3,
  one request.
* :meth:`AkshareShenwanClient.reverse_lookup_directory` - the approach
  planned in p1-plan 2.2, kept as a cross-check: ``sw_index_*_info`` plus one
  ``sw_index_third_cons`` call per level-3 index (legulegu.com), each index
  voting for the code its current constituents carry. legulegu throttles
  bursts (HTTP 429, then 504 for minutes), so a full run takes long.

Both directory paths are used by ``tools/refresh_shenwan_directory.py`` only;
runtime reads the committed ``config/shenwan_2021.yaml``.

Ticker mapping (:func:`to_canonical_ticker`), checked on 2026-10-06 against
all 5930 history symbols (prefixes 000-003, 300-302, 600-605, 688/689, 832,
833, 874, 920) and the suffixed tickers legulegu returns for level-3
constituents: ``6xxxxx`` / ``900xxx`` -> ``.SH``; ``0xxxxx`` / ``2xxxxx`` /
``3xxxxx`` -> ``.SZ``; ``920xxx`` / ``4xxxxx`` / ``8xxxxx`` -> ``.BJ``. The
history holds no B shares today; other prefixes are skipped and reported.

TLS: swsresearch.com serves its leaf certificate without the intermediate,
so default verification fails. :func:`swsresearch_tls` patches the
``requests`` name inside the AkShare modules used here (never globally) for
the duration of one call: requests to ``swsresearch.com`` verify against
certifi's bundle plus the intermediate shipped in ``providers/certs/``;
every other host keeps the default verification. The shim also adds a
timeout and turns HTTP / network failures into typed errors.

Errors: :class:`ShenwanTransientError` (network, timeout, HTTP 5xx / 429) is
retried; :class:`ShenwanSchemaError` (columns changed, unparsable payload)
and :class:`ShenwanError` (TLS failure, HTTP 4xx) are terminal. Point in
time: a ``start_date`` after the capture date (Asia/Shanghai) raises
:class:`LookaheadDataError`.

``akshare`` is imported lazily inside :class:`AkshareShenwanBackend`, so
offline tests never import it.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cache, partial
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import structlog

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.providers.errors import (
    LookaheadDataError,
    ProviderDataError,
    ProviderError,
    ProviderTransientError,
)
from investment_agent.providers.shenwan_directory import (
    SW2021_SYSTEM,
    ShenwanDirectory,
    ShenwanIndustry,
    build_directory,
    is_sw_code,
    level_codes,
)

log = structlog.get_logger(__name__)

PROVIDER = "akshare"
SWSRESEARCH_DOMAIN = "swsresearch.com"
INTERMEDIATE_CERT_PATH = Path(__file__).resolve().parent / "certs" / "swsresearch-intermediate.pem"

CLASS_CODE_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/SwClassCode_2021.xls"
CLASS_CODE_COLUMNS = frozenset({"行业代码", "一级行业名称", "二级行业名称", "三级行业名称"})
HISTORY_COLUMNS = frozenset({"symbol", "start_date", "industry_code"})
INFO_COLUMNS = frozenset({"行业代码", "行业名称", "成份个数"})
PARENT_INFO_COLUMNS = INFO_COLUMNS | {"上级行业"}
CONS_COLUMNS = frozenset({"股票代码"})

InfoLevel = Literal[1, 2, 3]

# AkShare modules whose module-level ``requests`` is patched by swsresearch_tls.
_AKSHARE_MODULES = ("akshare.stock.stock_industry_sw", "akshare.index.index_sw")
_PATCH_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ShenwanError(ProviderError):
    """Terminal AkShare / Shenwan failure (TLS, HTTP 4xx)."""


class ShenwanTransientError(ProviderTransientError):
    """Network / timeout / HTTP 5xx; worth retrying later."""


class ShenwanSchemaError(ProviderDataError):
    """The answer no longer has the expected shape (site redesign)."""


# --------------------------------------------------------------------------- #
# Ticker mapping
# --------------------------------------------------------------------------- #


def to_canonical_ticker(symbol: str) -> str | None:
    """``600519`` -> ``600519.SH``; ``None`` for an unknown prefix (see module doc)."""

    code = symbol.strip()
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(("6", "900")):
        return f"{code}.SH"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"
    if code.startswith(("920", "4", "8")):
        return f"{code}.BJ"
    return None


# --------------------------------------------------------------------------- #
# TLS scope for swsresearch.com
# --------------------------------------------------------------------------- #


@cache
def combined_ca_bundle() -> Path:
    """certifi's bundle + the swsresearch intermediate, written once per process."""

    import certifi

    content = (
        Path(certifi.where()).read_text(encoding="utf-8")
        + "\n"
        + INTERMEDIATE_CERT_PATH.read_text(encoding="utf-8")
    )
    directory = Path(tempfile.mkdtemp(prefix="ia-swsresearch-ca-"))
    target = directory / "ca-bundle.pem"
    target.write_text(content, encoding="utf-8")
    return target


def is_swsresearch_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == SWSRESEARCH_DOMAIN or host.endswith("." + SWSRESEARCH_DOMAIN)


class _ScopedRequests:
    """Stand-in for the ``requests`` module inside one AkShare module."""

    def __init__(self, real: ModuleType, *, timeout: float, bundle: Path) -> None:
        self._real = real
        self._timeout = timeout
        self._bundle = bundle

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self._timeout)
        if is_swsresearch_url(url):
            kwargs.setdefault("verify", str(self._bundle))
        exceptions = self._real.exceptions
        try:
            response = self._real.get(url, *args, **kwargs)
        except exceptions.SSLError as exc:
            raise ShenwanError(
                f"TLS verification failed for {url}: {exc}. If the site changed its "
                "certificate chain, refresh providers/certs/swsresearch-intermediate.pem",
                provider=PROVIDER,
                error_code="tls",
            ) from exc
        except (exceptions.ConnectionError, exceptions.Timeout) as exc:
            raise ShenwanTransientError(
                f"network error fetching {url}: {exc}", provider=PROVIDER
            ) from exc
        status = int(getattr(response, "status_code", 200))
        if status >= 500 or status == 429:
            raise ShenwanTransientError(
                f"HTTP {status} from {url}", provider=PROVIDER, error_code=str(status)
            )
        if status >= 400:
            raise ShenwanError(f"HTTP {status} from {url}", provider=PROVIDER, error_code=str(status))
        return response


@contextmanager
def swsresearch_tls(
    modules: Sequence[ModuleType], *, timeout: float = 60.0, bundle: Path | None = None
) -> Iterator[None]:
    """Route ``requests.get`` of ``modules`` through :class:`_ScopedRequests`.

    Only the given modules' ``requests`` attribute is replaced, under a
    process-wide lock, and restored on exit. Verification is never disabled.
    """

    ca_bundle = bundle or combined_ca_bundle()
    with _PATCH_LOCK:
        originals = [(module, module.__dict__["requests"]) for module in modules]
        try:
            for module, real in originals:
                setattr(module, "requests", _ScopedRequests(real, timeout=timeout, bundle=ca_bundle))  # noqa: B010
            yield
        finally:
            for module, real in originals:
                setattr(module, "requests", real)  # noqa: B010


# --------------------------------------------------------------------------- #
# Backend (real AkShare or an offline fake)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawTable:
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]


class ShenwanBackend(Protocol):
    def industry_history(self) -> RawTable: ...

    def class_code_table(self) -> RawTable: ...

    def index_info(self, level: InfoLevel) -> RawTable: ...

    def third_constituents(self, index_code: str) -> RawTable: ...


def _json_value(value: Any) -> Any:
    """pandas cell -> JSON-friendly value (dates as ISO strings, NaN/NaT -> None)."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value != value:  # NaN
        return None
    if hasattr(value, "isoformat"):  # pandas Timestamp
        try:
            return value.isoformat()
        except ValueError:  # NaT
            return None
    if type(value).__name__ in {"NaTType", "NAType"}:
        return None
    if hasattr(value, "item"):  # numpy scalar
        return _json_value(value.item())
    return value


def _table(frame: Any) -> RawTable:
    columns = tuple(str(column) for column in frame.columns)
    rows = tuple(
        {column: _json_value(value) for column, value in zip(columns, record, strict=True)}
        for record in frame.itertuples(index=False, name=None)
    )
    return RawTable(columns=columns, rows=rows)


class AkshareShenwanBackend:
    """Calls AkShare inside :func:`swsresearch_tls` (blocking; run in a thread)."""

    def __init__(self, *, http_timeout: float = 60.0) -> None:
        self._http_timeout = http_timeout

    def _call(self, func_name: str, *args: Any) -> RawTable:
        import importlib

        import akshare

        modules = [importlib.import_module(name) for name in _AKSHARE_MODULES]
        func = getattr(akshare, func_name)
        with swsresearch_tls(modules, timeout=self._http_timeout):
            try:
                frame = func(*args)
            except ProviderError:
                raise
            except (ValueError, KeyError, AttributeError, IndexError, TypeError) as exc:
                raise ShenwanSchemaError(
                    f"akshare {func_name} could not parse the answer: {exc!r}",
                    provider=PROVIDER,
                ) from exc
        return _table(frame)

    def industry_history(self) -> RawTable:
        return self._call("stock_industry_clf_hist_sw")

    def class_code_table(self) -> RawTable:
        """Shenwan's own code table: AkShare has no wrapper; same host and TLS scope."""

        import io

        import pandas  # type: ignore[import-untyped]
        import requests
        from akshare.utils.cons import headers

        scoped = _ScopedRequests(requests, timeout=self._http_timeout, bundle=combined_ca_bundle())
        response = scoped.get(CLASS_CODE_URL, headers=headers)
        try:
            frame = pandas.read_excel(io.BytesIO(response.content), dtype=str)
        except (ValueError, KeyError, ImportError) as exc:
            raise ShenwanSchemaError(
                f"{CLASS_CODE_URL} is not a readable spreadsheet: {exc!r}", provider=PROVIDER
            ) from exc
        return _table(frame)

    def index_info(self, level: InfoLevel) -> RawTable:
        name = {1: "sw_index_first_info", 2: "sw_index_second_info", 3: "sw_index_third_info"}
        return self._call(name[level])

    def third_constituents(self, index_code: str) -> RawTable:
        return self._call("sw_index_third_cons", index_code)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShenwanChange:
    start_date: date
    code: str
    updated: date | None = None


@dataclass(frozen=True)
class ShenwanHistory:
    captured_at: datetime
    changes: Mapping[str, tuple[ShenwanChange, ...]]
    """Canonical ticker -> changes ordered by ``start_date`` (strictly increasing)."""
    skipped_symbols: tuple[str, ...] = ()
    """Symbols whose exchange could not be inferred."""
    raw_rows: tuple[Mapping[str, Any], ...] = field(default=(), repr=False)
    system: str = SW2021_SYSTEM
    source: str = PROVIDER

    def current_codes(self) -> dict[str, str]:
        return {ticker: items[-1].code for ticker, items in self.changes.items() if items}

    def code_as_of(self, ticker: str, day: date) -> str | None:
        code = None
        for change in self.changes.get(ticker, ()):
            if change.start_date > day:
                break
            code = change.code
        return code

    @property
    def latest_start_date(self) -> date | None:
        return max(
            (items[-1].start_date for items in self.changes.values() if items), default=None
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _check_columns(table: RawTable, expected: frozenset[str], *, operation: str) -> None:
    missing = expected - set(table.columns)
    if missing:
        raise ShenwanSchemaError(
            f"akshare {operation} answer lacks columns {sorted(missing)}; "
            f"got {list(table.columns)}",
            provider=PROVIDER,
        )


def _parse_day(raw: Any, *, field_name: str, required: bool) -> date | None:
    if raw is None or raw == "":
        if required:
            raise ShenwanSchemaError(f"missing {field_name}", provider=PROVIDER)
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise ShenwanSchemaError(
                f"invalid {field_name} {raw!r}", provider=PROVIDER
            ) from exc


def _symbol_text(raw: Any) -> str:
    text = str(raw).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


def parse_history(
    table: RawTable, *, captured_at: datetime
) -> ShenwanHistory:
    """Validate and group ``stock_industry_clf_hist_sw`` rows (pure)."""

    operation = "stock_industry_clf_hist_sw"
    _check_columns(table, HISTORY_COLUMNS, operation=operation)
    if not table.rows:
        raise ShenwanSchemaError(f"akshare {operation} returned no rows", provider=PROVIDER)
    capture_date = captured_at.astimezone(SHANGHAI_TZ).date()
    by_ticker: dict[str, dict[date, ShenwanChange]] = {}
    skipped: set[str] = set()
    for row in table.rows:
        symbol = _symbol_text(row.get("symbol"))
        code = _symbol_text(row.get("industry_code"))
        if not is_sw_code(code):
            raise ShenwanSchemaError(
                f"akshare {operation}: invalid industry code {row.get('industry_code')!r} "
                f"for {symbol}",
                provider=PROVIDER,
            )
        start = _parse_day(row.get("start_date"), field_name="start_date", required=True)
        assert start is not None
        updated = _parse_day(row.get("update_time"), field_name="update_time", required=False)
        if start > capture_date:
            raise LookaheadDataError(
                f"akshare {operation}: {symbol} start_date {start.isoformat()} is after the "
                f"capture date {capture_date.isoformat()}",
                provider=PROVIDER,
            )
        ticker = to_canonical_ticker(symbol)
        if ticker is None:
            skipped.add(symbol)
            continue
        changes = by_ticker.setdefault(ticker, {})
        change = ShenwanChange(start_date=start, code=code, updated=updated)
        previous = changes.get(start)
        if previous is not None and (previous.updated or date.min) > (updated or date.min):
            continue  # same day twice: the later update wins
        changes[start] = change
    if skipped:
        log.warning(
            "shenwan_symbols_skipped", count=len(skipped), sample=sorted(skipped)[:10]
        )
    return ShenwanHistory(
        captured_at=captured_at,
        changes={
            ticker: tuple(changes[day] for day in sorted(changes))
            for ticker, changes in sorted(by_ticker.items())
        },
        skipped_symbols=tuple(sorted(skipped)),
        raw_rows=table.rows,
    )


# --------------------------------------------------------------------------- #
# Directory: official code table (pure part)
# --------------------------------------------------------------------------- #


def parse_class_code_table(
    table: RawTable, *, version: str, captured_on: date | None = None
) -> ShenwanDirectory:
    """``SwClassCode_2021.xls`` rows -> directory (level and parent from the code)."""

    operation = "SwClassCode_2021"
    _check_columns(table, CLASS_CODE_COLUMNS, operation=operation)
    name_columns = {1: "一级行业名称", 2: "二级行业名称", 3: "三级行业名称"}
    industries = []
    for row in table.rows:
        code = _symbol_text(row.get("行业代码"))
        if not is_sw_code(code):
            raise ShenwanSchemaError(
                f"{operation}: invalid code {row.get('行业代码')!r}", provider=PROVIDER
            )
        l1, l2, _ = level_codes(code)
        level = 1 if code == l1 else 2 if code == l2 else 3
        name = str(row.get(name_columns[level]) or "").strip()
        if not name:
            raise ShenwanSchemaError(f"{operation}: {code} has no name", provider=PROVIDER)
        parent = None if level == 1 else l1 if level == 2 else l2
        industries.append(ShenwanIndustry(code, name, level, parent))
    if not industries:
        raise ShenwanSchemaError(f"{operation} returned no rows", provider=PROVIDER)
    try:
        return build_directory(
            industries,
            version=version,
            captured_on=captured_on,
            source=f"swsresearch.com {CLASS_CODE_URL.rsplit('/', 1)[-1]}",
        )
    except ValueError as exc:
        raise ShenwanSchemaError(f"{operation}: {exc}", provider=PROVIDER) from exc


# --------------------------------------------------------------------------- #
# Directory reverse lookup via legulegu (cross-check only, pure part)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IndexInfo:
    index_code: str
    name: str
    parent_name: str | None
    members: int | None


def parse_index_info(table: RawTable, level: InfoLevel) -> list[IndexInfo]:
    operation = f"sw_index_info(level={level})"
    _check_columns(table, INFO_COLUMNS if level == 1 else PARENT_INFO_COLUMNS, operation=operation)
    if not table.rows:
        raise ShenwanSchemaError(f"akshare {operation} returned no rows", provider=PROVIDER)
    result = []
    for row in table.rows:
        members = row.get("成份个数")
        result.append(
            IndexInfo(
                index_code=str(row["行业代码"]).strip(),
                name=str(row["行业名称"]).strip(),
                parent_name=str(row["上级行业"]).strip() if level > 1 else None,
                members=int(members) if isinstance(members, int | float) else None,
            )
        )
    return result


@dataclass(frozen=True)
class ReverseLookup:
    index_code: str
    code: str | None
    agreement: float
    members: int


def vote_code(
    index_code: str, tickers: Sequence[str], current: Mapping[str, str]
) -> ReverseLookup:
    """The level-3 code most constituents of ``index_code`` currently carry."""

    votes = Counter(current[ticker] for ticker in tickers if ticker in current)
    if not votes:
        return ReverseLookup(index_code, None, 0.0, len(tickers))
    code, count = votes.most_common(1)[0]
    return ReverseLookup(index_code, code, count / sum(votes.values()), len(tickers))


def assemble_directory(
    *,
    first: Sequence[IndexInfo],
    second: Sequence[IndexInfo],
    third: Sequence[IndexInfo],
    lookups: Sequence[ReverseLookup],
    version: str,
    captured_on: date,
) -> ShenwanDirectory:
    """Combine the info tables and the level-3 reverse lookups (pure)."""

    third_by_index = {info.index_code: info for info in third}
    second_by_name: dict[str, IndexInfo] = {}
    for info in second:
        second_by_name.setdefault(info.name, info)
    first_by_name = {info.name: info for info in first}

    industries: dict[str, ShenwanIndustry] = {}
    unresolved: list[str] = []

    def put(entry: ShenwanIndustry) -> None:
        existing = industries.get(entry.code)
        if existing is not None and existing.name != entry.name:
            log.warning(
                "shenwan_directory_conflict",
                code=entry.code,
                kept=existing.name,
                other=entry.name,
            )
            return
        industries.setdefault(entry.code, entry)

    for lookup in lookups:
        third_info = third_by_index.get(lookup.index_code)
        if third_info is None or lookup.code is None:
            unresolved.append(lookup.index_code)
            continue
        if lookup.agreement < 0.5:
            log.warning(
                "shenwan_reverse_lookup_ambiguous",
                index_code=lookup.index_code,
                code=lookup.code,
                agreement=round(lookup.agreement, 3),
            )
        l1_code, l2_code, l3_code = level_codes(lookup.code)
        second_info = second_by_name.get(third_info.parent_name or "")
        first_info = (
            first_by_name.get(second_info.parent_name or "") if second_info is not None else None
        )
        if second_info is None or first_info is None:
            log.warning(
                "shenwan_parent_unresolved",
                index_code=lookup.index_code,
                code=lookup.code,
                parent=third_info.parent_name,
            )
            unresolved.append(lookup.index_code)
            continue
        put(ShenwanIndustry(l1_code, first_info.name, 1, None, first_info.index_code))
        put(ShenwanIndustry(l2_code, second_info.name, 2, l1_code, second_info.index_code))
        put(ShenwanIndustry(l3_code, third_info.name, 3, l2_code, third_info.index_code))
    resolved_indexes = {lookup.index_code for lookup in lookups}
    unresolved.extend(code for code in third_by_index if code not in resolved_indexes)
    return build_directory(
        industries.values(),
        version=version,
        captured_on=captured_on,
        source="akshare sw_index_first/second/third_info + sw_index_third_cons "
        "+ stock_industry_clf_hist_sw",
        unresolved_index_codes=unresolved,
    )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class AkshareShenwanClient:
    """Async facade: every AkShare call runs in a worker thread with retries."""

    def __init__(
        self,
        backend: ShenwanBackend | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timeout_seconds: float = 180.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 2.0,
        constituent_pause_seconds: float = 0.3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._backend = backend
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._constituent_pause_seconds = constituent_pause_seconds
        self._sleep = sleep

    async def fetch_industry_history(self) -> ShenwanHistory:
        backend = self._resolve_backend()
        table = await self._with_retries("stock_industry_clf_hist_sw", backend.industry_history)
        history = parse_history(table, captured_at=self._now())
        log.info(
            "shenwan_history_fetched",
            rows=len(table.rows),
            tickers=len(history.changes),
            skipped=len(history.skipped_symbols),
            latest_start_date=(
                history.latest_start_date.isoformat() if history.latest_start_date else None
            ),
        )
        return history

    async def fetch_code_directory(self, *, version: str | None = None) -> ShenwanDirectory:
        """The official 2021 code table (``SwClassCode_2021.xls``): one request."""

        backend = self._resolve_backend()
        table = await self._with_retries("SwClassCode_2021", backend.class_code_table)
        captured_on = self._now().astimezone(SHANGHAI_TZ).date()
        directory = parse_class_code_table(
            table, version=version or captured_on.isoformat(), captured_on=captured_on
        )
        log.info(
            "shenwan_directory_fetched",
            level1=len(directory.codes(1)),
            level2=len(directory.codes(2)),
            level3=len(directory.codes(3)),
        )
        return directory

    async def reverse_lookup_directory(
        self,
        history: ShenwanHistory,
        *,
        index_codes: Sequence[str] | None = None,
        version: str | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[ShenwanDirectory, list[ReverseLookup], dict[str, list[str]]]:
        """Directory rebuilt from legulegu.com - a cross-check of the official table.

        Per level-3 index (all, or only ``index_codes``) its current
        constituents vote for the code they carry in ``history``. Slow:
        legulegu answers bursts with HTTP 429 / 504. Also returns the lookups
        and the raw constituents.
        """

        backend = self._resolve_backend()
        current = history.current_codes()
        infos: dict[int, list[IndexInfo]] = {}
        levels: tuple[InfoLevel, ...] = (1, 2, 3)
        for level in levels:
            table = await self._with_retries(
                f"sw_index_info(level={level})", partial(backend.index_info, level)
            )
            infos[level] = parse_index_info(table, level)
        lookups: list[ReverseLookup] = []
        constituents: dict[str, list[str]] = {}
        wanted = set(index_codes) if index_codes is not None else None
        third = [info for info in infos[3] if wanted is None or info.index_code in wanted]
        failures = 0
        for position, info in enumerate(third, start=1):
            if progress is not None:
                progress(position, len(third), info.index_code)
            if info.members == 0:
                lookups.append(ReverseLookup(info.index_code, None, 0.0, 0))
                continue
            try:
                table = await self._with_retries(
                    "sw_index_third_cons", partial(backend.third_constituents, info.index_code)
                )
            except ShenwanTransientError as exc:
                # One broken index page must not sink the whole directory: it
                # stays unresolved (listed in the file) and is retried next time.
                log.warning(
                    "shenwan_constituents_unavailable", index_code=info.index_code, error=str(exc)
                )
                failures += 1
                if failures > max(3, len(third) // 10):
                    raise
                lookups.append(ReverseLookup(info.index_code, None, 0.0, 0))
                continue
            _check_columns(table, CONS_COLUMNS, operation="sw_index_third_cons")
            tickers = [str(row["股票代码"]).strip().upper() for row in table.rows]
            constituents[info.index_code] = tickers
            lookups.append(vote_code(info.index_code, tickers, current))
            if self._constituent_pause_seconds:
                await self._sleep(self._constituent_pause_seconds)
        captured_on = self._now().astimezone(SHANGHAI_TZ).date()
        directory = assemble_directory(
            first=infos[1],
            second=infos[2],
            third=third,
            lookups=lookups,
            version=version or captured_on.isoformat(),
            captured_on=captured_on,
        )
        return directory, lookups, constituents

    # -- plumbing ---------------------------------------------------------- #

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    def _resolve_backend(self) -> ShenwanBackend:
        if self._backend is None:
            self._backend = AkshareShenwanBackend()
        return self._backend

    async def _with_retries[T](self, operation: str, call: Callable[[], T]) -> T:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(call), timeout=self._timeout_seconds
                )
            except TimeoutError as exc:
                error: ShenwanTransientError = ShenwanTransientError(
                    f"akshare {operation} timed out after {self._timeout_seconds}s",
                    provider=PROVIDER,
                )
                error.__cause__ = exc
            except ShenwanTransientError as exc:
                error = exc
            if attempt >= self._max_attempts:
                raise error
            log.warning(
                "akshare_transient_retry", operation=operation, attempt=attempt, error=str(error)
            )
            await self._sleep(self._retry_backoff_seconds * attempt)

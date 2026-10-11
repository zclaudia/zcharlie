"""Controlled HK/US security identities and the market registry behind them.

The authority for a HK/US security identity is the hand-checked list file
(``config/research_material/pilot_list.yaml``), never a model answer and never
a provider response: market, ticker, security type, quote/report currency and
per-source symbols were each checked against the saved research evidence.
:func:`load_pilot_identities` rejects the whole file on a missing field, a
duplicate ``(market_code, ticker)``, a source symbol bound to two securities, a
``quote_currency`` that disagrees with the registry, or a US/HK identity that
binds no source of its market.

Identity rules carried by this module:

* ``SecurityMarket.quote_currency`` is the market's default *quote* currency; a
  security's reporting currency is carried separately on
  :class:`ControlledIdentity` and is never derived from it.
* A ticker is stored in one canonical form per market (HKEX: five digits with
  leading zeros; NASDAQ/NYSE: upper-case). Source symbols are *not* derived
  from it: each source keeps its own code (``00005.HK`` vs ``0005.HK`` vs CIK),
  converted only by an explicit per-source mapping.
* Companies are shared only through the explicit ``company_key`` of the list.
  Similar names or similar tickers never merge two issuers.

A-share identities stay in :mod:`investment_agent.domain.a_share`; this module
adds the registry for the exchanges the pilot list uses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PILOT_LIST_PATH = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "research_material"
    / "pilot_list.yaml"
)

SOURCE_EASTMONEY_HK = "eastmoney.hk.native.v1"
SOURCE_SEC_COMPANYFACTS = "sec.companyfacts.v1"
SOURCE_YAHOO_CHART = "yahoo.chart.v1"

#: Source ids a HK identity must bind at least one of (see the plan's T1.2).
HK_SOURCE_IDS: tuple[str, ...] = (SOURCE_EASTMONEY_HK, SOURCE_YAHOO_CHART)
#: Source ids a US identity must bind at least one of.
US_SOURCE_IDS: tuple[str, ...] = (SOURCE_SEC_COMPANYFACTS,)

_CURRENCY = re.compile(r"^[A-Z]{3}$")
_US_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,15}$")
_HK_SUFFIX = ".HK"
_US_SUFFIX = ".US"


class PilotListError(ValueError):
    """The hand-checked identity list cannot be used as an authority."""


class SecurityType(StrEnum):
    """Instrument type of the listed security (not of its issuer)."""

    ORDINARY = "ordinary"
    ADS = "ads"


@dataclass(frozen=True)
class SecurityMarket:
    """Registry row for an exchange the pilot list uses.

    ``quote_currency`` is the market default for prices only. It must not be
    used as a stand-in for a security's reporting currency.
    """

    code: str
    name: str
    timezone: str
    quote_currency: str


MARKETS: dict[str, SecurityMarket] = {
    "HKEX": SecurityMarket(
        code="HKEX",
        name="Hong Kong Stock Exchange",
        timezone="Asia/Hong_Kong",
        quote_currency="HKD",
    ),
    "NASDAQ": SecurityMarket(
        code="NASDAQ",
        name="NASDAQ Stock Market",
        timezone="America/New_York",
        quote_currency="USD",
    ),
    "NYSE": SecurityMarket(
        code="NYSE",
        name="New York Stock Exchange",
        timezone="America/New_York",
        quote_currency="USD",
    ),
}


@dataclass(frozen=True)
class SourceBinding:
    """One source and the symbol it uses for this security.

    ``symbol`` is the code *that source* expects: the Eastmoney security code
    (``00005.HK``), the Yahoo chart symbol (``0005.HK``), or the SEC CIK in
    decimal (an issuer identifier, not a share-class or exchange identifier).
    """

    source: str
    symbol: str
    note: str = ""


@dataclass(frozen=True)
class ControlledIdentity:
    """A hand-checked security identity; the unit the pilot list stores."""

    market_code: str
    ticker: str
    company_key: str
    company_name: str
    company_name_zh: str
    security_type: SecurityType
    quote_currency: str
    report_currency: str
    fiscal_year_end_note: str
    sources: tuple[SourceBinding, ...]
    valid_from: date
    valid_to: date | None = None
    ads_ratio: str | None = None
    notes: str = ""


def normalize_ticker(market_code: str, raw: str) -> str:
    """Return the stored ticker for ``raw`` on ``market_code``.

    HKEX accepts ``00002.HK`` / ``2`` / ``0002`` and stores five digits with
    leading zeros. NASDAQ/NYSE accept ``aapl.US`` / ``aapl`` and store upper
    case. Any market outside :data:`MARKETS` raises ``ValueError``.
    """

    market = MARKETS.get(market_code)
    if market is None:
        raise ValueError(f"unknown market for ticker {raw!r}: {market_code!r}")
    text = raw.strip()
    if not text:
        raise ValueError(f"empty ticker for market {market_code!r}")
    if market_code == "HKEX":
        if text.upper().endswith(_HK_SUFFIX):
            text = text[: -len(_HK_SUFFIX)]
        if not text.isdigit() or not 1 <= len(text) <= 5:
            raise ValueError(f"HKEX ticker must be 1-5 digits or end in .HK: {raw!r}")
        return text.zfill(5)
    if text.upper().endswith(_US_SUFFIX):
        text = text[: -len(_US_SUFFIX)]
    symbol = text.upper()
    if not _US_TICKER.fullmatch(symbol):
        raise ValueError(f"{market.code} ticker must start with a letter: {raw!r}")
    return symbol


def load_pilot_identities(path: Path) -> tuple[ControlledIdentity, ...]:
    """Read and fully validate the hand-checked identity list at ``path``."""

    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise PilotListError(f"{path}: pilot list must be a YAML mapping")
    entries = payload.get("identities")
    if not isinstance(entries, list):
        raise PilotListError(f"{path}: 'identities' must be a list")
    identities = tuple(
        _identity_from_mapping(entry, where=f"{path}:identities[{index}]")
        for index, entry in enumerate(entries)
    )
    validate_pilot_list(identities)
    return identities


def validate_pilot_list(identities: Sequence[ControlledIdentity]) -> None:
    """Reject a list that cannot serve as an identity authority.

    Raises :class:`PilotListError` on an empty list, a market outside the
    registry, a non-canonical ticker, a missing company key/name, a
    ``quote_currency`` (or malformed currency) that disagrees with the
    registry, an end date before the start date, an ADS without its ratio (or
    an ordinary share carrying one), a missing source binding, a duplicate
    ``(market_code, ticker)``, or one source symbol bound to two securities.
    """

    if not identities:
        raise PilotListError("pilot list contains no identities")
    by_listing: dict[tuple[str, str], str] = {}
    by_symbol: dict[tuple[str, str], str] = {}
    for identity in identities:
        where = f"{identity.market_code}/{identity.ticker}"
        if identity.market_code not in MARKETS:
            raise PilotListError(f"{where}: market is not in the HK/US registry")
        if normalize_ticker(identity.market_code, identity.ticker) != identity.ticker:
            raise PilotListError(f"{where}: ticker is not the canonical stored code")
        if not identity.company_key.strip():
            raise PilotListError(f"{where}: company_key is required")
        if not identity.company_name.strip():
            raise PilotListError(f"{where}: company_name is required")
        market = MARKETS[identity.market_code]
        if identity.quote_currency != market.quote_currency:
            raise PilotListError(
                f"{where} ({identity.company_name}): quote_currency "
                f"{identity.quote_currency!r} disagrees with the {market.code} "
                f"registry value {market.quote_currency!r}"
            )
        for label, currency in (
            ("quote_currency", identity.quote_currency),
            ("report_currency", identity.report_currency),
        ):
            if not _CURRENCY.fullmatch(currency):
                raise PilotListError(
                    f"{where} ({identity.company_name}): {label} {currency!r} "
                    "must be a three-letter upper-case code"
                )
        if identity.valid_to is not None and identity.valid_to < identity.valid_from:
            raise PilotListError(f"{where}: valid_to precedes valid_from")
        if identity.security_type is SecurityType.ADS:
            if not identity.ads_ratio:
                raise PilotListError(
                    f"{where} ({identity.company_name}): an ADS needs its "
                    "verified underlying-share ratio"
                )
        elif identity.ads_ratio is not None:
            raise PilotListError(
                f"{where} ({identity.company_name}): ads_ratio is set on an "
                "ordinary share"
            )
        if not identity.sources:
            raise PilotListError(f"{where}: at least one source binding is required")
        wanted = HK_SOURCE_IDS if identity.market_code == "HKEX" else US_SOURCE_IDS
        bound = {binding.source for binding in identity.sources}
        if not bound.intersection(wanted):
            raise PilotListError(
                f"{where} ({identity.company_name}): needs a source binding in {wanted}"
            )
        key = (identity.market_code, identity.ticker)
        if key in by_listing:
            raise PilotListError(
                f"{where}: duplicate (market_code, ticker), first seen with "
                f"{by_listing[key]}"
            )
        by_listing[key] = identity.company_key
        for binding in identity.sources:
            if not binding.source.strip() or not binding.symbol.strip():
                raise PilotListError(f"{where}: source and symbol must be non-empty")
            symbol_key = (binding.source, binding.symbol)
            if symbol_key in by_symbol:
                raise PilotListError(
                    f"{where}: {binding.source} symbol {binding.symbol!r} is "
                    f"already bound to {by_symbol[symbol_key]}; symbols are "
                    "per-source and per-security"
                )
            by_symbol[symbol_key] = f"{identity.market_code}/{identity.ticker}"


def find_identity(
    *, market_code: str, ticker: str, path: Path
) -> ControlledIdentity | None:
    """Exact ``(market_code, ticker)`` lookup; normalise ``ticker`` first.

    A non-normalised ticker is not found (``find_identity`` deliberately does
    not normalise; use :func:`normalize_ticker` at the call site).
    """

    for identity in load_pilot_identities(path):
        if identity.market_code == market_code and identity.ticker == ticker:
            return identity
    return None


def source_symbol(identity: ControlledIdentity, source: str) -> str | None:
    """The symbol ``source`` uses for ``identity``, or ``None`` if unbound."""

    for binding in identity.sources:
        if binding.source == source:
            return binding.symbol
    return None


def _identity_from_mapping(entry: object, *, where: str) -> ControlledIdentity:
    if not isinstance(entry, Mapping):
        raise PilotListError(f"{where}: identity must be a mapping")
    market_code = _text(entry, "market_code", where)
    if market_code not in MARKETS:
        raise PilotListError(f"{where}: market {market_code!r} is not in the HK/US registry")
    raw_ticker = _text(entry, "ticker", where)
    ticker = normalize_ticker(market_code, raw_ticker)
    if ticker != raw_ticker:
        raise PilotListError(
            f"{where}: ticker {raw_ticker!r} is not the canonical stored code "
            f"{ticker!r}"
        )
    security_type_text = _text(entry, "security_type", where)
    try:
        security_type = SecurityType(security_type_text)
    except ValueError as exc:
        raise PilotListError(
            f"{where}: security_type {security_type_text!r} is not one of "
            f"{[item.value for item in SecurityType]}"
        ) from exc
    return ControlledIdentity(
        market_code=market_code,
        ticker=ticker,
        company_key=_text(entry, "company_key", where),
        company_name=_text(entry, "company_name", where),
        company_name_zh=_text(entry, "company_name_zh", where),
        security_type=security_type,
        quote_currency=_text(entry, "quote_currency", where),
        report_currency=_text(entry, "report_currency", where),
        fiscal_year_end_note=_text(entry, "fiscal_year_end_note", where),
        sources=_sources_from_mapping(entry, where),
        valid_from=_date(entry, "valid_from", where),
        valid_to=_optional_date(entry, "valid_to", where),
        ads_ratio=_optional_text(entry, "ads_ratio", where),
        notes=_optional_text(entry, "notes", where) or "",
    )


def _sources_from_mapping(entry: Mapping[str, Any], where: str) -> tuple[SourceBinding, ...]:
    raw_sources = entry.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise PilotListError(f"{where}: 'sources' must be a non-empty list")
    bindings = []
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, Mapping):
            raise PilotListError(f"{where}: sources[{index}] must be a mapping")
        bindings.append(
            SourceBinding(
                source=_text(raw, "source", f"{where}:sources[{index}]"),
                symbol=_text(raw, "symbol", f"{where}:sources[{index}]"),
                note=_optional_text(raw, "note", f"{where}:sources[{index}]") or "",
            )
        )
    return tuple(bindings)


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PilotListError(f"{where}: {key} must be a non-empty string (quote it)")
    return value.strip()


def _optional_text(entry: Mapping[str, Any], key: str, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PilotListError(f"{where}: {key} must be a non-empty string when present")
    return value.strip()


def _date(entry: Mapping[str, Any], key: str, where: str) -> date:
    value = entry.get(key)
    parsed = _as_date(value)
    if parsed is None:
        raise PilotListError(f"{where}: {key} must be an ISO date (YYYY-MM-DD)")
    return parsed


def _optional_date(entry: Mapping[str, Any], key: str, where: str) -> date | None:
    value = entry.get(key)
    if value is None:
        return None
    parsed = _as_date(value)
    if parsed is None:
        raise PilotListError(f"{where}: {key} must be an ISO date (YYYY-MM-DD)")
    return parsed


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None

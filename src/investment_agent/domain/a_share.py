"""Canonical A-share symbol and exchange mappings."""

import re
from dataclasses import dataclass

_A_SHARE_TICKER = re.compile(r"^(?P<symbol>\d{6})\.(?P<market>SH|SZ|BJ)$")


@dataclass(frozen=True)
class AShareMarket:
    suffix: str
    market_code: str
    name: str
    timezone: str = "Asia/Shanghai"
    currency: str = "CNY"


A_SHARE_MARKETS: dict[str, AShareMarket] = {
    "SH": AShareMarket("SH", "SSE", "Shanghai Stock Exchange"),
    "SZ": AShareMarket("SZ", "SZSE", "Shenzhen Stock Exchange"),
    "BJ": AShareMarket("BJ", "BSE", "Beijing Stock Exchange"),
}
_MARKET_SUFFIXES = {market.market_code: suffix for suffix, market in A_SHARE_MARKETS.items()}


def parse_a_share_ticker(ticker: str) -> tuple[str, AShareMarket] | None:
    match = _A_SHARE_TICKER.fullmatch(ticker.upper())
    if match is None:
        return None
    return match.group("symbol"), A_SHARE_MARKETS[match.group("market")]


def format_listing_ticker(*, ticker: str, market_code: str) -> str:
    suffix = _MARKET_SUFFIXES.get(market_code)
    if suffix is None or not ticker.isdigit() or len(ticker) != 6:
        return ticker
    return f"{ticker}.{suffix}"

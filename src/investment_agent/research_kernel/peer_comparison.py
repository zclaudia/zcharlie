from __future__ import annotations

from statistics import median
from typing import Any

from investment_agent.research_kernel.models import normalize_number

PEER_METRICS = (
    "pe",
    "pb",
    "dividend_yield",
    "roe",
    "revenue_growth_3y",
    "net_profit_growth_3y",
    "debt_ratio",
    "max_drawdown_1y",
    "investment_score",
)

METRIC_DIRECTIONS = {
    "pe": "lower_better",
    "pb": "lower_better",
    "dividend_yield": "higher_better",
    "roe": "higher_better",
    "revenue_growth_3y": "higher_better",
    "net_profit_growth_3y": "higher_better",
    "debt_ratio": "lower_better",
    "max_drawdown_1y": "lower_better",
    "investment_score": "higher_better",
}

MARKET_CAP_BANDS = (
    ("micro", 0, 10_000_000_000),
    ("small", 10_000_000_000, 50_000_000_000),
    ("mid", 50_000_000_000, 200_000_000_000),
    ("large", 200_000_000_000, 1_000_000_000_000),
    ("mega", 1_000_000_000_000, None),
)


def _number(value: Any) -> float | None:
    try:
        return normalize_number(value)
    except ValueError:
        return None


def _metric(item: dict[str, Any], metric: str) -> float | None:
    if metric == "investment_score":
        return _number(item.get("investment_score"))
    return _number(item.get("key_metrics", {}).get(metric))


def _identity(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("symbol", "")), str(item.get("name", "")))


def market_cap_band(item: dict[str, Any]) -> str:
    value = _number(item.get("market_cap"))
    if value is None:
        value = _number(item.get("key_metrics", {}).get("market_cap"))
    if value is None:
        return "unknown"
    for label, lower, upper in MARKET_CAP_BANDS:
        if value >= lower and (upper is None or value < upper):
            return label
    return "unknown"


def _add_unique(
    peers: list[dict[str, Any]], peer: dict[str, Any], seen: set[str], max_peers: int
) -> None:
    symbol = str(peer.get("symbol", ""))
    if not symbol or symbol in seen or len(peers) >= max_peers:
        return
    peers.append(peer)
    seen.add(symbol)


def _normalized_manual_symbols(manual_peer_symbols: list[str] | None) -> list[str]:
    return [str(symbol) for symbol in manual_peer_symbols or []]


def _select_manual_peers_with_missing(
    target: dict[str, Any],
    candidates: list[dict[str, Any]],
    manual_symbols: list[str],
    max_peers: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    target_symbol = str(target.get("symbol", ""))
    peers: list[dict[str, Any]] = []
    seen: set[str] = {target_symbol}
    missing_symbols: list[str] = []
    by_symbol = {str(item.get("symbol", "")): item for item in candidates}

    for symbol in manual_symbols:
        if symbol in seen or len(peers) >= max_peers:
            missing_symbols.append(symbol)
            continue
        peer = by_symbol.get(symbol)
        if peer is None:
            missing_symbols.append(symbol)
            continue
        _add_unique(peers, peer, seen, max_peers)

    return peers, missing_symbols


def select_peers(
    target: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    manual_peer_symbols: list[str] | None = None,
    max_peers: int = 5,
) -> list[dict[str, Any]]:
    target_symbol = str(target.get("symbol", ""))
    peers: list[dict[str, Any]] = []
    seen: set[str] = {target_symbol}

    if manual_peer_symbols is not None:
        manual_symbols = _normalized_manual_symbols(manual_peer_symbols)
        peers, _missing_symbols = _select_manual_peers_with_missing(
            target,
            candidates,
            manual_symbols,
            max_peers,
        )
        return peers

    hint_values = {
        str(value) for value in target.get("company_profile", {}).get("peer_group_hints", [])
    }
    for candidate in candidates:
        symbol, name = _identity(candidate)
        if symbol in hint_values or name in hint_values:
            _add_unique(peers, candidate, seen, max_peers)

    target_industry = str(target.get("industry", ""))
    if target_industry:
        for candidate in candidates:
            if str(candidate.get("industry", "")) == target_industry:
                _add_unique(peers, candidate, seen, max_peers)

    target_strategy = str(target.get("primary_strategy", ""))
    if target_strategy:
        for candidate in candidates:
            if str(candidate.get("primary_strategy", "")) == target_strategy:
                _add_unique(peers, candidate, seen, max_peers)

    target_band = market_cap_band(target)
    if target_band != "unknown":
        for candidate in candidates:
            if market_cap_band(candidate) == target_band:
                _add_unique(peers, candidate, seen, max_peers)

    return peers


def _relative_position(target: float | None, peer_median: float | None) -> str:
    if target is None or peer_median is None:
        return "insufficient_peer_data"
    tolerance = 0.000001 if peer_median == 0 else abs(peer_median) * 0.05
    if abs(target - peer_median) <= tolerance:
        return "near_peer_median"
    if target > peer_median:
        return "above_peer_median"
    return "below_peer_median"


def compare_peer_metrics(
    target: dict[str, Any], peers: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    comparison: dict[str, dict[str, Any]] = {}
    for metric in PEER_METRICS:
        target_value = _metric(target, metric)
        peer_values = [
            value for value in (_metric(peer, metric) for peer in peers) if value is not None
        ]
        if peer_values:
            peer_median = median(peer_values)
            peer_min = min(peer_values)
            peer_max = max(peer_values)
        else:
            peer_median = None
            peer_min = None
            peer_max = None
        comparison[metric] = {
            "target": target_value,
            "peer_median": peer_median,
            "peer_min": peer_min,
            "peer_max": peer_max,
            "peer_count": len(peer_values),
            "relative_position": _relative_position(target_value, peer_median),
        }
    return comparison


def _strength(metric: str, relative_position: str) -> str | None:
    if relative_position == "near_peer_median" or relative_position == "insufficient_peer_data":
        return None
    direction = METRIC_DIRECTIONS[metric]
    if direction == "higher_better":
        return "stronger" if relative_position == "above_peer_median" else "weaker"
    return "stronger" if relative_position == "below_peer_median" else "weaker"


def _peer_summary(
    peer_group: list[dict[str, Any]], metric_comparison: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    stronger = [
        metric
        for metric, values in metric_comparison.items()
        if _strength(metric, values["relative_position"]) == "stronger"
    ]
    weaker = [
        metric
        for metric, values in metric_comparison.items()
        if _strength(metric, values["relative_position"]) == "weaker"
    ]
    metrics_with_peer_data = sum(
        1 for values in metric_comparison.values() if values["peer_count"] > 0
    )
    return {
        "peer_count": len(peer_group),
        "metrics_with_peer_data": metrics_with_peer_data,
        "stronger_than_peers": stronger,
        "weaker_than_peers": weaker,
    }


def build_peer_comparison(
    target: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    manual_peer_symbols: list[str] | None = None,
    max_peers: int = 5,
) -> dict[str, Any]:
    manual_symbols = _normalized_manual_symbols(manual_peer_symbols)
    if manual_peer_symbols is not None:
        peers, manual_missing_symbols = _select_manual_peers_with_missing(
            target,
            candidates,
            manual_symbols,
            max_peers,
        )
        peer_selection_mode = "manual"
    else:
        peers = select_peers(target, candidates, max_peers=max_peers)
        manual_missing_symbols = []
        peer_selection_mode = "automatic"
    metric_comparison = compare_peer_metrics(target, peers)
    peer_group = [
        {
            "symbol": peer.get("symbol"),
            "name": peer.get("name"),
            "industry": peer.get("industry"),
            "primary_strategy": peer.get("primary_strategy"),
            "market_cap_band": market_cap_band(peer),
        }
        for peer in peers
    ]
    summary = _peer_summary(peer_group, metric_comparison)
    needs_peer_review = summary["peer_count"] < 1 or summary["metrics_with_peer_data"] < 3
    return {
        "peer_selection_mode": peer_selection_mode,
        "manual_peer_symbols": manual_symbols,
        "manual_peer_missing_symbols": manual_missing_symbols,
        "peer_group": peer_group,
        "metric_comparison": metric_comparison,
        "peer_summary": summary,
        "needs_peer_review": needs_peer_review,
    }

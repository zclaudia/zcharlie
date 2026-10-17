"""Versioned, closed sets of annual peer metrics."""

from __future__ import annotations

from typing import Any

BAIJIU_PACK = "baijiu_annual_v1"
BANK_PACK = "bank_annual_v1"

PACK_METRIC_KEYS: dict[str, tuple[str, ...]] = {
    BAIJIU_PACK: ("roe", "contract_revenue", "cfo_parent_profit"),
    BANK_PACK: ("roe", "net_interest_margin", "npl_ratio", "provision_coverage"),
}


def metric_keys_for_pack(pack: str) -> tuple[str, ...]:
    """Return the only valid ordered keys for a supported pack."""
    try:
        return PACK_METRIC_KEYS[pack]
    except KeyError:
        raise ValueError(f"unknown peer metric pack {pack!r}") from None


def cohort_metric_pack(payload: Any) -> str:
    """Read a cohort pack, treating historic payloads without one as baijiu."""
    if not isinstance(payload, dict):
        return BAIJIU_PACK
    pack = payload.get("metric_pack", BAIJIU_PACK)
    if not isinstance(pack, str):
        raise ValueError("cohort metric_pack must be a string")
    metric_keys_for_pack(pack)
    return pack


def cohort_read_scope(payload: Any) -> tuple[str, str]:
    """Use a trusted roster's explicit source binding; preserve old rosters.

    Source versions are separate generation domains. Switching a bank roster
    to online collection therefore requires an immutable roster revision with
    a read_scope, rather than choosing the largest generation across adapters.
    """
    pack = cohort_metric_pack(payload)
    scope = payload.get("read_scope") if isinstance(payload, dict) else None
    if scope is None:
        return "eastmoney", "offline-run02-v1"
    if not isinstance(scope, dict) or set(scope) != {"provider", "adapter_version"}:
        raise ValueError("cohort read_scope must contain provider and adapter_version")
    from investment_agent.providers.eastmoney_bank_annual import BANK_ADAPTER_VERSION

    if pack != BANK_PACK or scope != {
        "provider": "eastmoney", "adapter_version": BANK_ADAPTER_VERSION,
    }:
        raise ValueError("cohort read_scope is unsupported for its metric pack")
    return scope["provider"], scope["adapter_version"]

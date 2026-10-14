"""What iFinD can verify, and the free-source values it is compared with (P1-A13).

Verifiable fields (factor-layer names):

* financial fields of the iFinD registry (:data:`IFIND_FIELD_MAPPINGS`) that
  one report period yields - ``roe``, ``gross_margin``, ``net_margin``,
  ``debt_ratio``, ``net_profit_parent``, ``revenue_total``,
  ``operating_cash_flow``, ``capital_expenditure``, ``free_cash_flow`` -
  fetched with the A3 financial endpoints (``/financial/...``) for the
  request's report period;
* ``total_shares`` (``/stock/info``, current) and ``close`` (``/stock/history``,
  raw, on the latest baostock trade date) - market cross-checks.

Other requested fields (e.g. ``eps_ttm``: no iFinD endpoint in the registry)
are reported ``unavailable``.

Free-source values are resolved exactly as the factor view resolves them
(:func:`investment_agent.factors.field_resolution.resolve_period_fields` over the
baostock and AkShare rows of the period - iFinD excluded), so "verified"
means "the value the view would use agrees with iFinD".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.factors.field_resolution import (
    PROVIDER_FIELD_RENAMES,
    FieldResolution,
    provider_fields_from_rows,
    resolve_period_fields,
)
from investment_agent.factors.field_sources import FieldSourceRegistry, relative_difference
from investment_agent.research.ifind_mapping import IFIND_FIELD_MAPPINGS
from investment_agent.snapshots.financial import FinancialEndpoint, financial_endpoints
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot

FREE_FINANCIAL_PROVIDERS: tuple[str, ...] = ("baostock", "akshare")
MARKET_FIELDS: tuple[str, ...] = ("total_shares", "close")
_CASH_FLOW = "/financial/statements:cash_flow"
_SINGLE_PERIOD_DERIVED = frozenset({"free_cash_flow"})


def _financial_field_endpoints() -> dict[str, str]:
    renames = PROVIDER_FIELD_RENAMES["ifind"]
    endpoints: dict[str, str] = {}
    for mapping in IFIND_FIELD_MAPPINGS:
        if mapping.payload_type != "financial":
            continue
        if mapping.status != "direct" and mapping.canonical_field not in _SINGLE_PERIOD_DERIVED:
            continue
        parts = [p.strip() for p in mapping.endpoint.split("+") if p.strip().startswith("/financial/")]
        if len(parts) != 1:
            continue
        endpoints[renames.get(mapping.canonical_field, mapping.canonical_field)] = parts[0]
    # cash paid for long-term assets rides in the cash-flow answer (source_values)
    endpoints.setdefault("capital_expenditure", _CASH_FLOW)
    return endpoints


FINANCIAL_FIELD_ENDPOINTS: dict[str, str] = _financial_field_endpoints()
VERIFIABLE_FIELDS: frozenset[str] = frozenset(FINANCIAL_FIELD_ENDPOINTS) | frozenset(MARKET_FIELDS)


def default_fields(registry: FieldSourceRegistry) -> list[str]:
    """``fields=None``: every key financial field of the registry plus ``close``."""

    keys = {source.name for source in registry.by_table("financial") if source.key}
    return sorted(keys | {"close"})


def endpoints_for(fields: Iterable[str]) -> list[FinancialEndpoint]:
    """The A3 financial endpoints (registry order) the fields need."""

    wanted = {FINANCIAL_FIELD_ENDPOINTS[name] for name in fields if name in FINANCIAL_FIELD_ENDPOINTS}
    return [endpoint for endpoint in financial_endpoints() if endpoint.key in wanted]


Verdict = Literal["verified", "conflict", "unavailable"]


@dataclass(frozen=True)
class FieldCheck:
    name: str
    verdict: Verdict
    ifind: float | None = None
    free: float | None = None
    free_provider: str | None = None
    relative_difference: float | None = None
    tolerance: float | None = None
    reason: str = ""

    @property
    def filled(self) -> bool:
        """iFinD supplied a value no free source has."""

        return self.verdict == "verified" and self.free is None

    def describe(self) -> dict[str, Any]:
        result: dict[str, Any] = {"verdict": self.verdict}
        for key in ("ifind", "free", "free_provider", "relative_difference", "tolerance"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if self.filled:
            result["filled"] = True
        if self.reason:
            result["reason"] = self.reason
        return result


def check_field(
    name: str,
    ifind: float | None,
    free: float | None,
    *,
    tolerance: float,
    free_provider: str | None = None,
) -> FieldCheck:
    """``verified`` within tolerance (or iFinD fills a gap), else ``conflict``."""

    if ifind is None:
        return FieldCheck(name, "unavailable", free=free, free_provider=free_provider,
                          reason="no iFinD value")
    if free is None:
        return FieldCheck(name, "verified", ifind=ifind, tolerance=tolerance,
                          reason="no free-source value; iFinD fills the gap")
    difference = relative_difference(ifind, free)
    verdict: Verdict = "conflict" if difference > tolerance else "verified"
    return FieldCheck(
        name,
        verdict,
        ifind=ifind,
        free=free,
        free_provider=free_provider,
        relative_difference=round(difference, 6),
        tolerance=tolerance,
    )


@dataclass(frozen=True)
class FreePeriod:
    fields: Mapping[str, float | None]
    sources: Mapping[str, FieldResolution]
    refs: tuple[str, ...]

    def value(self, name: str) -> tuple[float | None, str | None]:
        resolution = self.sources.get(name)
        return self.fields.get(name), resolution.provider if resolution else None


async def _latest_rows(
    session: AsyncSession, listing_id: int, report_period: date, providers: Iterable[str]
) -> list[FinancialSnapshot]:
    """Latest version per (provider, fields.part) of one period, every capture known now."""

    stmt = (
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.listing_id == listing_id,
            FinancialSnapshot.report_period == report_period,
            FinancialSnapshot.provider.in_(list(providers)),
        )
        .order_by(FinancialSnapshot.version)
    )
    latest: dict[tuple[str, str], FinancialSnapshot] = {}
    for row in (await session.execute(stmt)).scalars():
        part = row.fields.get("part") if isinstance(row.fields, dict) else None
        latest[(row.provider, str(part or ""))] = row
    return [latest[key] for key in sorted(latest)]


async def free_period(
    session: AsyncSession,
    listing_id: int,
    report_period: date,
    registry: FieldSourceRegistry,
) -> FreePeriod:
    rows = await _latest_rows(session, listing_id, report_period, FREE_FINANCIAL_PROVIDERS)
    grouped: dict[str, list[FinancialSnapshot]] = {}
    for row in rows:
        grouped.setdefault(row.provider, []).append(row)
    fields, sources = resolve_period_fields(
        [provider_fields_from_rows(provider, items) for provider, items in grouped.items()],
        anchor="baostock",
        registry=registry,
    )
    return FreePeriod(fields, sources, tuple(f"financial_snapshot:{row.id}" for row in rows))


async def latest_ifind_row(
    session: AsyncSession, listing_id: int, report_period: date
) -> FinancialSnapshot | None:
    stmt = (
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.listing_id == listing_id,
            FinancialSnapshot.report_period == report_period,
            FinancialSnapshot.provider == "ifind",
        )
        .order_by(FinancialSnapshot.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def latest_annual_period(
    session: AsyncSession, listing_id: int, today: date
) -> date:
    """Latest annual period a free source has published by ``today``.

    Without any: the latest fiscal year whose statutory deadline (04-30) has
    passed.
    """

    stmt = select(func.max(FinancialSnapshot.report_period)).where(
        FinancialSnapshot.listing_id == listing_id,
        FinancialSnapshot.provider.in_(list(FREE_FINANCIAL_PROVIDERS)),
        FinancialSnapshot.period_kind == "annual",
        FinancialSnapshot.available_from.is_not(None),
        FinancialSnapshot.available_from <= today,
    )
    found = (await session.execute(stmt)).scalar_one_or_none()
    if found is not None:
        return found
    year = today.year - 1 if (today.month, today.day) > (4, 30) else today.year - 2
    return date(year, 12, 31)


async def latest_free_bar(
    session: AsyncSession, listing_id: int, today: date
) -> MarketSnapshot | None:
    """Latest baostock raw bar on or before ``today`` (latest version)."""

    stmt = (
        select(MarketSnapshot)
        .where(
            MarketSnapshot.listing_id == listing_id,
            MarketSnapshot.provider == "baostock",
            MarketSnapshot.adjust == "none",
            MarketSnapshot.trade_date <= today,
        )
        .order_by(MarketSnapshot.trade_date.desc(), MarketSnapshot.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def latest_free_total_shares(
    session: AsyncSession, listing_id: int, today: date
) -> tuple[float | None, str | None]:
    """baostock ``total_shares`` of the latest period published by ``today``."""

    stmt = (
        select(FinancialSnapshot)
        .where(
            FinancialSnapshot.listing_id == listing_id,
            FinancialSnapshot.provider == "baostock",
            FinancialSnapshot.available_from.is_not(None),
            FinancialSnapshot.available_from <= today,
        )
        .order_by(FinancialSnapshot.report_period.desc(), FinancialSnapshot.version.desc())
    )
    for row in (await session.execute(stmt)).scalars():
        value = row.fields.get("total_shares") if isinstance(row.fields, dict) else None
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value), f"financial_snapshot:{row.id}"
    return None, None

"""AG03 read-only audit of existing PIT inputs; never computes FCFE coverage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from investment_agent.factors.library import STALE_PRICE_DAYS, _market_cap
from investment_agent.factors.valuation import (
    COVERAGE_TOLERANCE_DAYS,
    MIN_VALID_SESSIONS,
    PERCENTILE_YEARS,
    VALUATION_FACTORS,
)
from investment_agent.factors.view_db import load_snapshot_view
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.repositories.pit import end_of_business_day_utc
from investment_agent.storage.repositories.universe import UniverseRepository

AMOUNT_FIELDS = ("operating_cash_flow", "capital_expenditure")
FCFE_GAPS = (
    "borrowing_receipts", "bond_issue_receipts", "debt_repayments", "cash_interest_paid",
    "ordinary_equity_attribution", "scope_currency_unit_verification",
    "non_operating_and_capital_structure_review",
)


def numeric(value: object) -> bool:
    """Zero and negative amounts are available; null, bool and nonfinite are not."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def summarize(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [row[key] for row in rows]
    return {
        "denominator": len(rows),
        "available": sum(value["available"] for value in values),
        "quality": dict(Counter(value.get("quality", "raw_field_only") for value in values)),
        "primary_reasons": dict(Counter(value["primary_reason"] for value in values)),
        "all_reasons": dict(Counter(reason for value in values for reason in value["reasons"])),
    }


def percentile_audit(result: dict[str, Any]) -> dict[str, Any]:
    """Expose diagnostics from the existing factor, without another percentile formula."""
    reasons = list(result["flags"])
    if result["value"] is None and not reasons:
        reasons.append("current_value_missing")
    available = result["value"] is not None
    return {**result, "available": available, "primary_reason": reasons[0] if reasons else "available",
            "reasons": reasons}


def amount_audit(rows: list[FinancialSnapshot], years: tuple[int, ...]) -> dict[str, Any]:
    """Keep latest captures per provider/part/period; do not add overlapping reports."""
    latest: dict[tuple[str, str, date], FinancialSnapshot] = {}
    for row in sorted(rows, key=lambda item: item.version):
        latest[(row.provider, str(row.fields.get("part", "")), row.report_period)] = row
    evidence = []
    covered: dict[str, set[int]] = {field: set() for field in AMOUNT_FIELDS}
    providers: dict[str, set[str]] = {field: set() for field in AMOUNT_FIELDS}
    for row in latest.values():
        values = {field: row.fields.get(field) for field in AMOUNT_FIELDS}
        present = [field for field, value in values.items() if numeric(value)]
        if not present:
            continue
        for field in present:
            providers[field].add(row.provider)
            if row.period_kind == "annual" and row.report_period.year in years:
                covered[field].add(row.report_period.year)
        evidence.append({
            "id": row.id, "version": row.version, "provider": row.provider,
            "report_period": row.report_period.isoformat(), "period_kind": row.period_kind,
            "availability": row.availability, "available_from": str(row.available_from),
            "captured_at": row.captured_at.isoformat(), "is_backfill": row.is_backfill,
            "raw_ref": row.raw_ref, "amounts_cny": values,
            "source_values": row.fields.get("source_values", {}),
            "availability_basis": row.fields.get("availability_basis"),
        })
    reasons = [f"{field}:missing_annual_{year}" for field in AMOUNT_FIELDS for year in years
               if year not in covered[field]]
    if not evidence:
        reasons.insert(0, "no_raw_cashflow_amounts")
    return {
        "available": bool(evidence), "primary_reason": reasons[0] if reasons else "available",
        "reasons": reasons, "annual_years": list(years),
        "annual_coverage": {field: sorted(found) for field, found in covered.items()},
        "providers": {field: sorted(found) for field, found in providers.items()},
        "both_amounts_three_years": all(set(years) <= found for found in covered.values()),
        "evidence": evidence,
        "fcfe": {"status": "not_assessed_v3a", "verified_available": None,
                 "required_verification": list(FCFE_GAPS)},
    }


async def audit_session(session: AsyncSession, *, universe: str, as_of: date) -> dict[str, Any]:
    members = await UniverseRepository(session).members_as_of(universe, as_of)
    if not members:
        raise ValueError(f"empty or missing universe {universe} on {as_of}")
    membership = [asdict(member) for member in members]
    version = hashlib.sha256(json.dumps(membership, default=str, sort_keys=True).encode()).hexdigest()
    rows: list[dict[str, Any]] = []
    years = (as_of.year - 3, as_of.year - 2, as_of.year - 1)
    cutoff = end_of_business_day_utc(as_of)
    for member in members:
        view = await load_snapshot_view(session, member.ticker, as_of)
        cap = _market_cap(view)
        cap_reasons = list(cap.flags)
        if cap.value is None and not cap_reasons:
            cap_reasons.append("missing_market_cap_input")
        market = {
            **asdict(cap), "quality": cap.quality or "input_available_unverified",
            "available": cap.value is not None, "meaning": "total_market_cap",
            "ordinary_equity_compatibility": "not_verified_v3a",
            "unit": "CNY", "primary_reason": cap_reasons[0] if cap_reasons else "available",
            "reasons": cap_reasons,
        }
        financials = list((await session.execute(select(FinancialSnapshot).where(
            FinancialSnapshot.listing_id == member.listing_id,
            FinancialSnapshot.available_from <= as_of,
            FinancialSnapshot.availability != "unknown",
            FinancialSnapshot.report_period <= as_of,
            (FinancialSnapshot.captured_at < cutoff) | FinancialSnapshot.is_backfill.is_(True),
        ))).scalars())
        rows.append({
            "listing_id": member.listing_id, "ticker": member.ticker,
            "market_cap": market,
            **{factor.name: percentile_audit(asdict(factor.compute(view)))
               for factor in VALUATION_FACTORS},
            "cashflow": amount_audit(financials, years),
            "report_overdue_periods": [str(period) for period in view.overdue_periods],
            "history_sources": {
                "sessions": len(view.raw_bars),
                "providers": dict(Counter(bar.provider for bar in view.raw_bars)),
                "first_ref": view.raw_bars[0].ref if view.raw_bars else None,
                "latest_ref": view.raw_bars[-1].ref if view.raw_bars else None,
                "input_sha256": hashlib.sha256(json.dumps([
                    (str(bar.trade_date), bar.provider, bar.ref, bar.pe_ttm, bar.pb_mrq)
                    for bar in view.raw_bars
                ]).encode()).hexdigest(),
            },
        })
    inventory = {}
    for table, day_column in (("market_snapshot", "trade_date"),
                              ("financial_snapshot", "report_period")):
        inventory[table] = [dict(row) for row in (await session.execute(text(
            f"SELECT provider, count(*) AS captures, min({day_column}) AS first_period, "
            f"max({day_column}) AS last_period, min(captured_at) AS first_capture, "
            f"max(captured_at) AS last_capture FROM {table} GROUP BY provider"
        ))).mappings()]
    return {
        "schema_version": "ag03.coverage.v1", "as_of": as_of.isoformat(), "universe": universe,
        "target_pool_version": version, "membership": membership,
        "stored_inventory_not_coverage": inventory,
        "cashflow_summary": {
            "denominator": len(rows),
            "both_amounts_three_years": sum(row["cashflow"]["both_amounts_three_years"] for row in rows),
            "fields": {field: {
                "any_period_available": sum(bool(row["cashflow"]["providers"][field]) for row in rows),
                "three_annual_years": sum(set(years) <= set(row["cashflow"]["annual_coverage"][field]) for row in rows),
                "by_provider": dict(Counter(provider for row in rows for provider in row["cashflow"]["providers"][field])),
            } for field in AMOUNT_FIELDS},
            "fcfe_semantics_applicability_solver": "not_assessed_v3a",
        },
        "policy": {"window_years": PERCENTILE_YEARS, "minimum_positive_sessions": MIN_VALID_SESSIONS,
                   "coverage_tolerance_days": COVERAGE_TOLERANCE_DAYS,
                   "stale_price_calendar_days": STALE_PRICE_DAYS, "include_backfill": True,
                   "knowledge_cutoff_exclusive_utc": cutoff.isoformat(),
                   "cashflow_annual_years": years,
                   "cashflow_scope": "raw stored amounts; ratios and CFO-capex are not FCFE",
                   "later_layers": "V3b: semantics, applicability, numerical success not assessed"},
        "summary": {key: summarize(rows, key) for key in
                    ("market_cap", "pe_percentile_5y", "pb_percentile_5y", "cashflow")},
        "rows": rows,
    }


async def audit_database(path: Path, *, universe: str, as_of: date) -> dict[str, Any]:
    """Open an existing SQLite database with SQLite-enforced mode=ro; no migrations."""
    path = await asyncio.to_thread(path.resolve, strict=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_uri()}?mode=ro&uri=true")
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("PRAGMA query_only=ON")
            await connection.exec_driver_sql("BEGIN")
            async with AsyncSession(bind=connection) as session:
                result = await audit_session(session, universe=universe, as_of=as_of)
                result["database"] = {"path": str(path), "read_only": True,
                                      "size_bytes": (await asyncio.to_thread(path.stat)).st_size}
                result["data_version"] = hashlib.sha256(
                    json.dumps(result["rows"], default=str, sort_keys=True).encode()
                ).hexdigest()
                return result
    finally:
        await engine.dispose()

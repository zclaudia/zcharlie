"""Strict point-in-time readers for supplemental-rule source material.

These readers deliberately do not use the older factor DB loader: that loader
has a business-day cutoff and admits selected backfills. P2 accepts a precise
knowledge timestamp and only freezes rows actually captured by that instant.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.field_sources import (
    MARKET_ANCHOR_FIELD,
    default_field_sources,
    market_precedence,
)
from investment_agent.factors.industry_groups import usable_sw_code
from investment_agent.factors.registry import FLAG_INDUSTRY_FALLBACK
from investment_agent.factors.snapshot_model import (
    ADJUSTED_HISTORY_DAYS,
    TARGET_POOL,
    Bar,
    Dividend,
    DividendHistory,
    SnapshotView,
)
from investment_agent.factors.view_db import _financial_periods, resolve_market_bars
from investment_agent.investor.foundations import InvestorError
from investment_agent.providers.shenwan_directory import (
    SW2021_SYSTEM,
    default_shenwan_directory,
)
from investment_agent.storage.models.pit_snapshot import (
    DividendEventSnapshot,
    FinancialSnapshot,
    IndustryClassification,
    MarketSnapshot,
    ReportOverdue,
)
from investment_agent.storage.models.reference import Company, Listing

MAX_SOURCE_ROWS = 10_000
"""Per-subject read ceiling. Overflow is diagnostic and cannot imply absence."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # SQLite returns UTC-naive DateTime values. Repository writes normalize
        # to UTC, so restoring that timezone is safe; caller cutoffs must be aware.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("P2 source cutoff must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _date_available_at(value: date) -> datetime:
    """Conservative instant after an unknown-time publication date has ended."""
    return datetime.combine(value + timedelta(days=1), time.min, SHANGHAI_TZ).astimezone(UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


def content_hash(value: Any) -> str:
    raw = json.dumps(_json_value(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_payload(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    return {name: _json_value(getattr(row, name)) for name in columns}


def _manifest_row(kind: str, row: Any, columns: Sequence[str], *, time_precision: str,
                  available_from: datetime | date | None = None) -> dict[str, Any]:
    content = _row_payload(row, columns)
    result: dict[str, Any] = {
        "kind": kind,
        "row_id": getattr(row, "id", None),
        "content": content,
        "content_hash": content_hash(content),
        "captured_at": _json_value(getattr(row, "captured_at", None)),
        "time_precision": time_precision,
    }
    if available_from is not None:
        result["available_from"] = _json_value(available_from)
    return result


def _row_visible_at(row: Any, cutoff: datetime, kind: str, diagnostics: list[str]) -> bool:
    created_at = getattr(row, "created_at", None)
    if created_at is not None and _utc(created_at) > cutoff:
        diagnostics.append(f"{kind}_row_persisted_after_cutoff:{row.id}")
        return False
    updated_at = getattr(row, "updated_at", None)
    if updated_at is not None and _utc(updated_at) > cutoff:
        diagnostics.append(f"{kind}_row_mutated_after_cutoff:{row.id}")
        return False
    return True


async def read_listing(session: AsyncSession, listing_id: int) -> tuple[Listing, Company]:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise InvestorError("invalid_reference", "证券标的不存在")
    company = await session.get(Company, listing.company_id)
    if company is None:
        raise InvestorError("invalid_reference", "证券标的发行人不存在")
    return listing, company


async def read_classifications(
    session: AsyncSession, listing_id: int, cutoff: datetime, *, limit: int = MAX_SOURCE_ROWS
) -> tuple[list[IndustryClassification], list[dict[str, Any]], tuple[str, ...]]:
    """Read actual classification rows captured by cutoff, preserving system.

    Classification intervals are mutable and have no immutable revision. A
    row whose updated_at is later than cutoff cannot establish its earlier
    interval content; it is omitted and the caller receives an explicit gap.
    """
    cutoff = _require_aware(cutoff)
    stmt = (
        select(IndustryClassification)
        .where(IndustryClassification.listing_id == listing_id,
               IndustryClassification.captured_at <= cutoff)
        .order_by(IndustryClassification.system, IndustryClassification.valid_from)
        .limit(limit + 1)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    overflow = len(rows) > limit
    rows = rows[:limit]
    selected: list[IndustryClassification] = []
    diagnostics: list[str] = []
    for row in rows:
        if not _row_visible_at(row, cutoff, "classification", diagnostics):
            continue
        selected.append(row)
    if overflow:
        diagnostics.append("classification_rows_truncated:count_at_least_limit_plus_one")
    manifest = [
        _manifest_row("industry_classification", row,
                      ("id", "listing_id", "system", "code", "name", "valid_from",
                       "valid_to", "source", "captured_at", "created_at", "updated_at"),
                      time_precision="date_interval")
        for row in selected
    ]
    return selected, manifest, tuple(diagnostics)


async def read_snapshot_rows(
    session: AsyncSession,
    listing_id: int,
    cutoff: datetime,
    *,
    limit: int = MAX_SOURCE_ROWS,
) -> tuple[list[MarketSnapshot], list[FinancialSnapshot], list[DividendEventSnapshot],
           list[ReportOverdue], list[dict[str, Any]], tuple[str, ...]]:
    """Read strict-time snapshot rows, excluding all later captures/backfills."""
    cutoff = _require_aware(cutoff)
    diagnostics: list[str] = []

    async def bounded(stmt: Any, kind: str) -> list[Any]:
        rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
        if len(rows) > limit:
            diagnostics.append(f"{kind}_rows_truncated:count_at_least_limit_plus_one")
            return rows[:limit]
        return rows

    market_stmt = (select(MarketSnapshot).where(
        MarketSnapshot.listing_id == listing_id,
        MarketSnapshot.captured_at <= cutoff,
        MarketSnapshot.trade_date <= cutoff.astimezone(SHANGHAI_TZ).date(),
    ).order_by(MarketSnapshot.trade_date, MarketSnapshot.provider, MarketSnapshot.version))
    financial_stmt = (select(FinancialSnapshot).where(
        FinancialSnapshot.listing_id == listing_id,
        FinancialSnapshot.captured_at <= cutoff,
        FinancialSnapshot.availability != "unknown",
        FinancialSnapshot.available_from.is_not(None),
    ).order_by(FinancialSnapshot.report_period, FinancialSnapshot.provider,
               FinancialSnapshot.version))
    dividend_stmt = (select(DividendEventSnapshot).where(
        DividendEventSnapshot.listing_id == listing_id,
        DividendEventSnapshot.captured_at <= cutoff,
        DividendEventSnapshot.ex_date <= cutoff.astimezone(SHANGHAI_TZ).date(),
    ).order_by(DividendEventSnapshot.ex_date, DividendEventSnapshot.provider,
               DividendEventSnapshot.version))
    overdue_stmt = select(ReportOverdue).where(
        ReportOverdue.listing_id == listing_id,
        ReportOverdue.detected_on <= cutoff.astimezone(SHANGHAI_TZ).date(),
    ).order_by(ReportOverdue.report_period)
    markets = await bounded(market_stmt, "market")
    financial = await bounded(financial_stmt, "financial")
    dividends = await bounded(dividend_stmt, "dividend")
    overdue_all = await bounded(overdue_stmt, "overdue")
    markets = [row for row in markets
               if _row_visible_at(row, cutoff, "market", diagnostics)
               and _date_available_at(row.trade_date) <= cutoff and _utc(row.captured_at) <= cutoff]
    financial = [row for row in financial
                 if _row_visible_at(row, cutoff, "financial", diagnostics)]
    dividends = [row for row in dividends
                 if _row_visible_at(row, cutoff, "dividend", diagnostics)]
    overdue: list[ReportOverdue] = []
    for row in overdue_all:
        if not _row_visible_at(row, cutoff, "report_overdue", diagnostics):
            continue
        if _date_available_at(row.detected_on) > cutoff:
            continue
        if row.resolved_on is None or _date_available_at(row.resolved_on) > cutoff:
            overdue.append(row)

    manifest: list[dict[str, Any]] = []
    manifest.extend([
        _manifest_row("market_snapshot", r,
            ("id", "listing_id", "trade_date", "open", "high", "low", "close", "volume",
             "amount", "total_shares", "pe_ttm", "pb_mrq", "ps_ttm", "turnover", "trade_status",
             "is_st", "adjust", "provider", "captured_at", "raw_ref", "is_backfill", "version"),
            time_precision="timestamp") for r in markets
    ])
    for r in financial:
        eligible_at = _date_available_at(r.available_from)
        if eligible_at > cutoff:
            continue
        manifest.append(_manifest_row("financial_snapshot", r,
            ("id", "listing_id", "report_period", "period_kind", "available_from", "availability",
             "fields", "provider", "captured_at", "raw_ref", "is_backfill", "version"),
            time_precision="date", available_from=r.available_from))
    manifest.extend([
        _manifest_row("dividend_event", r,
            ("id", "listing_id", "ex_date", "record_date", "pay_date", "announce_date",
             "implement_date", "available_from", "cash_per_share_pre_tax", "stock_per_share",
             "reserve_per_share", "operate_year", "details", "provider", "captured_at", "raw_ref",
             "is_backfill", "version"), time_precision="date", available_from=r.available_from)
        for r in dividends if _date_available_at(r.available_from) <= cutoff
    ])
    manifest.extend([
        _manifest_row("report_overdue", r,
            ("id", "listing_id", "report_period", "deadline", "detected_on", "resolved_on", "provider",
             "detected_run_id", "resolved_run_id", "created_at", "updated_at"), time_precision="date")
        for r in overdue
    ])
    return markets, financial, dividends, overdue, manifest, tuple(diagnostics)


def build_strict_snapshot_view(
    ticker: str,
    as_of: datetime,
    classifications: Sequence[IndustryClassification],
    markets: Sequence[MarketSnapshot],
    financial: Sequence[FinancialSnapshot],
    dividends: Sequence[DividendEventSnapshot],
    overdue: Sequence[ReportOverdue],
) -> SnapshotView:
    """Build the existing pure factor view from already-cutoff-filtered rows."""
    cutoff = _require_aware(as_of)
    local_day = cutoff.astimezone(SHANGHAI_TZ).date()
    by_system = {
        system: sorted((r for r in classifications if r.system == system),
                       key=lambda r: r.valid_from, reverse=True)
        for system in {r.system for r in classifications}
    }
    def classification(system: str) -> IndustryClassification | None:
        return next((r for r in by_system.get(system, ())
                     if r.valid_from <= local_day and (r.valid_to is None or r.valid_to > local_day)), None)
    csrc = classification("csrc")
    sw = classification(SW2021_SYSTEM)
    sw_code = usable_sw_code(
        sw.code, sw.valid_from, sw.valid_to, default_shenwan_directory(), sw.name
    ) if sw is not None else None

    sources = default_field_sources()
    precedence = market_precedence(sources)
    by_date: dict[date, dict[str, MarketSnapshot]] = {}
    adjusted: list[Bar] = []
    for market_row in markets:
        if (market_row.trade_date > local_day or _date_available_at(market_row.trade_date) > cutoff
                or _utc(market_row.captured_at) > cutoff):
            continue
        if market_row.adjust == "none" and market_row.provider in precedence:
            by_date.setdefault(market_row.trade_date, {})[market_row.provider] = market_row
        if market_row.adjust == "backward" and market_row.provider == TARGET_POOL.provider:
            adjusted.append(_bar_from_snapshot(market_row))
    raw = resolve_market_bars(by_date, precedence, tolerance=sources.tolerance(MARKET_ANCHOR_FIELD))
    financial_latest: dict[tuple[date, str, str], FinancialSnapshot] = {}
    for financial_row in financial:
        if _utc(financial_row.captured_at) > cutoff or financial_row.available_from is None:
            continue
        # Date-only source timestamp is conservatively visible only after the
        # stated Shanghai date ended; never coerce it to a midnight instant.
        if _date_available_at(financial_row.available_from) > cutoff:
            continue
        part = financial_row.fields.get("part") if isinstance(financial_row.fields, dict) else None
        key = (financial_row.report_period, financial_row.provider, str(part or ""))
        prior = financial_latest.get(key)
        if prior is None or financial_row.version > prior.version:
            financial_latest[key] = financial_row
    periods = _financial_periods(list(financial_latest.values()), TARGET_POOL, sources)
    div_events = [r for r in dividends if _utc(r.captured_at) <= cutoff
                  and _date_available_at(r.available_from) <= cutoff and r.ex_date <= local_day]
    dividend_history = DividendHistory(
        events=tuple(Dividend(ex_date=r.ex_date,
            cash_per_share=float(r.cash_per_share_pre_tax) if r.cash_per_share_pre_tax is not None else None,
            share_multiplier=1.0 + float(r.stock_per_share or Decimal(0)) + float(r.reserve_per_share or Decimal(0)),
            available_from=r.available_from, ref=f"dividend_event:{r.id}") for r in div_events),
        covered_years={}, provider=TARGET_POOL.provider,
    )
    overdue_periods = [r.report_period for r in overdue if r.detected_on <= local_day
                       and (r.resolved_on is None or _date_available_at(r.resolved_on) > cutoff)]
    return SnapshotView.build(
        ticker=ticker, as_of=local_day, policy=TARGET_POOL,
        industry_code=csrc.code if csrc else None, industry_name=csrc.name if csrc else None,
        raw_bars=raw, adjusted_bars=[b for b in adjusted if b.trade_date >= local_day - timedelta(days=ADJUSTED_HISTORY_DAYS)],
        periods=periods, overdue_periods=overdue_periods, sw_code=sw_code,
        field_sources=sources, dividends=dividend_history,
    )


def _bar_from_snapshot(row: MarketSnapshot) -> Bar:
    from investment_agent.factors.view_db import _bar_from_row
    return _bar_from_row(row)


def _coverage_domains(diagnostics: Sequence[str]) -> frozenset[str]:
    domains: set[str] = set()
    for diagnostic in diagnostics:
        if diagnostic.startswith(("market_rows_truncated", "market_row_mutated_after_cutoff", "market_row_persisted_after_cutoff")):
            domains.add("market_snapshot")
        elif diagnostic.startswith(("financial_rows_truncated", "financial_row_mutated_after_cutoff", "financial_row_persisted_after_cutoff")):
            domains.add("financial_snapshot")
        elif diagnostic.startswith(("dividend_rows_truncated", "dividend_row_mutated_after_cutoff", "dividend_row_persisted_after_cutoff")):
            domains.add("dividend_event")
        elif diagnostic.startswith(("overdue_rows_truncated", "report_overdue_row_mutated_after_cutoff", "report_overdue_row_persisted_after_cutoff")):
            domains.add("report_overdue")
        elif diagnostic.startswith(("classification_rows_truncated", "classification_row_mutated_after_cutoff", "classification_row_persisted_after_cutoff")):
            domains.add("classification")
    return frozenset(domains)


def factor_snapshot(
    view: SnapshotView,
    factor_names: Sequence[str] | None = None,
    *,
    source_diagnostics: Sequence[str] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry = DEFAULT_REGISTRY
    selected = list(factor_names) if factor_names is not None else registry.names()
    values = registry.compute_all(view, selected)
    uncertain_domains = _coverage_domains(source_diagnostics)
    serial: dict[str, Any] = {}
    metadata: list[dict[str, Any]] = []
    by_name = {item["name"]: item for item in registry.catalogue()}
    for name, value in values.items():
        factor = registry.get(name)
        input_domains = {item.source.split(".", 1)[0] for item in factor.inputs}
        affected = bool(input_domains & uncertain_domains)
        if "classification" in uncertain_domains:
            affected = affected or (
                factor.applicability.groups is not None
                or bool(factor.applicability.excluded)
                or FLAG_INDUSTRY_FALLBACK in value.flags
            )
        flags = list(value.flags)
        if affected:
            # Preserve FactorValue.quality as a P1 contract. P2's adapter
            # interprets this additive flag as unknown for just this factor.
            flags.append("source_coverage_unknown")
        serial[name] = {
            "name": value.name, "version": value.version, "value": value.value,
            "quality": value.quality, "flags": flags,
            "inputs_ref": _json_value(value.inputs_ref),
        }
        meta = by_name[name]
        metadata.append({**meta, "applicability_groups": meta["applicability"]})
    return serial, metadata

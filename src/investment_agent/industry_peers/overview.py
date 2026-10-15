"""Read-only saved industry overview assembled from committed peer batches."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.industry_peers.artifacts import (
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_PROVIDER,
    PeerBatchMapping,
    scan_committed_batch_mappings,
)
from investment_agent.industry_peers.metric_packs import (
    BAIJIU_PACK,
    BANK_PACK,
    cohort_metric_pack,
    metric_keys_for_pack,
)
from investment_agent.industry_peers.service import (
    PeerTargetLookupError,
    get_industry_peer_context,
    list_industry_peer_companies,
)
from investment_agent.investor.common import OwnerId
from investment_agent.storage.models.peer import PeerCohortSnapshot

MIN_OVERVIEW_COMPANIES = 5
METRIC_LABELS = {
    "roe": ("年度加权 ROE", "fraction"),
    "contract_revenue": ("合同负债 / 营业收入", "fraction"),
    "cfo_parent_profit": ("经营现金流 / 归母净利润", "ratio"),
    "net_interest_margin": ("净息差", "fraction"),
    "npl_ratio": ("不良贷款率", "fraction"),
    "provision_coverage": ("拨备覆盖率", "fraction"),
}
METRIC_FIELDS = {
    "roe": {"roe_weighted_annual", "equity_parent"},
    "contract_revenue": {"contract_liabilities", "operating_revenue"},
    "cfo_parent_profit": {"operating_cash_flow", "net_profit_parent"},
    "net_interest_margin": {"net_interest_margin"},
    "npl_ratio": {"npl_ratio"},
    "provision_coverage": {"provision_coverage"},
}


def _pack(cohort: PeerCohortSnapshot) -> str:
    return cohort_metric_pack(cohort.payload)


def _committed_for_cohort(
    mappings: tuple[PeerBatchMapping, ...], cohort: PeerCohortSnapshot
) -> list[PeerBatchMapping]:
    return [
        mapping
        for mapping in mappings
        if str(mapping.cohort_ref.id) == cohort.id
        and mapping.cohort_ref.revision == cohort.revision
        and mapping.cohort_content_hash == cohort.content_hash
        and mapping.provider == DEFAULT_PROVIDER
        and mapping.adapter_version == DEFAULT_ADAPTER_VERSION
    ]


async def list_saved_industries(
    session: AsyncSession, *, data_dir: Path, owner_id: OwnerId = "local_owner"
) -> list[dict[str, Any]]:
    """Latest saved roster per key plus periods with committed batches."""
    rows = list((await session.execute(select(PeerCohortSnapshot))).scalars())
    latest: dict[str, PeerCohortSnapshot] = {}
    for row in rows:
        current = latest.get(row.cohort_key)
        if current is None or (row.revision, row.id) > (current.revision, current.id):
            latest[row.cohort_key] = row
    scan = await scan_committed_batch_mappings(session, data_dir, owner=owner_id)
    result = []
    for key, cohort in sorted(latest.items()):
        pack = _pack(cohort)
        if pack not in {BAIJIU_PACK, BANK_PACK}:
            continue
        mappings = _committed_for_cohort(scan.valid, cohort)
        result.append({
            "cohort_key": key,
            "metric_pack": pack,
            "revision": cohort.revision,
            "observed_on": cohort.observed_on,
            "acquired_at": cohort.acquired_at,
            "verification": cohort.verification,
            "source": cohort.payload.get("source", {}),
            "candidate_count": len(cohort.payload.get("entries", [])),
            "periods": sorted({mapping.report_period for mapping in mappings}, reverse=True),
        })
    return result


def _latest_batch(mappings: list[PeerBatchMapping], period: date) -> PeerBatchMapping | None:
    matching = [mapping for mapping in mappings if mapping.report_period == period]
    if not matching:
        return None
    # Generations are only comparable within one provider/adapter scope.
    scope = max((m.provider, m.adapter_version) for m in matching)
    scoped = [m for m in matching if (m.provider, m.adapter_version) == scope]
    return max(scoped, key=lambda m: (m.generation, m.submitted_at, m.batch_run_id))


async def industry_overview(
    session: AsyncSession,
    *,
    cohort_key: str,
    data_dir: Path,
    report_period: date | None = None,
    batch_run_id: str | None = None,
    owner_id: OwnerId = "local_owner",
) -> dict[str, Any]:
    """Read one immutable roster and its committed batch without recalculation."""
    rows = list((await session.execute(
        select(PeerCohortSnapshot).where(PeerCohortSnapshot.cohort_key == cohort_key)
    )).scalars())
    if not rows:
        raise PeerTargetLookupError(f"industry cohort {cohort_key!r} does not exist")
    cohort = max(rows, key=lambda row: (row.revision, row.id))
    scan = await scan_committed_batch_mappings(session, data_dir, owner=owner_id)
    selected: PeerBatchMapping | None = None
    if batch_run_id is not None:
        selected = next((m for m in scan.valid if m.batch_run_id == batch_run_id), None)
        if selected is None or selected.provider != DEFAULT_PROVIDER or selected.adapter_version != DEFAULT_ADAPTER_VERSION:
            raise PeerTargetLookupError("batch is not a committed batch for this saved industry roster")
        old_cohort = await session.get(PeerCohortSnapshot, str(selected.cohort_ref.id))
        if old_cohort is None or old_cohort.cohort_key != cohort_key:
            raise PeerTargetLookupError("batch is not a committed batch for this saved industry roster")
        cohort = old_cohort
        if report_period is not None and selected.report_period != report_period:
            raise ValueError("batch_run_id and report_period do not match")
    committed = _committed_for_cohort(scan.valid, cohort)
    pack = _pack(cohort)
    metric_keys = metric_keys_for_pack(pack)
    if batch_run_id is not None:
        # Keep the explicit immutable batch after the cohort lookup above.
        pass
    elif report_period is not None:
        selected = _latest_batch(committed, report_period)
    elif committed:
        selected = _latest_batch(committed, max(m.report_period for m in committed))

    entries = cohort.payload.get("entries")
    entries = entries if isinstance(entries, list) else []
    if selected is None:
        return {
            "cohort_key": cohort_key,
            "metric_pack": pack,
            "cohort_ref": {"kind": "peer_cohort_snapshot", "id": cohort.id, "revision": cohort.revision},
            "cohort_content_hash": cohort.content_hash,
            "observed_on": cohort.observed_on,
            "acquired_at": cohort.acquired_at,
            "verification": cohort.verification,
            "source": cohort.payload.get("source", {}),
            "candidate_count": len(entries),
            "available_periods": sorted({m.report_period for m in committed}, reverse=True),
            "report_period": report_period,
            "batch_run_id": None,
            "batch_status": None,
            "metrics": [],
            "companies": [],
            "warnings": list(scan.warnings),
            "gaps": ["该名单期次没有已提交的年度结果。"],
        }

    if not selected.targets:
        raise PeerTargetLookupError("committed batch has no target mapping")
    target = selected.targets[0]
    details = await list_industry_peer_companies(
        session, ticker=target.ticker, batch_run_id=selected.batch_run_id,
        owner_id=owner_id, data_dir=data_dir,
    )
    context = await get_industry_peer_context(
        session, ticker=target.ticker, batch_run_id=selected.batch_run_id,
        owner_id=owner_id, data_dir=data_dir,
    )
    live_context = None
    if batch_run_id is None:
        candidate_context = await get_industry_peer_context(
            session,
            ticker=target.ticker,
            owner_id=owner_id,
            data_dir=data_dir,
        )
        if (
            candidate_context.current_report_period == selected.report_period
            and candidate_context.report_period == selected.report_period
            and candidate_context.batch_run_id == selected.batch_run_id
            and candidate_context.cohort_ref == selected.cohort_ref
        ):
            live_context = candidate_context
    company_rows = {company.company_id: company for company in details.companies}
    evidence_by_company: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for link in details.field_evidence:
        for key, fields in METRIC_FIELDS.items():
            if link.field in fields:
                evidence_by_company.setdefault((link.company_id, key), []).append(link.ref.model_dump(mode="json"))
    contexts = {metric.metric_key: metric for metric in context.metrics}
    company_out = []
    metric_out = []
    for key in metric_keys:
        values: list[Decimal] = []
        valid_rows: list[dict[str, Any]] = []
        for entry in entries:
            company_id = int(entry["company_id"]) if isinstance(entry, dict) else 0
            company = company_rows.get(company_id)
            metric = next((m for m in company.metrics if m.metric_key == key), None) if company else None
            value: Decimal | None = None
            if metric is not None and metric.included and metric.value is not None:
                try:
                    value = Decimal(metric.value)
                except InvalidOperation:
                    value = None
                if value is not None and value.is_finite():
                    values.append(value)
                    valid_rows.append({
                        "value": value,
                        "is_target": bool(metric.is_target),
                    })
            company_out.append({
                "company_id": company_id,
                "ticker": str(entry.get("ticker", "")) if isinstance(entry, dict) else "",
                "name": str(entry.get("name", "")) if isinstance(entry, dict) else "",
                "metric_key": key,
                "value": str(value) if value is not None and value.is_finite() else None,
                "unit": metric.unit if metric else METRIC_LABELS[key][1],
                "is_target": bool(metric.is_target) if metric else False,
                "included": bool(metric and metric.included and value is not None),
                "exclusion_reason": (
                    metric.exclusion_reason if metric is not None
                    else contexts[key].gap_reason if key in contexts and contexts[key].gap_reason
                    else "batch未冻结该公司指标"
                ),
                "exclusion_detail": metric.exclusion_detail if metric else None,
                "evidence_refs": evidence_by_company.get((company_id, key), []),
            })
        labels = METRIC_LABELS[key]
        context_metric = contexts.get(key)
        target_values = [item["value"] for item in valid_rows if item["is_target"]]
        peer_values = [item["value"] for item in valid_rows if not item["is_target"]]
        metric_out.append({
            "metric_key": key,
            "label": labels[0],
            "unit": labels[1],
            "candidate_count": len(entries),
            "valid_count": len(values),
            "verified_coverage": (context_metric.verification_counts if context_metric else {}),
            "median": str(median(values)) if len(values) >= MIN_OVERVIEW_COMPANIES else None,
            "median_minimum": MIN_OVERVIEW_COMPANIES,
            "target_value": str(target_values[0]) if target_values else None,
            "target_excluded_median": str(median(peer_values)) if len(peer_values) >= MIN_OVERVIEW_COMPANIES else None,
            "gap_reason": context_metric.gap_reason if context_metric else "批次未保存该指标。",
        })
    warnings = [*scan.warnings, *details.warnings, *context.warnings]
    if live_context is not None and live_context.stale_reasons:
        warnings.append("已知来源有更新或更正，当前批次需要复核")
    return {
        "cohort_key": cohort_key,
        "metric_pack": pack,
        "cohort_ref": {"kind": "peer_cohort_snapshot", "id": cohort.id, "revision": cohort.revision},
        "cohort_content_hash": cohort.content_hash,
        "observed_on": cohort.observed_on,
        "acquired_at": cohort.acquired_at,
        "verification": cohort.verification,
        "source": cohort.payload.get("source", {}),
        "candidate_count": len(entries),
        "available_periods": sorted({m.report_period for m in committed}, reverse=True),
        "report_period": selected.report_period,
        "batch_run_id": selected.batch_run_id,
        "batch_status": selected.status,
        "generation": selected.generation,
        "submitted_at": selected.submitted_at,
        "comparison_target": {
            "ticker": target.ticker,
            "name": target.name,
        },
        "context_status": live_context.status.value if live_context is not None else None,
        "stale_reasons": list(live_context.stale_reasons) if live_context is not None else [],
        "metrics": metric_out,
        "companies": company_out,
        "warnings": warnings,
        "gaps": [*details.gaps, *context.gaps],
    }

"""Explicitly prepare computed material metrics for numeric publication.

This application service shares the read model's current-version selection and
pure metric rules, then turns only the exact facts named by each successful
result into evidence and a saved calculation. It performs no upstream IO and
is intentionally called by an explicit research action, never by financial GET.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, find_identity
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef, OwnerId, Quantity, utc_timestamp
from investment_agent.investor.enums import CalculationStatus
from investment_agent.investor.foundations import InvestorError
from investment_agent.materials.context import (
    ANNUAL_FACTS,
    BANK_COMPANY_KEYS,
    COMPANY_SUBJECT,
    _current_versions,
)
from investment_agent.materials.contracts import FactPeriod, FactRecord
from investment_agent.materials.evidence import dataset_facts, material_evidence
from investment_agent.materials.investor_bridge import (
    MATERIAL_CALCULATION_METHOD,
    MATERIAL_CALCULATION_VERSION,
    save_material_calculation,
)
from investment_agent.materials.metrics import compute_metrics, profile_from_identity
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.repositories.research_material import ResearchDatasetRepository


@dataclass(frozen=True)
class PreparedMetric:
    """Publication preparation outcome for one computed key and actual period."""

    key: str
    period_end: date | None
    status: Literal["prepared", "blocked"]
    calculation_status: str
    calculation_ref: ObjectRef | None
    fact_refs: tuple[ObjectRef, ...]
    gaps: tuple[str, ...]


@dataclass(frozen=True)
class MaterialPublicationReport:
    """Per-result outcomes; a gap never hides another metric's usable result."""

    listing_id: int
    as_of: datetime
    status: Literal["prepared", "partial", "blocked"]
    metrics: tuple[PreparedMetric, ...]
    gaps: tuple[str, ...]


async def prepare_material_publication_payload(
    session: AsyncSession,
    *,
    listing_id: int,
    as_of: datetime,
    dataset_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """JSON application boundary for offline commands, with public input errors."""
    try:
        report = await prepare_material_publication(
            session, listing_id=listing_id, as_of=as_of, dataset_ids=dataset_ids
        )
    except InvestorError as exc:
        raise ValueError(exc.detail.message) from exc
    return {
        "listing_id": report.listing_id,
        "as_of": report.as_of.isoformat(),
        "status": report.status,
        "metrics": [
            {
                "key": metric.key,
                "period_end": metric.period_end.isoformat() if metric.period_end else None,
                "status": metric.status,
                "calculation_status": metric.calculation_status,
                "calculation_ref": (
                    metric.calculation_ref.model_dump(mode="json")
                    if metric.calculation_ref
                    else None
                ),
                "fact_refs": [ref.model_dump(mode="json") for ref in metric.fact_refs],
                "gaps": list(metric.gaps),
            }
            for metric in report.metrics
        ],
        "gaps": list(report.gaps),
    }


async def prepare_material_publication(
    session: AsyncSession,
    *,
    listing_id: int,
    as_of: datetime,
    owner_id: OwnerId = "local_owner",
    dataset_ids: Sequence[int] | None = None,
) -> MaterialPublicationReport:
    """Save eligible results from the HK/US material metric set.

    ``dataset_ids`` is an optional explicit source selection. Without it, the
    read model's current versions are used. A supplied selection is intersected
    with those same current versions, so an old revision is never mistaken for
    current context. Distinct facts with the same input name and period are
    rejected as ambiguous even when their numeric values happen to agree.

    All writes are enclosed in one savepoint. If an unexpected persistence
    error occurs, the caller's surrounding transaction retains none of this
    invocation's partial evidence or calculation rows.
    """

    normalized_as_of = utc_timestamp(as_of)
    async with session.begin_nested():
        return await _prepare(
            session,
            listing_id=listing_id,
            as_of=normalized_as_of,
            owner_id=owner_id,
            dataset_ids=dataset_ids,
        )


async def _prepare(
    session: AsyncSession,
    *,
    listing_id: int,
    as_of: datetime,
    owner_id: OwnerId,
    dataset_ids: Sequence[int] | None,
) -> MaterialPublicationReport:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise InvestorError("invalid_reference", f"Listing {listing_id} does not exist")
    company = await session.get(Company, listing.company_id)
    identity = find_identity(
        market_code=listing.market_code,
        ticker=listing.ticker,
        path=DEFAULT_PILOT_LIST_PATH,
    )
    if identity is None or company is None:
        return MaterialPublicationReport(
            listing_id=listing_id,
            as_of=as_of,
            status="blocked",
            metrics=(),
            gaps=("listing_not_covered",),
        )

    history = await ResearchDatasetRepository(session).history(
        kind=ANNUAL_FACTS,
        subject_kind=COMPANY_SUBJECT,
        subject_id=company.id,
    )
    current = _current_versions(history)
    if dataset_ids is not None:
        requested = tuple(dict.fromkeys(dataset_ids))
        current_by_id = {row.id: row for row in current}
        unknown = tuple(item for item in requested if item not in current_by_id)
        if unknown:
            raise InvestorError(
                "invalid_reference",
                f"Selected material dataset(s) are not current for this listing: {unknown}",
            )
        material = tuple(current_by_id[item] for item in requested)
    else:
        material = current

    facts_by_dataset: dict[int, tuple[FactRecord, ...]] = {
        row.id: dataset_facts(row) for row in material
    }
    fact_sources = tuple((row, fact) for row in material for fact in facts_by_dataset[row.id])
    all_facts = tuple(fact for _, fact in fact_sources)
    profile = profile_from_identity(identity, is_bank=identity.company_key in BANK_COMPANY_KEYS)
    metric_set = compute_metrics(facts=all_facts, profile=profile, fiscal_years=_periods(all_facts))

    outcomes: list[PreparedMetric] = []
    overall_gaps: list[str] = []
    for result in metric_set.metrics:
        reasons = list(result.notes)
        fact_refs: tuple[ObjectRef, ...] = ()
        calculation_ref: ObjectRef | None = None
        if result.inputs:
            _inputs, input_gap = _resolve_inputs(result.inputs, fact_sources)
            if input_gap is not None and input_gap not in reasons:
                reasons.append(input_gap)
        if result.status == CalculationStatus.OK and result.value is not None:
            exact, resolution_gap = _resolve_inputs(result.inputs, fact_sources)
            if resolution_gap is not None:
                reasons.append(resolution_gap)
            else:
                materialized, reason = await _materialize_exact_facts(
                    session,
                    listing_id=listing_id,
                    owner_id=owner_id,
                    as_of=as_of,
                    exact=exact,
                )
                if reason is not None:
                    reasons.append(reason)
                else:
                    fact_refs = tuple(materialized)
                    calculation_ref = await save_material_calculation(
                        session,
                        listing_id=listing_id,
                        fact_refs=fact_refs,
                        results={result.key: result.value},
                        as_of=as_of,
                        method=MATERIAL_CALCULATION_METHOD,
                        method_version=MATERIAL_CALCULATION_VERSION,
                        operation_id=_calculation_operation_id(
                            owner_id=owner_id,
                            listing_id=listing_id,
                            key=result.key,
                            period_end=result.period_end,
                            fact_refs=fact_refs,
                            value=result.value,
                            as_of=as_of,
                        ),
                        owner_id=owner_id,
                    )
        elif not reasons:
            reasons.append(f"metric_status:{result.status.value}")
        if result.status != CalculationStatus.OK:
            reasons.insert(0, f"metric_status:{result.status.value}")
        if calculation_ref is None and not reasons:
            reasons.append("no_publishable_calculation")
        outcome = PreparedMetric(
            key=result.key,
            period_end=result.period_end,
            status="prepared" if calculation_ref is not None else "blocked",
            calculation_status=result.status.value,
            calculation_ref=calculation_ref,
            fact_refs=fact_refs,
            gaps=tuple(dict.fromkeys(reasons)),
        )
        outcomes.append(outcome)
        overall_gaps.extend(f"{result.key}@{result.period_end}:{reason}" for reason in outcome.gaps)

    return MaterialPublicationReport(
        listing_id=listing_id,
        as_of=as_of,
        status=(
            "prepared"
            if outcomes and all(item.calculation_ref is not None for item in outcomes)
            else "partial"
            if any(item.calculation_ref is not None for item in outcomes)
            else "blocked"
        ),
        metrics=tuple(outcomes),
        gaps=tuple(dict.fromkeys(overall_gaps)),
    )


def _periods(facts: Sequence[FactRecord]) -> tuple[FactPeriod, ...]:
    """Distinct actual annual periods, in the same order as the context model."""

    annual = [fact.period for fact in facts if fact.period.basis.value == "annual"]
    chosen = annual if annual else [fact.period for fact in facts]
    periods: dict[date, FactPeriod] = {}
    for period in sorted(chosen, key=lambda item: (item.end, item.start or item.end)):
        existing = periods.get(period.end)
        rank = (period.basis.value == "annual", period.label is not None, period.days is not None)
        existing_rank = (
            (
                existing.basis.value == "annual",
                existing.label is not None,
                existing.days is not None,
            )
            if existing is not None
            else None
        )
        if existing is None or (existing_rank is not None and rank > existing_rank):
            periods[period.end] = period
    return tuple(periods[end] for end in sorted(periods))


def _resolve_inputs(
    inputs: Sequence[str],
    fact_sources: Sequence[tuple[ResearchDataset, FactRecord]],
) -> tuple[tuple[tuple[ResearchDataset, FactRecord], ...], str | None]:
    resolved: list[tuple[ResearchDataset, FactRecord]] = []
    for item in inputs:
        metric, separator, period_text = item.partition("@")
        if not separator:
            return (), f"invalid_metric_input:{item}"
        try:
            period_end = date.fromisoformat(period_text)
        except ValueError:
            return (), f"invalid_metric_input_period:{item}"
        matches = tuple(
            source
            for source in fact_sources
            if source[1].metric == metric and source[1].period.end == period_end
        )
        if len(matches) > 1:
            return (), f"ambiguous_source:{item}:{len(matches)}_facts"
        if not matches:
            return (), f"missing_exact_input:{item}"
        resolved.append(matches[0])
    return tuple(resolved), None


async def _materialize_exact_facts(
    session: AsyncSession,
    *,
    listing_id: int,
    owner_id: OwnerId,
    as_of: datetime,
    exact: Sequence[tuple[ResearchDataset, FactRecord]],
) -> tuple[tuple[ObjectRef, ...], str | None]:
    evidence = EvidenceService(session)
    pending: list[tuple[ResearchDataset, EvidenceRecord, ObjectRef | None, FactRecord]] = []
    for dataset, fact in exact:
        records, skipped = material_evidence(
            dataset,
            subject_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
            metrics=(fact.metric,),
            created_at=_aware(dataset.captured_at),
        )
        matching = tuple(
            record
            for record in records
            if record.period is not None
            and record.period.end == fact.period.end
            and record.source_ref.id == dataset.id
            and record.source_ref.revision == dataset.revision
        )
        if len(matching) != 1:
            reason = skipped[0] if skipped else "exact_fact_not_materializable"
            return (), f"{fact.metric}@{fact.period.end}:{reason}"
        record = matching[0]
        if max(record.published_at, record.available_from, record.acquired_at) > as_of:
            return (), f"fact_not_visible_at_as_of:{fact.metric}@{fact.period.end}"
        prior = await _saved_fact_ref(
            session,
            owner_id=owner_id,
            listing_id=listing_id,
            dataset_id=dataset.id,
            revision=dataset.revision,
            content_hash=record.content_hash,
        )
        if prior is not None:
            visible = await evidence.get_evidence(
                owner_id,
                prior,
                as_of,
                subject_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
            )
            if visible.status != "ok" or visible.data is None:
                return (), f"fact_not_publishable:{fact.metric}@{fact.period.end}:{visible.status}"
        pending.append((dataset, record, prior, fact))

    refs: list[ObjectRef] = []
    for dataset, record, prior, fact in pending:
        if prior is None:
            operation_id = uuid5(
                NAMESPACE_URL,
                f"material-evidence:{owner_id}:{listing_id}:{dataset.id}:{dataset.revision}:{record.content_hash}",
            )
            prior = await evidence.save_evidence(owner_id, record, operation_id)
        visible = await evidence.get_evidence(
            owner_id,
            prior,
            as_of,
            subject_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
        )
        if visible.status != "ok" or visible.data is None:
            return (), f"fact_not_publishable:{fact.metric}@{fact.period.end}:{visible.status}"
        refs.append(prior)
    return tuple(refs), None


async def _saved_fact_ref(
    session: AsyncSession,
    *,
    owner_id: OwnerId,
    listing_id: int,
    dataset_id: int,
    revision: int,
    content_hash: str,
) -> ObjectRef | None:
    rows = (
        await session.scalars(select(SavedEvidence).where(SavedEvidence.owner_id == owner_id))
    ).all()
    for row in rows:
        payload = row.payload
        source = payload.get("source_ref", {})
        subject = payload.get("subject_ref", {})
        if (
            payload.get("source_kind") == "research_dataset"
            and subject.get("kind") == "listing"
            and subject.get("id") == listing_id
            and source.get("id") == dataset_id
            and source.get("revision") == revision
            and payload.get("content_hash") == content_hash
        ):
            return ObjectRef(kind="evidence", id=UUID(str(row.id)), revision=row.revision)
    return None


def _calculation_operation_id(
    *,
    owner_id: OwnerId,
    listing_id: int,
    key: str,
    period_end: date | None,
    fact_refs: Sequence[ObjectRef],
    value: Quantity,
    as_of: datetime,
) -> UUID:
    facts = ",".join(f"{ref.kind}:{ref.id}:{ref.revision}" for ref in fact_refs)
    return uuid5(
        NAMESPACE_URL,
        f"material-metric:{MATERIAL_CALCULATION_VERSION}:{owner_id}:{listing_id}:{key}:{period_end}:{facts}:"
        f"{value.value}:{value.unit}:{value.currency}:{as_of.isoformat()}",
    )


def _aware(value: datetime) -> datetime:
    """SQLite may return naive stored datetimes; persisted material is UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

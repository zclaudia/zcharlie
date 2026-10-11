"""Material facts become referencable evidence only when their time is provable.

Plan 07: HK/US material facts are converted into :class:`EvidenceRecord` values
whose identity, period, currency and publication boundary keep the source's own
precision. A fact whose publication time is unknown is *not* converted — it
stays in the material layer and the second return value of
:func:`material_evidence` explains why, so a page can still show the fact and
its limitation. Acquisition time is the dataset row's real ``captured_at``;
nothing is back-filled.

The conversion never upgrades verification: the material's ``candidate`` /
``verified`` state is copied verbatim, and ``conflict`` is passed through as
stored (``unresolved`` stays unresolved).

Time boundaries (date-precision sources state a day, not a moment):

* ``timestamp``: ``published_at`` is the stated moment and ``available_from`` is
  ``accepted_at`` when the source states one, else ``published_at``.
* ``date``: ``published_at`` is midnight UTC of that day and ``available_from``
  is the later of the next day's midnight UTC and ``accepted_at`` — the fact is
  only claimed to be knowable from the day after it was stated.
* ``unknown``: no evidence record; the reason is ``unknown_publication_time``.

This module is a pure read of one stored ``research_dataset`` row: it reads the
payload exclusively through ``materials.contracts.payload_facts`` /
``fact_from_payload`` (never by parsing key names here) and writes no storage.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid5

from investment_agent.evidence.contracts import (
    DatePrecision,
    DocumentProvenance,
    EvidenceRecord,
    Period,
)
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.materials.contracts import (
    DatePrecision as MaterialDatePrecision,
)
from investment_agent.materials.contracts import (
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactValue,
    PeriodBasis,
    ReportingScope,
    Verification,
    fact_from_payload,
    payload_facts,
)
from investment_agent.storage.models.research_material import ResearchDataset

UNKNOWN_PUBLICATION_TIME = "unknown_publication_time"
"""Skip reason: the source states no provable publication time."""

UNKNOWN_PERIOD_START = "period_start_unknown"
"""Skip reason: a non-instant period has no start date to state."""

VALUE_NOT_EXPRESSIBLE = "value_not_expressible"
"""Skip reason: a missing value carries no reason, or the raw value is unparsable."""

METRIC_NOT_SELECTED = "metric_not_selected"
"""Skip reason: the caller asked for other metrics only."""

_RESEARCH_ACTOR = "material_adapter"

_ConflictState = Literal["none", "unresolved", "resolved"]


def dataset_facts(dataset: ResearchDataset) -> tuple[FactRecord, ...]:
    """Every fact of one stored dataset, in payload order.

    Reads only through the frozen 02 pair, so source-specific top-level payload
    keys (pagination, accession, unmapped counts) are never interpreted here.
    Structural payload drift raises ``ValueError`` from
    ``materials.contracts``; that is a stored-data problem, not a fact to skip.
    """

    return tuple(fact_from_payload(entry) for entry in payload_facts(dataset.payload))


def material_evidence(
    dataset: ResearchDataset,
    *,
    subject_ref: ObjectRef,
    company_ref: ObjectRef | None = None,
    metrics: Sequence[str] | None = None,
    created_at: datetime | None = None,
) -> tuple[tuple[EvidenceRecord, ...], tuple[str, ...]]:
    """Convert one dataset's facts into evidence; return ``(records, skipped)``.

    ``subject_ref`` is the listing the research archive is built on
    (``kind="listing"``), as for every other evidence producer. ``company_ref``
    is the controlled company the caller resolved for that listing: when the
    dataset is company level, its id must equal ``dataset.subject_id`` so
    company facts cannot be attached to another company's listing; a
    listing-level dataset is checked against ``subject_ref`` instead.

    ``metrics`` optionally restricts the conversion to the named metrics; every
    fact that is not converted contributes exactly one entry to the second
    tuple, in payload order, using one of the module's reason constants.

    ``created_at`` is the record creation time (defaults to now). Saving goes
    through ``EvidenceService.save_evidence``, which re-materializes with this
    same function and assigns the stored identity.
    """

    if subject_ref.kind != "listing" or subject_ref.field_path is not None:
        raise ValueError("material evidence requires a listing subject reference")
    if dataset.subject_kind == "listing" and dataset.subject_id != subject_ref.id:
        raise ValueError("dataset subject listing does not match the evidence subject")
    if (
        company_ref is not None
        and dataset.subject_kind == "company"
        and company_ref.id != dataset.subject_id
    ):
        raise ValueError("dataset subject company does not match the given company reference")

    records: list[EvidenceRecord] = []
    skipped: list[str] = []
    selected = None if metrics is None else frozenset(metrics)
    for fact in dataset_facts(dataset):
        if selected is not None and fact.metric not in selected:
            skipped.append(METRIC_NOT_SELECTED)
            continue
        window = _publication_window(fact.provenance)
        if window is None:
            skipped.append(UNKNOWN_PUBLICATION_TIME)
            continue
        published_at, available_from, precision = window
        period = _period(fact.period)
        if period is None:
            skipped.append(UNKNOWN_PERIOD_START)
            continue
        value = _quantity(fact.value)
        if value is None:
            skipped.append(VALUE_NOT_EXPRESSIBLE)
            continue
        provenance = _provenance(dataset, fact)
        locator = _locator(dataset, fact)
        content_hash = _content_hash(dataset, fact)
        records.append(
            EvidenceRecord(
                id=uuid5(NAMESPACE_URL, content_hash),
                revision=1,
                created_at=_aware(created_at) if created_at is not None else datetime.now(UTC),
                actor=_RESEARCH_ACTOR,
                origin_ref=None,
                source_ref=ObjectRef(
                    kind="research_dataset",
                    id=dataset.id,
                    revision=dataset.revision,
                ),
                source_kind="research_dataset",
                locator=locator,
                content_hash=content_hash,
                subject_ref=subject_ref,
                period=period,
                published_at=published_at,
                acquired_at=_aware(dataset.captured_at),
                available_from=available_from,
                verification="verified"
                if fact.verification == Verification.VERIFIED
                else "candidate",
                # 02's payload validation admits only CONFLICT_STATES values,
                # and the state is copied verbatim, never re-judged here.
                conflict=cast(_ConflictState, fact.conflict),
                date_precision=DatePrecision(precision.value),
                value=value,
                text=_text(fact, value, provenance),
                document_provenance=provenance,
            )
        )
    return tuple(records), tuple(skipped)


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; stored values were written as UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _publication_window(
    provenance: FactProvenance,
) -> tuple[datetime, datetime, MaterialDatePrecision] | None:
    """``(published_at, available_from, precision)`` or ``None`` when unknown.

    ``source_available_at`` is deliberately unused: when the source states no
    provable time, the boundary is not filled in from a weaker field. A
    ``timestamp`` claim without the stated moment is not downgraded to a date
    either — the fact simply stays unconverted (``unknown_publication_time``).
    """

    precision = provenance.source_published_precision
    if precision == MaterialDatePrecision.UNKNOWN:
        return None
    accepted_at = _aware(provenance.accepted_at) if provenance.accepted_at is not None else None
    if precision == MaterialDatePrecision.TIMESTAMP:
        if provenance.source_published_at is None:
            return None
        published_at = _aware(provenance.source_published_at)
        available_from = accepted_at if accepted_at is not None else published_at
        return published_at, available_from, precision
    day = provenance.source_published_date
    if day is None and provenance.source_published_at is not None:
        day = _aware(provenance.source_published_at).date()
    if day is None:
        return None
    published_at = datetime.combine(day, time.min, UTC)
    # A stated day says nothing about the hour: the fact only counts as known
    # from the next day's start, never earlier than when it was accepted.
    next_day = published_at + timedelta(days=1)
    available_from = max(next_day, accepted_at) if accepted_at is not None else next_day
    return published_at, available_from, precision


def _period(period: FactPeriod) -> Period | None:
    """Map the material period; ``None`` when a stated start cannot be kept.

    ``instant`` facts are a point in time, so start and end are both the
    period end; every other basis keeps its stated start and never invents one.
    """

    if period.basis == PeriodBasis.INSTANT:
        start = period.end
    elif period.start is not None:
        start = period.start
    else:
        return None
    return Period(start=start, end=period.end, basis=period.basis.value)


def _quantity(value: FactValue) -> Quantity | None:
    """The stored value with its unit and currency; missing values stay visible.

    A missing value is not zero: ``raw_value is None`` becomes ``Quantity(None,
    unknown_reason=...)``. ``None`` is returned only when the payload cannot be
    expressed honestly (no reason for a missing value, unparsable decimal).
    """

    if value.raw_value is None:
        if value.missing_reason is None:
            return None
        return Quantity(
            value=None,
            unit=value.unit,
            currency=value.currency,
            unknown_reason=value.missing_reason,
        )
    try:
        amount = Decimal(value.raw_value)
    except InvalidOperation:
        return None
    return Quantity(value=amount, unit=value.unit, currency=value.currency)


def _provenance(dataset: ResearchDataset, fact: FactRecord) -> DocumentProvenance | None:
    """Original-document provenance when the source locates a stated amount.

    ``DocumentProvenance.raw_value`` and ``raw_currency`` are non-empty text, so
    provenance is built only when the fact carries both a locator and a raw
    amount with its currency; the locator string keeps the page either way.
    """

    raw_currency = fact.value.currency
    if fact.original_locator is None or fact.value.raw_value is None or raw_currency is None:
        return None
    return DocumentProvenance(
        parser_version=dataset.adapter_version,
        original_hash=fact.provenance.original_hash or dataset.content_hash,
        fragment_hash=_digest(_fact_identity(dataset, fact)),
        metric=fact.metric,
        raw_unit=fact.value.raw_unit,
        raw_currency=raw_currency,
        raw_value=fact.value.raw_value,
        verification_reason=f"material_fact_verification={fact.verification.value}",
        reporting_scope="consolidated"
        if fact.semantics.reporting_scope == ReportingScope.CONSOLIDATED
        else "unknown",
    )


def _text(fact: FactRecord, value: Quantity, provenance: DocumentProvenance | None) -> str | None:
    """One factual sentence for a missing value with no original provenance."""

    if value.value is not None or provenance is not None:
        return None
    return f"source states no value for {fact.metric}: {value.unknown_reason}"


def _locator(dataset: ResearchDataset, fact: FactRecord) -> str:
    anchor = fact.period.label or fact.period.end.isoformat()
    locator = f"{dataset.source}:{dataset.kind}:{anchor}"
    if fact.original_locator is not None:
        return f"{locator}:{fact.original_locator}"
    return locator


def _fact_identity(dataset: ResearchDataset, fact: FactRecord) -> dict[str, object]:
    return {
        "dataset": dataset.content_hash,
        "metric": fact.metric,
        "source": {
            "label": fact.semantics.source_label,
            "field": fact.semantics.source_field,
        },
        "period": {
            "start": None if fact.period.start is None else fact.period.start.isoformat(),
            "end": fact.period.end.isoformat(),
            "basis": fact.period.basis.value,
            "label": fact.period.label,
        },
        "locator": _locator(dataset, fact),
    }


def _content_hash(dataset: ResearchDataset, fact: FactRecord) -> str:
    """Material version hash plus the fact's own identity.

    One dataset version holds many facts and one evidence record is one fact,
    so the dataset hash alone would collide across them; the metric, period and
    locator make each fact's hash distinct inside the same version.
    """

    return _digest(_fact_identity(dataset, fact))


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

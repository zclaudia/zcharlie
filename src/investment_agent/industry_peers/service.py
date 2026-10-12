"""Read-only peer-context query service (WP6).

HTTP (WP9) and the agent tool (WP11) share this module. Its discipline is
fixed by the technical design (§模块与数据流, §任务触发与失败处理):

* **Never an upstream request.** The service reads saved snapshots, saved
  calculations and submitted batch mappings only. The target year is derived
  from local actual-disclosure records (the same local logic the
  ``/report-year`` route is built on) plus the target's own saved annual
  snapshots - the route itself, which can reach a provider, is never called.
* **Never a write at query time.** Every function here only reads; submission
  lives in :mod:`investment_agent.industry_peers.batch`.
* **Batch mappings own record identity.** The current read selects the valid
  submitted mapping with the maximum ``generation`` inside one scope
  ``(cohort_ref, report_period, provider, adapter_version)`` - never by file
  or finish time - and reads exactly the mapped calculation references. A
  newer record without a submitted mapping is invisible to the read, so a
  result can never be spliced from three "latest" records across batches.
  ``batch_run_id`` reads one batch's mapping verbatim and rejects a batch
  that does not belong to the target (:class:`PeerTargetLookupError`, which
  the API layer maps to 404).

Status table (design §任务触发与失败处理) and where each row is produced:

| row | produced here by |
|---|---|
| outside the roster / not yet covered | ``not_supported`` result; an unknown or non-canonical ticker raises instead (:class:`PeerTargetLookupError` / ``ValueError``, API: 404 / 422) |
| a collection is queued or running | ``missing`` result with the ``job_hint`` structural slot; the job lookup itself belongs to WP8/WP11 |
| partial sources | overall ``partial`` with per-metric valid counts, reasons and check coverage |
| metric not applicable to the target | per-metric ``target_not_applicable``, peer median kept |
| known conflicts | per-metric ``conflict`` (the conflicted frozen input was excluded), overall ``conflict`` |
| known corrections / re-checks | overall ``stale`` on the current read (WP4 :func:`peer_field_states`); the old batch stays readable by ``batch_run_id`` |
| nothing usable | ``missing`` with explicit gap reasons |

Cutoff (research binding time): a mapping submitted after the cutoff, or a
mapped calculation record created after it, does not enter the result - the
gap is shown as such. Inputs acquired after the cutoff cannot hide behind a
younger record: a record created before the cutoff cannot cite later-acquired
evidence, so the record-level check plus the mapping's ``submitted_at`` bound
the whole batch. History reads (explicit batch or cutoff) never run the stale
scan: they report the past as it was; only the current display is marked for
re-check.

Old reference: when the current year has no submitted result but the previous
year does, the previous year's batch is returned with ``old_reference=True``
and an explicit warning - it never masquerades as the current year.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord
from investment_agent.industry_peers.artifacts import (
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_PROVIDER,
    MappingArtifactError,
    PeerBatchMapping,
    TargetMapping,
    mapping_artifact_path,
    mappings_in_scope,
    read_batch_mapping,
    scan_batch_mappings,
    select_current_mapping,
)
from investment_agent.industry_peers.calculations import (
    DETAILS_SCHEMA,
    METHOD,
    MIN_VALID_PEERS,
    find_peer_calculations,
    metric_spec,
)
from investment_agent.industry_peers.contracts import (
    PeerContextStatus,
    PeerMetricStatus,
    PeerQuantity,
)
from investment_agent.industry_peers.evidence import peer_field_states
from investment_agent.investor.common import Contract, ObjectRef, OwnerId
from investment_agent.storage.models.earnings_events import DisclosureScheduleRecord
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.reference import Company
from investment_agent.storage.repositories.listing import ListingRepository

SERVICE_SCHEMA_VERSION = 1
"""Version of this service's response contract."""

# DEFAULT_PROVIDER / DEFAULT_ADAPTER_VERSION come from the artifacts module:
# one definition of the offline scope for both submission and read side. The
# trusted configuration passes its own scope explicitly; generations are
# never compared across provider or adapter versions.

_INSUFFICIENT_REASON = (
    "only {valid} valid peers of {candidate} candidates; at least {required} are required "
    "before a median is generated"
)
_NO_CITABLE_EVIDENCE = (
    "the record cites no source evidence; its inputs stay snapshot-only (no disclosure-date "
    "basis), so the check coverage is shown but none of it is verified evidence"
)
_UNREADABLE_RECORD = "the mapped calculation record is not readable for this metric"
_RECORD_AFTER_CUTOFF = (
    "the mapped calculation record was created at {created}, after the cutoff {cutoff}; "
    "a future-created result never enters an older read"
)
_STALE_STATUSES = frozenset({"pending_recheck", "conflict", "corrected"})


class PeerTargetLookupError(LookupError):
    """The target does not exist, or the batch does not belong to the target.

    The API layer (WP9) maps this to 404; the agent adapter can render the
    same distinction without HTTP semantics.
    """


# --- response contracts --------------------------------------------------------


class PeerTargetIdentity(Contract):
    """The resolved directory identity of one target ticker."""

    ticker: str
    company_id: int
    listing_id: int
    name: str | None = None


class PeerJobHint(Contract):
    """Structural slot for collection-run hints; WP8 fills the lookup, WP11
    surfaces it. The service never invents job state at read time."""

    has_pending_job: bool | None = None
    run_reference: str | None = None


class PeerExcludedCompany(Contract):
    """One company's auditable exclusion from one metric."""

    company_id: int
    ticker: str
    name: str
    reason: str
    detail: str
    is_target: bool = False


class PeerMetricContext(Contract):
    """One metric's read-side state, straight from the mapped record."""

    metric_key: str
    status: PeerMetricStatus
    state: Literal["saved", "reused", "gap"]
    target_value: PeerQuantity | None = None
    peer_median: PeerQuantity | None = None
    ascending_position: PeerQuantity | None = None
    valid_peer_count: int = 0
    candidate_peer_count: int = 0
    verification_counts: dict[str, int] = Field(default_factory=dict)
    """Check coverage of the cited evidence (citable refs only; the record's
    quality notes carry the frozen-input tally, which can be larger)."""
    excluded_companies: tuple[PeerExcludedCompany, ...] = ()
    calculation_ref: ObjectRef | None = None
    result_refs: dict[str, ObjectRef] = Field(default_factory=dict)
    """``calculation.results.<key>`` references (the agent cites these)."""
    gap_reason: str | None = None
    reason: str | None = None
    quality_notes: tuple[str, ...] = ()
    cites_no_evidence: bool = False
    """The record used inputs but cites none of them (snapshot-only inputs)."""
    cutoff_excluded: bool = False
    """The mapped record exists but was created after the read's cutoff, so it
    is not visible to this read; all metrics in that state leave ``missing``."""


class PeerContextResult(Contract):
    """The whole peer-context answer for one target (HTTP and agent share it)."""

    schema_version: int = SERVICE_SCHEMA_VERSION
    status: PeerContextStatus
    target: PeerTargetIdentity
    cohort_ref: ObjectRef | None = None
    cohort_content_hash: str | None = None
    report_period: date | None = None
    """The period of the returned batch (the old year on an old reference)."""
    current_report_period: date | None = None
    """The locally determined current year, when it could be determined."""
    batch_run_id: str | None = None
    generation: int | None = None
    provider: str | None = None
    adapter_version: str | None = None
    batch_status: Literal["success", "partial"] | None = None
    metrics: tuple[PeerMetricContext, ...] = ()
    warnings: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    stale_reasons: tuple[str, ...] = ()
    old_reference: bool = False
    inputs_acquired_earliest: datetime | None = None
    inputs_acquired_latest: datetime | None = None
    last_checked_at: datetime | None = None
    submitted_at: datetime | None = None
    data_as_of: datetime
    job_hint: PeerJobHint | None = None


class PeerCompanyMetricDetail(Contract):
    """One company's fixed-version contribution to one metric."""

    metric_key: str
    is_target: bool = False
    included: bool
    value: str | None = None
    unit: str | None = None
    exclusion_reason: str | None = None
    exclusion_detail: str | None = None
    evidence_refs: tuple[ObjectRef, ...] = ()


class PeerCompanyDetail(Contract):
    """One company across the batch's metrics (the page detail rows)."""

    company_id: int
    listing_id: int
    ticker: str
    name: str
    metrics: tuple[PeerCompanyMetricDetail, ...] = ()


class PeerFieldEvidenceLink(Contract):
    """A citable field-evidence reference inside the batch's records."""

    company_id: int
    field: str
    role: str
    ref: ObjectRef


class PeerCompaniesResult(Contract):
    """The fixed-version peer detail of one target inside one batch."""

    schema_version: int = SERVICE_SCHEMA_VERSION
    status: PeerContextStatus
    target: PeerTargetIdentity
    batch_run_id: str | None = None
    report_period: date | None = None
    cohort_ref: ObjectRef | None = None
    companies: tuple[PeerCompanyDetail, ...] = ()
    field_evidence: tuple[PeerFieldEvidenceLink, ...] = ()
    warnings: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    data_as_of: datetime


# --- identity, roster and period resolution ------------------------------------


def _artifacts_root(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    # Imported lazily so the module imports without an environment; the
    # Settings call keeps the app's configured DATA_DIR (see the CLI usage).
    from investment_agent.config import Settings

    return Settings().data_dir  # type: ignore[call-arg]


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def resolve_peer_target(session: AsyncSession, *, ticker: str) -> PeerTargetIdentity:
    """Resolve the ticker through the existing directory only (no name matching).

    A non-canonical ticker raises ``ValueError`` (API: 422); a ticker without
    a directory identity raises :class:`PeerTargetLookupError` (API: 404).
    """

    normalized = ticker.strip().upper()
    if parse_a_share_ticker(normalized) is None:
        raise ValueError(f"not a canonical A-share ticker: {ticker!r}")
    listing = await ListingRepository(session).get_a_share_listing(normalized)
    if listing is None:
        raise PeerTargetLookupError(f"target {normalized} does not exist in the stock directory")
    company = await session.get(Company, listing.company_id)
    return PeerTargetIdentity(
        ticker=format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code),
        company_id=listing.company_id,
        listing_id=listing.id,
        name=company.canonical_name if company is not None else None,
    )


async def resolve_cohort(session: AsyncSession, *, company_id: int) -> PeerCohortSnapshot | None:
    """The roster version that covers the company (latest revision per key).

    Several cohort keys may exist over time; the company's latest roster per
    key wins, and across keys the most recently acquired one. ``None`` means
    the feature does not cover the company (``not_supported``).
    """

    rows = list((await session.execute(select(PeerCohortSnapshot))).scalars())
    latest_by_key: dict[str, PeerCohortSnapshot] = {}
    for row in rows:
        entries = row.payload.get("entries") if isinstance(row.payload, dict) else None
        if not isinstance(entries, list):
            continue
        if not any(
            isinstance(entry, dict) and entry.get("company_id") == company_id for entry in entries
        ):
            continue
        current = latest_by_key.get(row.cohort_key)
        if current is None or (row.revision, row.id) > (current.revision, current.id):
            latest_by_key[row.cohort_key] = row
    if not latest_by_key:
        return None
    return max(latest_by_key.values(), key=lambda row: (row.acquired_at, row.cohort_key))


async def _latest_local_annual_period(
    session: AsyncSession, *, listing_id: int, as_of: datetime
) -> date | None:
    """The latest annual period local records prove available for the listing.

    The same local logic the ``/report-year`` route is built on - actual
    financial captures and actually published schedule entries - plus the
    peer feature's own annual snapshots. The route itself is never called:
    that path can reach a provider, and this service never does. Records
    captured after ``as_of`` do not count, so an older cutoff cannot see a
    year that was only imported later.
    """

    day = as_of.date()
    periods: set[date] = set()
    financial = await session.execute(
        select(FinancialSnapshot.report_period).where(
            FinancialSnapshot.listing_id == listing_id,
            FinancialSnapshot.period_kind == "annual",
            FinancialSnapshot.availability == "actual",
            FinancialSnapshot.available_from.is_not(None),
            FinancialSnapshot.available_from <= day,
            FinancialSnapshot.captured_at < as_of,
        )
    )
    periods.update(row[0] for row in financial.all())
    schedule = await session.execute(
        select(DisclosureScheduleRecord.report_period).where(
            DisclosureScheduleRecord.listing_id == listing_id,
            DisclosureScheduleRecord.actual_date.is_not(None),
            DisclosureScheduleRecord.actual_date <= day,
            DisclosureScheduleRecord.captured_at < as_of,
        )
    )
    periods.update(row[0] for row in schedule.all())
    peer_snapshots = await session.execute(
        select(PeerAnnualSnapshot.report_period).where(
            PeerAnnualSnapshot.listing_id == listing_id,
            PeerAnnualSnapshot.captured_at <= as_of,
        )
    )
    periods.update(row[0] for row in peer_snapshots.all())
    annual = [
        period
        for period in periods
        if (period.month, period.day) == (12, 31) and period.year < day.year
    ]
    return max(annual) if annual else None


# --- record assembly -----------------------------------------------------------


@dataclass
class _CompanyDraft:
    """Mutable per-company accumulator for the fixed-version detail rows."""

    listing_id: int = 0
    ticker: str = ""
    name: str = ""
    metrics: dict[str, PeerCompanyMetricDetail] = field(default_factory=dict)


@dataclass
class _TargetAssembly:
    """Everything one target's read needs, per metric and per company."""

    contexts: list[PeerMetricContext] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    stale_inputs: list[tuple[int, str, str]] = field(default_factory=list)
    """``(company_id, ticker, field)`` triples the displayed outputs rest on;
    the stale scan checks them through the WP4 dedicated query."""
    companies: dict[int, _CompanyDraft] = field(default_factory=dict)
    field_links: list[PeerFieldEvidenceLink] = field(default_factory=list)


def _metric_status_of(record: CalculationRecord) -> PeerMetricStatus:
    details = record.details if isinstance(record.details, dict) else {}
    raw = details.get("metric_status")
    try:
        return PeerMetricStatus(raw) if isinstance(raw, str) else PeerMetricStatus.MISSING
    except ValueError:
        return PeerMetricStatus.MISSING


def _count_of(record: CalculationRecord, key: str) -> int:
    quantity = record.results.get(key)
    if quantity is not None and isinstance(quantity.value, Decimal):
        return int(quantity.value)
    return 0


def _excluded_companies(record: CalculationRecord) -> tuple[PeerExcludedCompany, ...]:
    details = record.details if isinstance(record.details, dict) else {}
    entries = details.get("excluded_companies")
    if not isinstance(entries, list):
        return ()
    excluded = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        excluded.append(
            PeerExcludedCompany(
                company_id=int(entry.get("company_id", 0)),
                ticker=str(entry.get("ticker", "")),
                name=str(entry.get("name", "")),
                reason=str(entry.get("reason", "")),
                detail=str(entry.get("detail", "")),
                is_target=bool(entry.get("is_target", False)),
            )
        )
    return tuple(excluded)


async def _verification_counts(session: AsyncSession, refs: tuple[ObjectRef, ...]) -> dict[str, int]:
    """Check coverage over the record's citable evidence (read-only)."""

    counts: dict[str, int] = {}
    if not refs:
        return counts
    wanted = {(str(ref.id), ref.revision) for ref in refs}
    rows = list(
        (
            await session.execute(
                select(SavedEvidence).where(SavedEvidence.id.in_({key[0] for key in wanted}))
            )
        ).scalars()
    )
    by_key = {(row.id, row.revision): row for row in rows}
    for key in sorted(wanted):
        row = by_key.get(key)
        if row is None:
            continue
        try:
            record = EvidenceRecord.model_validate(row.payload)
        except ValueError:
            continue
        counts[record.verification] = counts.get(record.verification, 0) + 1
    return counts


def _metric_reason(
    *,
    status: PeerMetricStatus,
    valid: int,
    candidate: int,
    cites_no_evidence: bool,
    gap_reason: str | None,
    record: CalculationRecord | None,
) -> str | None:
    if status is PeerMetricStatus.INSUFFICIENT_PEERS:
        return _INSUFFICIENT_REASON.format(
            valid=valid, candidate=candidate, required=MIN_VALID_PEERS
        )
    if cites_no_evidence:
        return _NO_CITABLE_EVIDENCE
    if record is not None and status is PeerMetricStatus.MISSING:
        details = record.details if isinstance(record.details, dict) else {}
        target_exclusions = [
            entry
            for entry in details.get("excluded_companies", [])
            if isinstance(entry, dict) and entry.get("is_target")
        ]
        if target_exclusions:
            return (
                "target input unusable "
                f"({target_exclusions[0].get('reason')}); the peer median is still shown "
                "when available"
            )
    return gap_reason


def _used_stale_inputs(record: CalculationRecord) -> list[tuple[int, str, str]]:
    """``(company_id, ticker, field)`` triples the displayed outputs rest on."""

    details = record.details if isinstance(record.details, dict) else {}
    raw_key = details.get("metric_key")
    if not isinstance(raw_key, str):
        return []
    try:
        spec = metric_spec(raw_key)
    except ValueError:
        return []
    fields = sorted(
        {
            name
            for name in (spec.numerator_field, spec.denominator_field, spec.applicability_field)
            if name is not None
        }
    )
    rows: list[tuple[int, str]] = []
    included = details.get("included_companies")
    if isinstance(included, list):
        rows.extend(
            (int(entry.get("company_id", 0)), str(entry.get("ticker", "")))
            for entry in included
            if isinstance(entry, dict)
        )
    target = details.get("target")
    if isinstance(target, dict):
        rows.append((int(target.get("company_id", 0)), str(target.get("ticker", ""))))
    return [
        (company_id, ticker, field_name) for company_id, ticker in rows for field_name in fields
    ]


async def _assemble_target(
    session: AsyncSession,
    *,
    owner_id: OwnerId,
    identity: PeerTargetIdentity,
    mapping: PeerBatchMapping,
    target_entry: TargetMapping | None,
    cutoff: datetime | None,
) -> _TargetAssembly:
    """Read one target's mapped records into metric contexts and detail rows.

    Exactly the mapped references are read (via WP5
    :func:`find_peer_calculations`); a record that is missing, unreadable, or
    created after the cutoff becomes an explicit per-metric gap - never a
    silent substitution by another record.
    """

    assembly = _TargetAssembly()
    if target_entry is None:
        return assembly

    saved = await find_peer_calculations(
        session,
        owner_id=owner_id,
        target_company_id=identity.company_id,
        report_period=mapping.report_period,
    )
    by_id = {item.ref.id: item for item in saved}

    for entry in sorted(target_entry.metric_calculation_refs, key=lambda item: item.metric_key):
        spec = metric_spec(entry.metric_key)
        if entry.state == "gap":
            assembly.contexts.append(
                PeerMetricContext(
                    metric_key=entry.metric_key,
                    status=PeerMetricStatus.MISSING,
                    state=entry.state,
                    gap_reason=entry.gap_reason,
                    reason=entry.gap_reason,
                    candidate_peer_count=entry.candidate_peer_count or 0,
                )
            )
            continue
        item = by_id.get(entry.ref.id) if entry.ref is not None else None
        if item is None:
            assembly.gaps.append(f"{entry.metric_key}: {_UNREADABLE_RECORD}")
            assembly.contexts.append(
                PeerMetricContext(
                    metric_key=entry.metric_key,
                    status=PeerMetricStatus.MISSING,
                    state=entry.state,
                    gap_reason=_UNREADABLE_RECORD,
                    reason=_UNREADABLE_RECORD,
                )
            )
            continue
        record = item.record
        details = record.details if isinstance(record.details, dict) else {}
        if details.get("schema") != DETAILS_SCHEMA or record.method != METHOD:
            assembly.gaps.append(f"{entry.metric_key}: {_UNREADABLE_RECORD}")
            assembly.contexts.append(
                PeerMetricContext(
                    metric_key=entry.metric_key,
                    status=PeerMetricStatus.MISSING,
                    state=entry.state,
                    gap_reason=_UNREADABLE_RECORD,
                    reason=_UNREADABLE_RECORD,
                )
            )
            continue
        if cutoff is not None and _as_utc(record.created_at) > _as_utc(cutoff):
            reason = _RECORD_AFTER_CUTOFF.format(
                created=record.created_at.isoformat(), cutoff=cutoff.astimezone(UTC).isoformat()
            )
            assembly.warnings.append(f"{entry.metric_key}: {reason}")
            assembly.contexts.append(
                PeerMetricContext(
                    metric_key=entry.metric_key,
                    status=PeerMetricStatus.MISSING,
                    state=entry.state,
                    gap_reason=reason,
                    reason=reason,
                    cutoff_excluded=True,
                )
            )
            continue

        status = _metric_status_of(record)
        valid = _count_of(record, "valid_peer_count")
        candidate = _count_of(record, "candidate_peer_count")
        target_quantity = record.results.get("target")
        cites_no_evidence = len(record.fact_refs) == 0 and (
            valid > 0 or target_quantity is not None
        )
        context = PeerMetricContext(
            metric_key=entry.metric_key,
            status=status,
            state=entry.state,
            target_value=target_quantity,
            peer_median=record.results.get("peer_median"),
            ascending_position=record.results.get("ascending_position"),
            valid_peer_count=valid,
            candidate_peer_count=candidate,
            verification_counts=await _verification_counts(session, record.fact_refs),
            excluded_companies=_excluded_companies(record),
            calculation_ref=item.ref,
            result_refs={
                key: ObjectRef(
                    kind="calculation",
                    id=item.ref.id,
                    revision=item.ref.revision,
                    field_path=f"results.{key}",
                )
                for key in record.results
            },
            quality_notes=record.quality,
            cites_no_evidence=cites_no_evidence,
            reason=_metric_reason(
                status=status,
                valid=valid,
                candidate=candidate,
                cites_no_evidence=cites_no_evidence,
                gap_reason=entry.gap_reason,
                record=record,
            ),
        )
        assembly.contexts.append(context)

        # Fixed-version detail rows: exclusions (with reasons) first, then the
        # included companies' values, then the target's own value.
        for excluded in context.excluded_companies:
            draft = assembly.companies.setdefault(excluded.company_id, _CompanyDraft())
            draft.ticker = draft.ticker or excluded.ticker
            draft.name = draft.name or excluded.name
            draft.metrics[entry.metric_key] = PeerCompanyMetricDetail(
                metric_key=entry.metric_key,
                is_target=excluded.is_target,
                included=False,
                exclusion_reason=excluded.reason,
                exclusion_detail=excluded.detail,
            )
        included_rows = details.get("included_companies")
        if isinstance(included_rows, list):
            for included in included_rows:
                if not isinstance(included, dict):
                    continue
                company_id = int(included.get("company_id", 0))
                draft = assembly.companies.setdefault(company_id, _CompanyDraft())
                draft.ticker = draft.ticker or str(included.get("ticker", ""))
                draft.name = draft.name or str(included.get("name", ""))
                draft.metrics[entry.metric_key] = PeerCompanyMetricDetail(
                    metric_key=entry.metric_key,
                    included=True,
                    value=included.get("value"),
                    unit=spec.unit,
                )
        if target_quantity is not None:
            draft = assembly.companies.setdefault(identity.company_id, _CompanyDraft())
            draft.ticker = draft.ticker or identity.ticker
            draft.name = draft.name or (identity.name or "")
            draft.metrics[entry.metric_key] = PeerCompanyMetricDetail(
                metric_key=entry.metric_key,
                is_target=True,
                included=True,
                value=str(target_quantity.value) if target_quantity.value is not None else None,
                unit=target_quantity.unit,
            )
        fact_evidence = details.get("fact_evidence")
        if isinstance(fact_evidence, list):
            for fact in fact_evidence:
                if not isinstance(fact, dict) or not isinstance(fact.get("ref"), dict):
                    continue
                assembly.field_links.append(
                    PeerFieldEvidenceLink(
                        company_id=int(fact.get("company_id", 0)),
                        field=str(fact.get("field", "")),
                        role=str(fact.get("role", "")),
                        ref=ObjectRef.model_validate(fact["ref"]),
                    )
                )
        assembly.stale_inputs.extend(_used_stale_inputs(record))
    return assembly


def _overall_status(
    *,
    has_target_entry: bool,
    contexts: list[PeerMetricContext],
    stale_reasons: tuple[str, ...],
) -> PeerContextStatus:
    """The status-table mapping (see the module docstring for row coverage)."""

    if not has_target_entry or not contexts:
        return PeerContextStatus.MISSING
    if any(context.status is PeerMetricStatus.CONFLICT for context in contexts):
        return PeerContextStatus.CONFLICT
    if all(
        context.state == "gap" or context.cutoff_excluded for context in contexts
    ):
        return PeerContextStatus.MISSING
    if stale_reasons:
        return PeerContextStatus.STALE
    if all(
        context.status in (PeerMetricStatus.COMPARABLE, PeerMetricStatus.TARGET_NOT_APPLICABLE)
        for context in contexts
    ) and not any(context.cites_no_evidence for context in contexts):
        return PeerContextStatus.READY
    return PeerContextStatus.PARTIAL


async def _stale_reasons(
    session: AsyncSession, *, inputs: list[tuple[int, str, str]], report_period: date
) -> tuple[str, ...]:
    """Known corrections / re-checks over the displayed inputs (WP4 query).

    Only the current read runs this: a supplier update or a declared
    correction makes the displayed values await re-check, while the old batch
    itself stays readable by ``batch_run_id``.
    """

    reasons: list[str] = []
    seen: set[tuple[int, str]] = set()
    for company_id, ticker, field_name in sorted(inputs):
        if (company_id, field_name) in seen:
            continue
        seen.add((company_id, field_name))
        report = await peer_field_states(
            session, company_id=company_id, metric=field_name, report_period=report_period
        )
        if report.status in _STALE_STATUSES:
            reasons.append(
                f"{ticker or f'company {company_id}'} {field_name}: {report.status}; "
                "the displayed value awaits re-check"
            )
    return tuple(reasons)


def _missing_result(
    *,
    identity: PeerTargetIdentity,
    as_of: datetime,
    cohort_ref: ObjectRef | None = None,
    cohort_content_hash: str | None = None,
    gaps: list[str],
    warnings: tuple[str, ...] = (),
    current_report_period: date | None = None,
) -> PeerContextResult:
    return PeerContextResult(
        status=PeerContextStatus.MISSING,
        target=identity,
        cohort_ref=cohort_ref,
        cohort_content_hash=cohort_content_hash,
        current_report_period=current_report_period,
        gaps=tuple(gaps),
        warnings=warnings,
        data_as_of=as_of,
        job_hint=PeerJobHint(),
    )


# --- public read API -----------------------------------------------------------


async def get_industry_peer_context(
    session: AsyncSession,
    *,
    ticker: str,
    batch_run_id: str | None = None,
    cutoff: datetime | None = None,
    owner_id: OwnerId = "local_owner",
    data_dir: Path | None = None,
    provider: str = DEFAULT_PROVIDER,
    adapter_version: str = DEFAULT_ADAPTER_VERSION,
) -> PeerContextResult:
    """Read the peer context of one target (read-only; never an upstream call).

    Without ``batch_run_id`` the current result is the valid submitted mapping
    with the maximum generation inside ``(cohort, period, provider, adapter)``;
    with no result for the current year but one for the previous year, the
    previous year is returned as an explicit old reference. With
    ``batch_run_id`` exactly that batch's mapping is read and must belong to
    the target. ``cutoff`` (research binding time) hides mappings and records
    submitted after it.
    """

    if cutoff is not None and cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    as_of = cutoff.astimezone(UTC) if cutoff is not None else datetime.now(UTC)
    root = _artifacts_root(data_dir)

    identity = await resolve_peer_target(session, ticker=ticker)
    cohort = await resolve_cohort(session, company_id=identity.company_id)
    if cohort is None:
        return PeerContextResult(
            status=PeerContextStatus.NOT_SUPPORTED,
            target=identity,
            data_as_of=as_of,
            gaps=(
                f"{identity.ticker} is not a member of any saved peer cohort roster; "
                "the feature does not cover it yet",
            ),
        )
    cohort_ref = ObjectRef(
        kind="peer_cohort_snapshot", id=UUID(cohort.id), revision=cohort.revision
    )

    scan = scan_batch_mappings(root)
    warnings = list(scan.warnings)
    old_reference = False
    current_period: date | None = None
    mapping: PeerBatchMapping | None

    if batch_run_id is not None:
        try:
            mapping = read_batch_mapping(mapping_artifact_path(root, batch_run_id))
        except MappingArtifactError as exc:
            return _missing_result(
                identity=identity,
                as_of=as_of,
                cohort_ref=cohort_ref,
                cohort_content_hash=cohort.content_hash,
                gaps=[f"batch mapping unreadable: {exc}"],
                warnings=tuple(warnings),
            )
        if mapping.target_for(identity.company_id) is None:
            raise PeerTargetLookupError(
                f"batch {batch_run_id} holds no result for target {identity.ticker}"
            )
        if mapping.cohort_ref != cohort_ref:
            warnings.append(
                f"batch {batch_run_id} was built against roster revision "
                f"{mapping.cohort_ref.revision}; the current roster is revision {cohort.revision}"
            )
        if cutoff is not None and mapping.submitted_at > cutoff:
            return _missing_result(
                identity=identity,
                as_of=as_of,
                cohort_ref=cohort_ref,
                cohort_content_hash=cohort.content_hash,
                gaps=[
                    f"batch {batch_run_id} was submitted at {mapping.submitted_at.isoformat()}, "
                    f"after the cutoff {cutoff.astimezone(UTC).isoformat()}"
                ],
                warnings=tuple(warnings),
            )
    else:
        current_period = await _latest_local_annual_period(
            session, listing_id=identity.listing_id, as_of=as_of
        )
        if current_period is None:
            return _missing_result(
                identity=identity,
                as_of=as_of,
                cohort_ref=cohort_ref,
                cohort_content_hash=cohort.content_hash,
                gaps=[
                    "cannot determine the target year from local disclosure records, and no "
                    "submitted peer batch exists"
                ],
                warnings=tuple(warnings),
            )
        scope = mappings_in_scope(
            scan.valid,
            cohort_ref=cohort_ref,
            report_period=current_period,
            provider=provider,
            adapter_version=adapter_version,
        )
        mapping = select_current_mapping(scope, cutoff=cutoff)
        if mapping is None:
            previous = date(current_period.year - 1, 12, 31)
            mapping = select_current_mapping(
                mappings_in_scope(
                    scan.valid,
                    cohort_ref=cohort_ref,
                    report_period=previous,
                    provider=provider,
                    adapter_version=adapter_version,
                ),
                cutoff=cutoff,
            )
            if mapping is None:
                return _missing_result(
                    identity=identity,
                    as_of=as_of,
                    cohort_ref=cohort_ref,
                    cohort_content_hash=cohort.content_hash,
                    gaps=[f"no submitted peer batch for {current_period.isoformat()}"],
                    warnings=tuple(warnings),
                    current_report_period=current_period,
                )
            old_reference = True
            warnings.append(
                f"no submitted result for the current year {current_period.isoformat()}; "
                f"showing {previous.isoformat()} as an old reference - the current year's "
                "data is still missing"
            )

    target_entry = mapping.target_for(identity.company_id)
    assembly = await _assemble_target(
        session,
        owner_id=owner_id,
        identity=identity,
        mapping=mapping,
        target_entry=target_entry,
        cutoff=cutoff,
    )
    gaps = list(assembly.gaps)
    warnings.extend(assembly.warnings)
    if target_entry is None:
        gaps.append(
            f"batch {mapping.batch_run_id} holds no mapping entry for {identity.ticker}; "
            "the target could not be built in that batch"
        )

    stale: tuple[str, ...] = ()
    if batch_run_id is None and cutoff is None and not old_reference:
        stale = await _stale_reasons(
            session, inputs=assembly.stale_inputs, report_period=mapping.report_period
        )
    status = _overall_status(
        has_target_entry=target_entry is not None,
        contexts=assembly.contexts,
        stale_reasons=stale,
    )
    return PeerContextResult(
        status=status,
        target=identity,
        cohort_ref=mapping.cohort_ref,
        cohort_content_hash=mapping.cohort_content_hash,
        report_period=mapping.report_period,
        current_report_period=current_period,
        batch_run_id=mapping.batch_run_id,
        generation=mapping.generation,
        provider=mapping.provider,
        adapter_version=mapping.adapter_version,
        batch_status=mapping.status,
        metrics=tuple(assembly.contexts),
        warnings=tuple(warnings),
        gaps=tuple(gaps),
        stale_reasons=stale,
        old_reference=old_reference,
        inputs_acquired_earliest=mapping.inputs_acquired_earliest,
        inputs_acquired_latest=mapping.inputs_acquired_latest,
        last_checked_at=mapping.last_checked_at,
        submitted_at=mapping.submitted_at,
        data_as_of=as_of,
    )


async def list_industry_peer_companies(
    session: AsyncSession,
    *,
    ticker: str,
    batch_run_id: str,
    cutoff: datetime | None = None,
    owner_id: OwnerId = "local_owner",
    data_dir: Path | None = None,
) -> PeerCompaniesResult:
    """The fixed-version peer detail of one target inside one batch (read-only).

    Values, exclusions and evidence references come from the mapped records;
    nothing is recomputed and no other batch's record is mixed in. A batch
    that holds no result for the target raises
    :class:`PeerTargetLookupError` (API: 404).
    """

    if cutoff is not None and cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    as_of = cutoff.astimezone(UTC) if cutoff is not None else datetime.now(UTC)
    root = _artifacts_root(data_dir)

    identity = await resolve_peer_target(session, ticker=ticker)
    try:
        mapping = read_batch_mapping(mapping_artifact_path(root, batch_run_id))
    except MappingArtifactError as exc:
        return PeerCompaniesResult(
            status=PeerContextStatus.MISSING,
            target=identity,
            batch_run_id=batch_run_id,
            gaps=(f"batch mapping unreadable: {exc}",),
            data_as_of=as_of,
        )
    target_entry = mapping.target_for(identity.company_id)
    if target_entry is None:
        raise PeerTargetLookupError(
            f"batch {batch_run_id} holds no result for target {identity.ticker}"
        )

    assembly = await _assemble_target(
        session,
        owner_id=owner_id,
        identity=identity,
        mapping=mapping,
        target_entry=target_entry,
        cutoff=cutoff,
    )

    # Seed the company rows from the roster the batch was built against, then
    # merge the per-metric details; roster members the batch could not freeze
    # stay listed with empty metric details instead of disappearing.
    roster: dict[int, PeerCompanyDetail] = {}
    cohort_id = mapping.cohort_ref.id
    if isinstance(cohort_id, UUID):
        cohort_row = await session.get(PeerCohortSnapshot, str(cohort_id))
        entries = cohort_row.payload.get("entries") if cohort_row is not None else None
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                company_id = int(item.get("company_id", 0))
                roster[company_id] = PeerCompanyDetail(
                    company_id=company_id,
                    listing_id=int(item.get("listing_id", 0)),
                    ticker=str(item.get("ticker", "")),
                    name=str(item.get("name", "")),
                )
    companies: list[PeerCompanyDetail] = []
    for company_id in sorted(set(roster) | set(assembly.companies)):
        seed = roster.get(company_id)
        draft = assembly.companies.get(company_id)
        companies.append(
            PeerCompanyDetail(
                company_id=company_id,
                listing_id=seed.listing_id if seed is not None else 0,
                ticker=(seed.ticker if seed is not None else "") or (draft.ticker if draft else ""),
                name=(seed.name if seed is not None else "") or (draft.name if draft else ""),
                metrics=(
                    tuple(draft.metrics[key] for key in sorted(draft.metrics)) if draft else ()
                ),
            )
        )

    return PeerCompaniesResult(
        status=_overall_status(
            has_target_entry=True, contexts=assembly.contexts, stale_reasons=()
        ),
        target=identity,
        batch_run_id=mapping.batch_run_id,
        report_period=mapping.report_period,
        cohort_ref=mapping.cohort_ref,
        companies=tuple(companies),
        field_evidence=tuple(assembly.field_links),
        warnings=tuple(assembly.warnings),
        gaps=tuple(assembly.gaps),
        data_as_of=as_of,
    )


__all__ = [
    "DEFAULT_ADAPTER_VERSION",
    "DEFAULT_PROVIDER",
    "SERVICE_SCHEMA_VERSION",
    "PeerCompaniesResult",
    "PeerCompanyDetail",
    "PeerCompanyMetricDetail",
    "PeerContextResult",
    "PeerFieldEvidenceLink",
    "PeerJobHint",
    "PeerMetricContext",
    "PeerTargetIdentity",
    "PeerTargetLookupError",
    "get_industry_peer_context",
    "list_industry_peer_companies",
    "resolve_cohort",
    "resolve_peer_target",
]

"""Deterministic peer-metric calculations and their saved records (WP5).

The pure layer turns one *frozen batch input* - the cohort roster membership
plus every company's annual fields (or their explicit gaps) for one target and
one report period - into the three fixed metrics:

* ``roe``               = ``roe_weighted_annual`` (issuer weighted-average ROE,
  already saved as a fraction at import; never divided again, never recomputed
  from profit and equity), unit ``fraction``.
* ``contract_revenue``  = ``contract_liabilities / operating_revenue``, unit
  ``fraction``. The denominator is operating revenue, never the
  ``revenue_total_check`` total-revenue check-only field.
* ``cfo_parent_profit`` = ``operating_cash_flow / net_profit_parent``, unit
  ``ratio`` (saved as a full-precision decimal string; 4-decimal formatting is
  display semantics owned by a later work package).

Discipline fixed by the technical design (§同行计算记录):

* **Pure and deterministic.** No IO, no clock, standard-library ``median``,
  :class:`~decimal.Decimal` arithmetic on decimal-string inputs - never float.
  Exclusions are per company and per metric with an auditable reason; the
  three metrics are independent (a missing cash flow never blocks ROE).
* **Peer set.** Peers are cohort members other than the target. A metric needs
  at least :data:`MIN_VALID_PEERS` valid peers before a median is saved; the
  ascending position additionally requires the target to apply. A
  non-applicable target (evidence-backed non-positive denominator) keeps the
  peer median; fewer valid peers saves only the verifiable coverage counts and
  the exclusion audit - never a zero in place of a statistic. No usable source
  fact at all yields a :class:`MetricGap` and no saved calculation.
* **Ascending position** is the midrank percentile over the valid peers:
  ``(strictly_below + 0.5 * equal) / valid_peer_count * 100``, saved in
  ``percent`` (0-100). The golden run-02 numbers have no ties, so Maotai's
  ``17/18`` reduces to the pure strictly-below form; the ``0.5 * equal``
  midrank credit defines the tie behaviour (equal values share one position)
  and matches the golden file's ``ascending_midrank_percentile``.
* **Applicability exclusions need field evidence.** A non-positive denominator
  (revenue, parent profit, or - for ROE applicability - parent equity) is a
  data-backed applicability exclusion. A *missing* field is never claimed as
  "not applicable": without the field the applicability is simply unknown, and
  the entry is excluded as missing or unverifiable.
* **Records.** ``SavedCalculation`` with method ``industry_peer_context``,
  version ``1.0`` and ``details.metric_key`` fixed to the three keys.
  ``results`` carries only the keys that actually exist (``target``,
  ``peer_median``, ``ascending_position``, ``valid_peer_count``,
  ``candidate_peer_count``). Record status ``ok`` means exactly that its
  existing result keys are correct - never that every source is verified; the
  ``quality`` notes record the field check coverage as it is.
* **input_hash** covers the cohort hash, target identity, report period, the
  precise input versions (every company's snapshot ``semantic_hash``), the
  exclusion states with their bases and the method rules - canonical JSON,
  sha256. All outputs of one metric share this dependency scope; there is no
  per-result-key trimming in the first phase, so any frozen-input change is a
  new input and appends a new record. Saving with an unchanged input reuses
  the old record UUID (method and inputs re-verified first) instead of
  appending a duplicate.
* **fact_refs** cite the saved evidence of every value that entered the
  outputs plus the applicability inputs (parent profit for non-positive
  denominators, parent equity for the ROE check). Missing and conflicted
  entries live only in the exclusion audit inside ``details`` - they are never
  dressed up as numeric facts.

The saving helpers are the seam WP8 orchestrates: freeze the batch input,
compute, build, then submit the per-target :class:`TargetCalculationRefs`
mapping. Reading-side queries stay minimal (WP6 owns the service).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from statistics import median
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerMetricStatus
from investment_agent.investor.common import ObjectRef, OwnerId, Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.peer import PeerAnnualSnapshot

METHOD = "industry_peer_context"
"""``SavedCalculation.method`` for every peer-context metric record."""

METHOD_VERSION = "1.0"
"""``SavedCalculation.method_version``; a rule change appends new records."""

INPUT_HASH_SCHEMA = "industry_peer_input_hash/1"
"""Schema tag inside the canonical input-hash payload."""

DETAILS_SCHEMA = "industry_peer_details/1"
"""Schema tag of the ``SavedCalculation.details`` payload."""

MIN_VALID_PEERS = 5
"""Valid peers required before a median (and position) may be saved."""

POSITION_RULE = "ascending_midrank_percentile"
"""Name of the relative-position rule, aligned with the run-02 golden file.

``(strictly_below + 0.5 * equal) / valid_peer_count * 100``: the golden case
has no ties and reduces to ``strictly_below / valid_peer_count * 100``
(Maotai ROE 94.44% = 17/18). Equal values share one midrank position, so a
target equal to every peer sits at exactly 50%."""

ROE = "roe"
CONTRACT_REVENUE = "contract_revenue"
CFO_PARENT_PROFIT = "cfo_parent_profit"

METRIC_KEYS: tuple[str, ...] = (ROE, CONTRACT_REVENUE, CFO_PARENT_PROFIT)
"""The three fixed metric keys; nothing else is computed or saved."""

# Exclusion audit reasons (per company x metric; part of the input hash).
NOT_IN_COHORT = "not_in_cohort"
DUPLICATE_COMPANY = "duplicate_company"
PERIOD_MISMATCH = "period_mismatch"
MISSING_FIELD = "missing_field"
FIELD_CONFLICT = "field_conflict"
SOURCE_FIELD_MISMATCH = "source_field_mismatch"
NON_POSITIVE_DENOMINATOR = "non_positive_denominator"
NON_POSITIVE_EQUITY = "non_positive_equity"
APPLICABILITY_INPUT_MISSING = "applicability_input_missing"

_APPLICABILITY_REASONS = frozenset({NON_POSITIVE_DENOMINATOR, NON_POSITIVE_EQUITY})
"""Evidence-backed applicability exclusions (they may declare the target
not-applicable); every other reason is an unknown or unusable input, never a
claim that the metric does not apply."""

_COUNT_UNIT = "count"
_POSITION_UNIT = "percent"


@dataclass(frozen=True)
class MetricSpec:
    """One metric's source fields, unit and canonical formula text."""

    metric_key: str
    numerator_field: str
    """Field carrying the value (ROE) or the numerator (the two ratios)."""
    denominator_field: str | None
    """Divisor field; ``None`` for ROE (the disclosed ratio is used as saved)."""
    applicability_field: str | None
    """Field whose presence and positivity prove applicability; the denominator
    itself for the ratios, parent equity for ROE."""
    unit: str
    formula: str

    @property
    def needs_applicability_ref(self) -> bool:
        """ROE cites parent equity separately from its saved value."""

        return (
            self.applicability_field is not None
            and self.applicability_field != self.numerator_field
            and self.applicability_field != self.denominator_field
        )


_METRIC_SPECS: dict[str, MetricSpec] = {
    spec.metric_key: spec
    for spec in (
        MetricSpec(
            metric_key=ROE,
            numerator_field="roe_weighted_annual",
            denominator_field=None,
            applicability_field="equity_parent",
            unit="fraction",
            formula="roe_weighted_annual (issuer weighted-average ROE, saved as fraction)",
        ),
        MetricSpec(
            metric_key=CONTRACT_REVENUE,
            numerator_field="contract_liabilities",
            denominator_field="operating_revenue",
            applicability_field="operating_revenue",
            unit="fraction",
            formula="contract_liabilities / operating_revenue",
        ),
        MetricSpec(
            metric_key=CFO_PARENT_PROFIT,
            numerator_field="operating_cash_flow",
            denominator_field="net_profit_parent",
            applicability_field="net_profit_parent",
            unit="ratio",
            formula="operating_cash_flow / net_profit_parent",
        ),
    )
}


def metric_spec(metric_key: str) -> MetricSpec:
    """The fixed spec of one metric; an unknown key is a caller error."""

    try:
        return _METRIC_SPECS[metric_key]
    except KeyError:
        raise ValueError(f"unknown peer metric key {metric_key!r}") from None


# --- frozen batch input -------------------------------------------------------


@dataclass(frozen=True)
class BatchField:
    """One metric source field of one company inside the frozen batch.

    ``value`` is a :class:`~decimal.Decimal` parsed from the snapshot's decimal
    string (never float); ``None`` only together with ``missing_reason``.
    ``source_semantic`` states which canonical metric the value actually is, so
    a check-only field filed under a ratio's denominator name is detected
    instead of silently used. ``source_version`` is the snapshot
    ``semantic_hash`` the value came from (input-version identity) and
    ``verification`` the field's saved check status for the quality notes.
    A ``conflict_reason`` marks an unresolved conflict: the entry is excluded
    from the calculation, never silently preferred or hidden.
    """

    value: Decimal | None
    missing_reason: str | None = None
    conflict_reason: str | None = None
    source_semantic: str | None = None
    source_version: str | None = None
    verification: str | None = None

    def __post_init__(self) -> None:
        if (self.value is None) != (self.missing_reason is not None):
            raise ValueError(
                "a missing batch field requires a reason; a known value cannot carry one"
            )

    @classmethod
    def missing(cls, reason: str, *, source_version: str | None = None) -> BatchField:
        return cls(value=None, missing_reason=reason, source_version=source_version)


@dataclass(frozen=True)
class BatchCompany:
    """One company's frozen slice of the batch input."""

    company_id: int
    listing_id: int
    ticker: str
    name: str
    in_cohort: bool
    report_period: date
    fields: Mapping[str, BatchField] = field(default_factory=dict)


@dataclass(frozen=True)
class FrozenBatch:
    """The immutable calculation input for one target, cohort and period.

    Built once by the caller (WP8 freezes it inside the batch task; tests build
    it from imported snapshots), then handed to the pure functions. The target
    must appear exactly once; a duplicated peer company is a data-level
    exclusion recorded in the audit, never a silent first-wins pick.
    """

    target_company_id: int
    report_period: date
    cohort_ref: ObjectRef
    cohort_content_hash: str
    companies: tuple[BatchCompany, ...]

    def __post_init__(self) -> None:
        if self.cohort_ref.kind != "peer_cohort_snapshot":
            raise ValueError("cohort_ref must reference a peer_cohort_snapshot")
        matches = [c for c in self.companies if c.company_id == self.target_company_id]
        if len(matches) != 1:
            raise ValueError(
                "the frozen batch must contain the target company exactly once "
                f"(found {len(matches)} entries for company {self.target_company_id})"
            )

    @property
    def target_company(self) -> BatchCompany:
        return next(c for c in self.companies if c.company_id == self.target_company_id)


def batch_field_from_snapshot(row: PeerAnnualSnapshot, metric: str) -> BatchField:
    """The batch input shape of one stored snapshot field (pure mapping).

    This is the seam between the WP3 storage shape and the frozen batch: the
    canonical field entry fixes value and missing reason, the row fixes the
    input version. ``source_semantic`` is the canonical metric key the entry is
    filed under, so a stored snapshot never triggers the semantics guard.
    """

    entry = PeerAnnualField.model_validate(row.fields[metric])
    return BatchField(
        value=entry.value,
        missing_reason=entry.missing_reason,
        conflict_reason=None,
        source_semantic=metric,
        source_version=row.semantic_hash,
        verification=entry.verification.value,
    )


# --- pure calculation ---------------------------------------------------------


@dataclass(frozen=True)
class PeerExclusion:
    """One company's exclusion from one metric, with its auditable basis."""

    company_id: int
    ticker: str
    name: str
    reason: str
    detail: str


@dataclass(frozen=True)
class PeerInclusion:
    """One valid peer's contribution to one metric."""

    company_id: int
    ticker: str
    name: str
    value: Decimal


@dataclass(frozen=True)
class MetricOutcome:
    """The deterministic result of one metric over one frozen batch."""

    metric_key: str
    metric_status: PeerMetricStatus
    target_company_id: int
    target_value: Decimal | None
    """The target's metric value; ``None`` while the target is excluded."""
    target_exclusion: PeerExclusion | None
    peer_median: Decimal | None
    ascending_position: Decimal | None
    valid_peer_count: int
    candidate_peer_count: int
    inclusions: tuple[PeerInclusion, ...]
    exclusions: tuple[PeerExclusion, ...]
    quality: tuple[str, ...]


@dataclass(frozen=True)
class MetricGap:
    """Missing-semantics outcome: no usable source fact for the metric.

    Returned instead of a :class:`MetricOutcome` when neither the target nor a
    single peer produced a usable value. The caller keeps the batch-level gap
    (WP8's batch manifest); no calculation record is created for it.
    """

    metric_key: str
    reason: str
    candidate_peer_count: int
    exclusions: tuple[PeerExclusion, ...]
    input_hash: str


@dataclass(frozen=True)
class _Evaluation:
    """Internal per-company evaluation result for one metric."""

    value: Decimal | None
    exclusion: PeerExclusion | None
    value_entry: BatchField | None
    """The numerator entry when it passed its checks (a fact-ref candidate)."""
    applicability_entry: BatchField | None
    """The applicability entry when it carries a usable value (a fact-ref
    candidate for applicability exclusions and ROE inclusions)."""

    @property
    def included(self) -> bool:
        return self.exclusion is None and self.value is not None


def _field_exclusion(
    company: BatchCompany,
    metric_key: str,
    field_name: str,
    entry: BatchField | None,
    *,
    role: str,
) -> PeerExclusion | None:
    """Data-level usability checks for one field entry (shared by value inputs
    and applicability inputs); ``None`` when the entry is usable."""

    where = field_name if role == "value" else f"{field_name} ({role})"
    if entry is None:
        return PeerExclusion(
            company_id=company.company_id,
            ticker=company.ticker,
            name=company.name,
            reason=MISSING_FIELD,
            detail=f"{metric_key} input {where} is absent from the frozen batch entry",
        )
    if entry.value is None:
        return PeerExclusion(
            company_id=company.company_id,
            ticker=company.ticker,
            name=company.name,
            reason=MISSING_FIELD,
            detail=(
                f"{metric_key} input {where} has no value ({entry.missing_reason}); "
                "a missing value is never treated as a number"
            ),
        )
    if entry.conflict_reason is not None:
        return PeerExclusion(
            company_id=company.company_id,
            ticker=company.ticker,
            name=company.name,
            reason=FIELD_CONFLICT,
            detail=(
                f"{metric_key} input {where} has an unresolved conflict "
                f"({entry.conflict_reason}); unverified conflicting input is excluded"
            ),
        )
    if entry.source_semantic is not None and entry.source_semantic != field_name:
        return PeerExclusion(
            company_id=company.company_id,
            ticker=company.ticker,
            name=company.name,
            reason=SOURCE_FIELD_MISMATCH,
            detail=(
                f"{metric_key} input {where} carries {entry.source_semantic} semantics; "
                "that field is not this metric's input (the total-revenue check-only "
                "field is never an operating-revenue denominator)"
            ),
        )
    return None


def _applicability_exclusion(
    company: BatchCompany, reason: str, field_name: str, value: Decimal
) -> PeerExclusion:
    return PeerExclusion(
        company_id=company.company_id,
        ticker=company.ticker,
        name=company.name,
        reason=reason,
        detail=(
            f"{field_name} = {value} is not positive; the metric does not apply to this "
            "company (applicability exclusion backed by the saved field value)"
        ),
    )


def _evaluate(spec: MetricSpec, company: BatchCompany) -> _Evaluation:
    """Evaluate one company for one metric.

    A missing denominator is a missing input; a non-positive one is an
    evidence-backed applicability exclusion. For ROE a missing parent-equity
    input leaves applicability unknown - never claimed as not applicable.
    """

    numerator = company.fields.get(spec.numerator_field)
    numerator_exclusion = _field_exclusion(
        company, spec.metric_key, spec.numerator_field, numerator, role="value"
    )
    if numerator_exclusion is not None or numerator is None:
        return _Evaluation(None, numerator_exclusion, None, None)

    if spec.denominator_field is not None:
        denominator = company.fields.get(spec.denominator_field)
        denominator_exclusion = _field_exclusion(
            company, spec.metric_key, spec.denominator_field, denominator, role="value"
        )
        if denominator_exclusion is not None or denominator is None:
            return _Evaluation(None, denominator_exclusion, None, None)
        assert denominator.value is not None and numerator.value is not None
        if denominator.value <= 0:
            return _Evaluation(
                None,
                _applicability_exclusion(
                    company, NON_POSITIVE_DENOMINATOR, spec.denominator_field, denominator.value
                ),
                None,
                denominator,
            )
        return _Evaluation(numerator.value / denominator.value, None, numerator, denominator)

    applicability_name = spec.applicability_field
    assert applicability_name is not None
    applicability = company.fields.get(applicability_name)
    if applicability is None or applicability.value is None:
        return _Evaluation(
            None,
            PeerExclusion(
                company_id=company.company_id,
                ticker=company.ticker,
                name=company.name,
                reason=APPLICABILITY_INPUT_MISSING,
                detail=(
                    f"{spec.metric_key} applicability needs {applicability_name}; without "
                    "that field the metric can be neither claimed applicable nor excluded "
                    "as not applicable"
                ),
            ),
            None,
            None,
        )
    applicability_exclusion = _field_exclusion(
        company, spec.metric_key, applicability_name, applicability, role="applicability"
    )
    if applicability_exclusion is not None:
        return _Evaluation(None, applicability_exclusion, None, None)
    assert applicability.value is not None and numerator.value is not None
    if applicability.value <= 0:
        return _Evaluation(
            None,
            _applicability_exclusion(
                company, NON_POSITIVE_EQUITY, applicability_name, applicability.value
            ),
            None,
            applicability,
        )
    return _Evaluation(numerator.value, None, numerator, applicability)


def ascending_position(valid_values: list[Decimal], target_value: Decimal) -> Decimal:
    """The midrank position of ``target_value`` among the valid peer values.

    ``(strictly_below + 0.5 * equal) / n * 100`` in percent - see
    :data:`POSITION_RULE`. Deterministic and tie-stable: equal values share one
    position, and a target equal to every peer sits at exactly 50%.
    """

    strictly_below = sum(1 for value in valid_values if value < target_value)
    equal = sum(1 for value in valid_values if value == target_value)
    numerator = Decimal(strictly_below) + Decimal(equal) / Decimal(2)
    return numerator / Decimal(len(valid_values)) * Decimal(100)


def _structural_exclusion(company: BatchCompany, batch: FrozenBatch) -> PeerExclusion | None:
    if company.report_period != batch.report_period:
        return PeerExclusion(
            company_id=company.company_id,
            ticker=company.ticker,
            name=company.name,
            reason=PERIOD_MISMATCH,
            detail=(
                f"input period {company.report_period.isoformat()} differs from the batch "
                f"period {batch.report_period.isoformat()}; periods are never mixed"
            ),
        )
    return None


def _tally(entry: BatchField | None, tally: dict[str, int]) -> None:
    if entry is None:
        return
    if entry.verification in tally:
        tally[entry.verification] += 1
    else:
        tally["unstated"] += 1


def _quality_notes(
    spec: MetricSpec, tally: dict[str, int], has_applicability_evidence: bool
) -> tuple[str, ...]:
    notes = [
        f"{spec.metric_key} ({spec.formula}); field verification of used inputs - "
        f"verified: {tally['verified']}, candidate: {tally['candidate']}, "
        f"unstated: {tally['unstated']}"
    ]
    if tally["candidate"] or tally["unstated"]:
        notes.append(
            "status ok means the existing result keys are correct, not that every source "
            "is verified; the check coverage above is the honest record"
        )
    if has_applicability_evidence:
        notes.append(
            "applicability exclusions are backed by the saved applicability-field "
            "evidence cited in fact_refs"
        )
    return tuple(notes)


def compute_peer_metric(batch: FrozenBatch, metric_key: str) -> MetricOutcome | MetricGap:
    """Compute one metric over the frozen batch (pure, no clock, no IO).

    Peers are cohort members other than the target; every company is included
    or excluded per metric with an auditable reason. With
    :data:`MIN_VALID_PEERS` valid peers the median is saved; the position needs
    the target to apply as well. Without any usable source fact the function
    returns a :class:`MetricGap` - the caller keeps the gap and saves nothing.
    """

    spec = metric_spec(metric_key)
    target = batch.target_company

    by_id: dict[int, BatchCompany] = {}
    duplicated_ids: set[int] = set()
    for company in batch.companies:
        if company.company_id in by_id:
            duplicated_ids.add(company.company_id)
            continue
        by_id[company.company_id] = company

    def audit_entry(company_id: int) -> BatchCompany:
        return by_id[company_id]

    # Peer candidates: distinct cohort members other than the target. Duplicates
    # stay candidates (they were considered) but are excluded from valid peers.
    peer_candidates = [
        by_id[company_id]
        for company_id in sorted(by_id)
        if company_id != batch.target_company_id and by_id[company_id].in_cohort
    ]
    candidate_peer_count = len(peer_candidates)

    exclusions: list[PeerExclusion] = []

    def duplicate_exclusion(company_id: int) -> PeerExclusion:
        company = audit_entry(company_id)
        return PeerExclusion(
            company_id=company.company_id,
            ticker=company.ticker,
            name=company.name,
            reason=DUPLICATE_COMPANY,
            detail=(
                "the company appears more than once in the frozen batch; every occurrence "
                "is excluded instead of silently picking one"
            ),
        )

    def cohort_exclusion(company: BatchCompany) -> PeerExclusion:
        return PeerExclusion(
            company_id=company.company_id,
            ticker=company.ticker,
            name=company.name,
            reason=NOT_IN_COHORT,
            detail="outside the saved cohort roster; peers come from the roster only",
        )

    pre_exclusions: dict[int, PeerExclusion] = {}
    for company_id in sorted(by_id):
        if company_id == batch.target_company_id:
            continue
        company = audit_entry(company_id)
        exclusion: PeerExclusion | None
        if company_id in duplicated_ids:
            exclusion = duplicate_exclusion(company_id)
        elif not company.in_cohort:
            exclusion = cohort_exclusion(company)
        else:
            exclusion = _structural_exclusion(company, batch)
        if exclusion is not None:
            pre_exclusions[company_id] = exclusion
    exclusions.extend(pre_exclusions.values())

    # Peer evaluation.
    inclusions: list[PeerInclusion] = []
    evaluations: dict[int, _Evaluation] = {}
    verification_tally: dict[str, int] = {"verified": 0, "candidate": 0, "unstated": 0}
    has_applicability_evidence = False
    for company in peer_candidates:
        if company.company_id in pre_exclusions:
            continue
        evaluation = _evaluate(spec, company)
        evaluations[company.company_id] = evaluation
        if evaluation.exclusion is not None:
            exclusions.append(evaluation.exclusion)
            if evaluation.exclusion.reason in _APPLICABILITY_REASONS:
                has_applicability_evidence = True
                _tally(evaluation.applicability_entry, verification_tally)
            continue
        assert evaluation.value is not None
        inclusions.append(
            PeerInclusion(
                company_id=company.company_id,
                ticker=company.ticker,
                name=company.name,
                value=evaluation.value,
            )
        )
        _tally(evaluation.value_entry, verification_tally)
        if spec.needs_applicability_ref:
            _tally(evaluation.applicability_entry, verification_tally)

    # Target evaluation (independent of the peer loop).
    target_evaluation = _evaluate(spec, target)
    structural = _structural_exclusion(target, batch)
    if structural is not None:
        target_evaluation = _Evaluation(None, structural, None, None)
    target_value = target_evaluation.value
    target_exclusion = target_evaluation.exclusion
    if target_evaluation.included:
        _tally(target_evaluation.value_entry, verification_tally)
        if spec.needs_applicability_ref:
            _tally(target_evaluation.applicability_entry, verification_tally)
    elif target_exclusion is not None and target_exclusion.reason in _APPLICABILITY_REASONS:
        has_applicability_evidence = True
        _tally(target_evaluation.applicability_entry, verification_tally)

    inclusions.sort(key=lambda inclusion: inclusion.company_id)
    exclusions.sort(key=lambda exclusion: (exclusion.company_id, exclusion.reason))
    valid_values = [inclusion.value for inclusion in inclusions]
    valid_peer_count = len(valid_values)

    peer_median: Decimal | None = None
    position: Decimal | None = None
    if valid_peer_count >= MIN_VALID_PEERS:
        peer_median = median(valid_values)
        if target_value is not None:
            position = ascending_position(valid_values, target_value)

    metric_status, gap_reason = _metric_status(
        target_included=target_value is not None,
        target_exclusion=target_exclusion,
        valid_peer_count=valid_peer_count,
    )
    input_hash = metric_input_hash(
        batch,
        metric_key,
        included_peer_ids=[inclusion.company_id for inclusion in inclusions],
        excluded=exclusions,
        target_included=target_value is not None,
        target_value=target_value,
        target_exclusion=target_exclusion,
    )

    if metric_status is PeerMetricStatus.MISSING and target_value is None and valid_peer_count == 0:
        return MetricGap(
            metric_key=metric_key,
            reason=gap_reason or "no usable source fact for this metric in the frozen batch",
            candidate_peer_count=candidate_peer_count,
            exclusions=tuple(exclusions),
            input_hash=input_hash,
        )

    return MetricOutcome(
        metric_key=metric_key,
        metric_status=metric_status,
        target_company_id=batch.target_company_id,
        target_value=target_value,
        target_exclusion=target_exclusion,
        peer_median=peer_median,
        ascending_position=position,
        valid_peer_count=valid_peer_count,
        candidate_peer_count=candidate_peer_count,
        inclusions=tuple(inclusions),
        exclusions=tuple(exclusions),
        quality=_quality_notes(spec, verification_tally, has_applicability_evidence),
    )


def _metric_status(
    *,
    target_included: bool,
    target_exclusion: PeerExclusion | None,
    valid_peer_count: int,
) -> tuple[PeerMetricStatus, str | None]:
    """Per-metric status; ``missing`` additionally carries a gap reason."""

    enough = valid_peer_count >= MIN_VALID_PEERS
    if target_included and enough:
        return PeerMetricStatus.COMPARABLE, None
    if enough and target_exclusion is not None:
        if target_exclusion.reason in _APPLICABILITY_REASONS:
            return PeerMetricStatus.TARGET_NOT_APPLICABLE, None
        if target_exclusion.reason == FIELD_CONFLICT:
            return PeerMetricStatus.CONFLICT, None
        return (
            PeerMetricStatus.MISSING,
            f"target input unusable ({target_exclusion.reason}); the peer median is still "
            "available but the target has no comparable value",
        )
    if not enough:
        if valid_peer_count == 0 and not target_included:
            reason = "no usable source fact for this metric in the frozen batch"
            if target_exclusion is not None:
                reason += f" (target: {target_exclusion.reason})"
            return PeerMetricStatus.MISSING, reason
        return (
            PeerMetricStatus.INSUFFICIENT_PEERS,
            f"only {valid_peer_count} valid peers; at least {MIN_VALID_PEERS} are required "
            "before a median is generated",
        )
    return PeerMetricStatus.COMPARABLE, None


# --- input hash ---------------------------------------------------------------


def _rules_payload(spec: MetricSpec) -> dict[str, Any]:
    return {
        "min_valid_peers": MIN_VALID_PEERS,
        "position_rule": POSITION_RULE,
        "value_definition": spec.formula,
        "unit": spec.unit,
    }


def metric_input_hash(
    batch: FrozenBatch,
    metric_key: str,
    *,
    included_peer_ids: list[int],
    excluded: list[PeerExclusion],
    target_included: bool,
    target_value: Decimal | None,
    target_exclusion: PeerExclusion | None = None,
) -> str:
    """sha256 over the canonical serialization of everything the metric depends on.

    Covers the cohort hash, target identity, report period, the precise input
    versions (each company's snapshot ``semantic_hash`` alongside its field
    values and gaps), the exclusion states with their bases (the target's own
    exclusion included) and the method rules. One metric's whole output shares
    this dependency scope - the first phase does no per-result-key trimming.
    """

    spec = metric_spec(metric_key)
    target = batch.target_company
    payload = {
        "schema": INPUT_HASH_SCHEMA,
        "method": METHOD,
        "method_version": METHOD_VERSION,
        "metric_key": metric_key,
        "rules": _rules_payload(spec),
        "cohort_content_hash": batch.cohort_content_hash,
        "cohort_ref": batch.cohort_ref.model_dump(mode="json"),
        "report_period": batch.report_period.isoformat(),
        "target": {
            "company_id": target.company_id,
            "listing_id": target.listing_id,
            "ticker": target.ticker,
        },
        "companies": [
            {
                "company_id": company.company_id,
                "listing_id": company.listing_id,
                "in_cohort": company.in_cohort,
                "report_period": company.report_period.isoformat(),
                "fields": {
                    name: {
                        "value": None if entry.value is None else str(entry.value),
                        "missing_reason": entry.missing_reason,
                        "conflict_reason": entry.conflict_reason,
                        "source_semantic": entry.source_semantic,
                        "source_version": entry.source_version,
                        "verification": entry.verification,
                    }
                    for name, entry in sorted(company.fields.items())
                },
            }
            for company in _company_index(batch)
        ],
        "decision": {
            "target_included": target_included,
            "target_value": None if target_value is None else str(target_value),
            "target_exclusion": None
            if target_exclusion is None
            else {"reason": target_exclusion.reason, "detail": target_exclusion.detail},
            "included_peer_ids": sorted(included_peer_ids),
            "excluded": [
                {
                    "company_id": exclusion.company_id,
                    "reason": exclusion.reason,
                    "detail": exclusion.detail,
                }
                for exclusion in sorted(excluded, key=lambda e: (e.company_id, e.reason))
            ],
        },
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _company_index(batch: FrozenBatch) -> list[BatchCompany]:
    """First occurrence per company id, ordered by company id (deterministic)."""

    index: dict[int, BatchCompany] = {}
    for company in batch.companies:
        index.setdefault(company.company_id, company)
    return [index[company_id] for company_id in sorted(index)]


# --- record building ----------------------------------------------------------


@dataclass(frozen=True)
class BuiltMetricCalculation:
    """Build outcome for one metric: a record, or a gap - never an empty ok."""

    metric_key: str
    record: CalculationRecord | None
    gap: MetricGap | None
    input_hash: str


def _required_fact_keys(spec: MetricSpec, outcome: MetricOutcome) -> list[tuple[int, str, str]]:
    """``(company_id, field, role)`` entries that must be cited as fact refs.

    Values that entered the outputs plus the applicability inputs (including
    the evidence behind every applicability exclusion); missing and conflicted
    entries stay in the exclusion audit instead.
    """

    keys: set[tuple[int, str, str]] = set()

    def value_keys(company_id: int) -> None:
        keys.add((company_id, spec.numerator_field, "value"))
        if spec.denominator_field is not None:
            keys.add((company_id, spec.denominator_field, "value"))
        if spec.needs_applicability_ref:
            assert spec.applicability_field is not None
            keys.add((company_id, spec.applicability_field, "applicability"))

    for inclusion in outcome.inclusions:
        value_keys(inclusion.company_id)
    if outcome.target_value is not None:
        value_keys(outcome.target_company_id)
    applicability_keys: list[tuple[int, PeerExclusion]] = []
    if outcome.target_exclusion is not None:
        applicability_keys.append((outcome.target_company_id, outcome.target_exclusion))
    applicability_keys.extend((exclusion.company_id, exclusion) for exclusion in outcome.exclusions)
    for company_id, exclusion in applicability_keys:
        if exclusion.reason in _APPLICABILITY_REASONS and spec.applicability_field is not None:
            keys.add((company_id, spec.applicability_field, "applicability"))
    return sorted(keys)


def metric_results(spec: MetricSpec, outcome: MetricOutcome) -> dict[str, Quantity]:
    """The result quantities of one metric outcome (pure; shared by the build
    and the save-path verification hook)."""

    results: dict[str, Quantity] = {}
    if outcome.target_value is not None:
        results["target"] = Quantity(value=outcome.target_value, unit=spec.unit)
    if outcome.peer_median is not None:
        results["peer_median"] = Quantity(value=outcome.peer_median, unit=spec.unit)
    if outcome.ascending_position is not None:
        results["ascending_position"] = Quantity(
            value=outcome.ascending_position, unit=_POSITION_UNIT
        )
    results["valid_peer_count"] = Quantity(
        value=Decimal(outcome.valid_peer_count), unit=_COUNT_UNIT
    )
    results["candidate_peer_count"] = Quantity(
        value=Decimal(outcome.candidate_peer_count), unit=_COUNT_UNIT
    )
    return results


def verify_built_metric_record(
    batch: FrozenBatch, metric_key: str, record: CalculationRecord
) -> None:
    """The trusted save-path recheck (WP12): recompute before persisting.

    The record is rebuilt from the same frozen batch by an independent
    computation and compared field by field: the input hash, the deterministic
    record identity derived from it, every result quantity and unit, and the
    per-metric details (status, target, period, cohort). A mismatch is a
    caller/build error - the record is never saved. The pure helper is the
    seam WP8's final transaction and the offline submission both run through
    (:func:`save_peer_calculations`).
    """

    outcome = compute_peer_metric(batch, metric_key)
    if isinstance(outcome, MetricGap):
        raise ValueError(
            f"peer metric {metric_key} recomputes to a gap ({outcome.reason}); "
            "no ok record may be saved for it"
        )
    spec = metric_spec(metric_key)
    if record.method != METHOD or record.method_version != METHOD_VERSION:
        raise ValueError("peer metric record carries a foreign method or version")
    expected_hash = outcome_input_hash(batch, metric_key, outcome)
    if record.input_hash != expected_hash:
        raise ValueError("peer metric record input_hash does not match the recomputed input hash")
    if record.id != uuid5(NAMESPACE_URL, f"peer-metric-record/{record.input_hash}"):
        raise ValueError("peer metric record identity does not match its input hash derivation")
    if record.results != metric_results(spec, outcome):
        raise ValueError("peer metric record results do not match the recomputed outcome")
    details = record.details if isinstance(record.details, dict) else {}
    target = batch.target_company
    expected_details = {
        "metric_key": metric_key,
        "metric_status": outcome.metric_status.value,
        "report_period": batch.report_period.isoformat(),
        "target_value": None if outcome.target_value is None else str(outcome.target_value),
        "target.company_id": target.company_id,
        "target.listing_id": target.listing_id,
        "target.ticker": target.ticker,
        "cohort.content_hash": batch.cohort_content_hash,
    }
    actual_details = {
        "metric_key": details.get("metric_key"),
        "metric_status": details.get("metric_status"),
        "report_period": details.get("report_period"),
        "target_value": details.get("target_value"),
        "target.company_id": (details.get("target") or {}).get("company_id")
        if isinstance(details.get("target"), dict)
        else None,
        "target.listing_id": (details.get("target") or {}).get("listing_id")
        if isinstance(details.get("target"), dict)
        else None,
        "target.ticker": (details.get("target") or {}).get("ticker")
        if isinstance(details.get("target"), dict)
        else None,
        "cohort.content_hash": (details.get("cohort") or {}).get("content_hash")
        if isinstance(details.get("cohort"), dict)
        else None,
    }
    if actual_details != expected_details:
        raise ValueError("peer metric record details do not match the recomputed outcome")


def build_peer_metric_record(
    batch: FrozenBatch,
    metric_key: str,
    *,
    evidence_refs: Mapping[tuple[int, str], ObjectRef],
    as_of: datetime,
    batch_run_id: str | None = None,
    require_fact_refs: bool = True,
) -> BuiltMetricCalculation:
    """Build one metric's ``CalculationRecord`` (pure; computes internally).

    ``evidence_refs`` maps ``(company_id, field)`` to the saved-evidence
    :class:`~investment_agent.investor.common.ObjectRef` of a materialized
    snapshot field. Every value and applicability input the record relies on
    must have a reference - a missing one is a caller error, because the record
    would cite facts it cannot support. Missing and conflicted inputs are not
    referenced: they live in the exclusion audit inside ``details`` only.

    ``require_fact_refs=False`` is the declared-inputs submission mode (WP6's
    offline path, reused by WP8): an input without a materialized reference is
    *declared* instead of failing the build - the record then cites only the
    refs it has, counts the rest in ``details["unmaterialized_inputs"]`` and
    says so in ``quality``. Nothing is hidden: the read side treats a record
    that used inputs but cites none of them as an evidence gap and shows it
    partial. The default keeps the strict raising behaviour for existing
    callers.
    """

    outcome = compute_peer_metric(batch, metric_key)
    if isinstance(outcome, MetricGap):
        return BuiltMetricCalculation(
            metric_key=metric_key, record=None, gap=outcome, input_hash=outcome.input_hash
        )
    spec = metric_spec(metric_key)

    refs: list[ObjectRef] = []
    fact_evidence: list[dict[str, Any]] = []
    unmaterialized = 0
    for company_id, field_name, role in _required_fact_keys(spec, outcome):
        ref = evidence_refs.get((company_id, field_name))
        if ref is None:
            if require_fact_refs:
                raise ValueError(
                    f"no saved evidence reference for company {company_id} field {field_name}; "
                    "materialize the field before saving the metric calculation"
                )
            unmaterialized += 1
            continue
        refs.append(ref)
        fact_evidence.append(
            {
                "company_id": company_id,
                "field": field_name,
                "role": role,
                "ref": ref.model_dump(mode="json"),
            }
        )

    results = metric_results(spec, outcome)

    target = batch.target_company
    input_hash = outcome_input_hash(batch, metric_key, outcome)
    details: dict[str, Any] = {
        "schema": DETAILS_SCHEMA,
        "metric_key": metric_key,
        "metric_status": outcome.metric_status.value,
        "target": {
            "company_id": target.company_id,
            "listing_id": target.listing_id,
            "ticker": target.ticker,
            "name": target.name,
        },
        "report_period": batch.report_period.isoformat(),
        "cohort": {
            "ref": batch.cohort_ref.model_dump(mode="json"),
            "content_hash": batch.cohort_content_hash,
        },
        # Placeholder only: the submitted batch mapping owns the real
        # association, and a reused record keeps its original batch identity.
        "batch_run_id": batch_run_id,
        "rules": _rules_payload(spec),
        "position_rule": POSITION_RULE,
        "target_value": None if outcome.target_value is None else str(outcome.target_value),
        "included_companies": [
            {
                "company_id": inclusion.company_id,
                "ticker": inclusion.ticker,
                "name": inclusion.name,
                "value": str(inclusion.value),
            }
            for inclusion in outcome.inclusions
        ],
        "excluded_companies": [
            {
                "company_id": exclusion.company_id,
                "ticker": exclusion.ticker,
                "name": exclusion.name,
                "reason": exclusion.reason,
                "detail": exclusion.detail,
                "is_target": False,
            }
            for exclusion in outcome.exclusions
        ]
        + (
            [
                {
                    "company_id": outcome.target_exclusion.company_id,
                    "ticker": outcome.target_exclusion.ticker,
                    "name": outcome.target_exclusion.name,
                    "reason": outcome.target_exclusion.reason,
                    "detail": outcome.target_exclusion.detail,
                    "is_target": True,
                }
            ]
            if outcome.target_exclusion is not None
            else []
        ),
        "fact_evidence": fact_evidence,
        "unmaterialized_inputs": unmaterialized,
        "input_hash": input_hash,
    }
    quality = outcome.quality
    if unmaterialized:
        quality = (
            *quality,
            f"{unmaterialized} of the metric's used inputs have no materialized evidence "
            "reference (snapshot-only inputs); the record cites only the refs it has and "
            "the read side must show this as an evidence gap",
        )
    record = CalculationRecord(
        # Pre-save identity only: save_calculation assigns the persisted id,
        # revision, created_at and actor. The derivation stays deterministic
        # (input-hash based, no clock) so the same input builds the same draft.
        id=uuid5(NAMESPACE_URL, f"peer-metric-record/{input_hash}"),
        revision=1,
        created_at=as_of,
        actor=METHOD,
        origin_ref=None,
        method=METHOD,
        method_version=METHOD_VERSION,
        fact_refs=tuple(refs),
        assumption_refs=(),
        input_hash=input_hash,
        as_of=as_of,
        # ``ok`` asserts exactly that the existing result keys are correct - the
        # per-metric business state (comparable / insufficient / ...) lives in
        # details.metric_status, and the quality notes carry the check coverage.
        applicable=True,
        status=CalculationStatus.OK,
        results=results,
        quality=quality,
        errors=(),
        details=details,
    )
    return BuiltMetricCalculation(
        metric_key=metric_key, record=record, gap=None, input_hash=input_hash
    )


def outcome_input_hash(batch: FrozenBatch, metric_key: str, outcome: MetricOutcome) -> str:
    """The metric's input hash, derived from its computed outcome."""

    return metric_input_hash(
        batch,
        metric_key,
        included_peer_ids=[inclusion.company_id for inclusion in outcome.inclusions],
        excluded=list(outcome.exclusions),
        target_included=outcome.target_value is not None,
        target_value=outcome.target_value,
        target_exclusion=outcome.target_exclusion,
    )


# --- saved-record query and idempotent submission ------------------------------


@dataclass(frozen=True)
class SavedPeerCalculation:
    """One saved peer metric record with its object reference."""

    ref: ObjectRef
    record: CalculationRecord


@dataclass(frozen=True)
class MetricCalculationRef:
    """One metric's submission outcome: newly saved, reused, or a kept gap."""

    metric_key: str
    state: Literal["saved", "reused", "gap"]
    ref: ObjectRef | None = None
    gap_reason: str | None = None


@dataclass(frozen=True)
class TargetCalculationRefs:
    """The per-target batch reference mapping WP8 submits (three metrics)."""

    target_company_id: int
    target_listing_id: int
    target_ticker: str
    report_period: date
    metric_refs: tuple[MetricCalculationRef, ...]

    def ref_for(self, metric_key: str) -> MetricCalculationRef | None:
        return next((entry for entry in self.metric_refs if entry.metric_key == metric_key), None)


def _peer_details(record: CalculationRecord) -> dict[str, Any] | None:
    """The method-owned details of a peer metric record, or ``None``."""

    details = record.details if isinstance(record.details, dict) else {}
    if record.method != METHOD or details.get("schema") != DETAILS_SCHEMA:
        return None
    if details.get("metric_key") not in METRIC_KEYS:
        return None
    if not isinstance(details.get("target"), dict):
        return None
    return details


async def find_peer_calculations(
    session: AsyncSession,
    *,
    owner_id: OwnerId,
    target_company_id: int | None = None,
    report_period: date | None = None,
    metric_key: str | None = None,
) -> list[SavedPeerCalculation]:
    """Saved peer metric records for one owner, filtered and ordered.

    Filters by method, target, report period and metric key - exactly the
    projection WP6 reads. The cohort is a single small roster (the design fixes
    the first-phase query scale), so the filtering walks the owner's saved
    calculation payloads instead of pre-building a JSON index projection; a
    later scale-up adds the index projection without changing this contract.
    """

    rows = list(
        (
            await session.execute(
                select(SavedCalculation).where(SavedCalculation.owner_id == owner_id)
            )
        ).scalars()
    )
    found: list[SavedPeerCalculation] = []
    for row in rows:
        try:
            record = CalculationRecord.model_validate(row.payload)
        except ValueError:
            # A payload that no longer validates cannot be identified as a peer
            # record; the strict read path (get_calculation) reports it loudly.
            continue
        details = _peer_details(record)
        if details is None:
            continue
        if metric_key is not None and details.get("metric_key") != metric_key:
            continue
        if (
            target_company_id is not None
            and details["target"].get("company_id") != target_company_id
        ):
            continue
        if report_period is not None and details.get("report_period") != report_period.isoformat():
            continue
        found.append(
            SavedPeerCalculation(
                ref=ObjectRef(kind="calculation", id=UUID(row.id), revision=row.revision),
                record=record,
            )
        )

    def _sort_key(saved: SavedPeerCalculation) -> tuple[str, str, str, str]:
        details = _peer_details(saved.record) or {}
        return (
            str(details.get("report_period", "")),
            str(details.get("metric_key", "")),
            saved.record.as_of.isoformat(),
            str(saved.ref.id),
        )

    found.sort(key=_sort_key)
    return found


async def save_peer_calculations(
    session: AsyncSession,
    *,
    owner_id: OwnerId,
    batch: FrozenBatch,
    evidence_refs: Mapping[tuple[int, str], ObjectRef],
    as_of: datetime,
    batch_run_id: str | None = None,
    require_fact_refs: bool = True,
) -> TargetCalculationRefs:
    """Compute all three metrics and submit their records idempotently.

    An input whose ``input_hash`` already has a saved record for this target,
    period and metric key reuses that record's UUID (method and inputs are
    re-verified before reuse; the old record keeps its original batch
    identity). A changed input appends a new record. A metric without any
    usable source fact keeps its gap and creates no ``ok`` record.

    ``require_fact_refs`` is passed through to :func:`build_peer_metric_record`
    (see there): only the declared-inputs submission path (WP6 offline / WP8
    batch finalize) sets it to ``False``.
    """

    service = EvidenceService(session)
    target = batch.target_company
    metric_refs: list[MetricCalculationRef] = []
    for metric_key in METRIC_KEYS:
        built = build_peer_metric_record(
            batch,
            metric_key,
            evidence_refs=evidence_refs,
            as_of=as_of,
            batch_run_id=batch_run_id,
            require_fact_refs=require_fact_refs,
        )
        if built.record is None:
            gap_reason = built.gap.reason if built.gap is not None else "unusable input"
            metric_refs.append(
                MetricCalculationRef(metric_key=metric_key, state="gap", gap_reason=gap_reason)
            )
            continue
        # Trusted save-path recheck (WP12): an independent recomputation from
        # the same frozen batch must reproduce the built record exactly.
        verify_built_metric_record(batch, metric_key, built.record)
        existing = await find_peer_calculations(
            session,
            owner_id=owner_id,
            target_company_id=target.company_id,
            report_period=batch.report_period,
            metric_key=metric_key,
        )
        match = next(
            (
                saved
                for saved in existing
                if saved.record.method == METHOD
                and saved.record.method_version == METHOD_VERSION
                and saved.record.input_hash == built.input_hash
            ),
            None,
        )
        if match is not None:
            metric_refs.append(
                MetricCalculationRef(metric_key=metric_key, state="reused", ref=match.ref)
            )
            continue
        ref = await service.save_calculation(owner_id, built.record, uuid4())
        metric_refs.append(MetricCalculationRef(metric_key=metric_key, state="saved", ref=ref))
    return TargetCalculationRefs(
        target_company_id=target.company_id,
        target_listing_id=target.listing_id,
        target_ticker=target.ticker,
        report_period=batch.report_period,
        metric_refs=tuple(metric_refs),
    )


__all__ = [
    "APPLICABILITY_INPUT_MISSING",
    "CFO_PARENT_PROFIT",
    "CONTRACT_REVENUE",
    "DETAILS_SCHEMA",
    "DUPLICATE_COMPANY",
    "FIELD_CONFLICT",
    "INPUT_HASH_SCHEMA",
    "METHOD",
    "METHOD_VERSION",
    "METRIC_KEYS",
    "MIN_VALID_PEERS",
    "MISSING_FIELD",
    "NON_POSITIVE_DENOMINATOR",
    "NON_POSITIVE_EQUITY",
    "NOT_IN_COHORT",
    "PERIOD_MISMATCH",
    "POSITION_RULE",
    "ROE",
    "SOURCE_FIELD_MISMATCH",
    "BatchCompany",
    "BatchField",
    "BuiltMetricCalculation",
    "FrozenBatch",
    "MetricCalculationRef",
    "MetricGap",
    "MetricOutcome",
    "MetricSpec",
    "PeerExclusion",
    "PeerInclusion",
    "SavedPeerCalculation",
    "TargetCalculationRefs",
    "ascending_position",
    "batch_field_from_snapshot",
    "build_peer_metric_record",
    "compute_peer_metric",
    "find_peer_calculations",
    "metric_input_hash",
    "metric_results",
    "metric_spec",
    "outcome_input_hash",
    "save_peer_calculations",
    "verify_built_metric_record",
]

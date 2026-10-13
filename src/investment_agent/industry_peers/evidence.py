"""Trusted field adaptation between annual snapshots and evidence (WP4).

The annual fact snapshot
(:class:`~investment_agent.storage.models.peer.PeerAnnualSnapshot`) is the
only place an unconfirmed or date-less peer fact may live.
:func:`peer_field_evidence` is the trusted path that turns one saved field
into an :class:`~investment_agent.evidence.contracts.EvidenceRecord`, and
``EvidenceService.save_evidence`` re-derives that record from the stored row
before accepting it. Verified evidence can therefore only come from a saved
snapshot whose per-field proof is complete: a client or model upload that
claims a peer source and ``verified`` never matches the re-derived record and
is refused.

Discipline fixed by the technical design (§年度事实快照):

* ``subject`` is the field's listing, ``source`` the snapshot version and the
  field locator (``fields.<metric>``). The record identity and content hash
  are derived, so a retry rebuilds the byte-identical request and the existing
  idempotency receipt replays; a different payload under the same operation
  identity is rejected by the receipt. Materialize each snapshot field once
  through :func:`peer_field_operation_id`.
* A source date keeps its own precision: the peer disclosure/revision date is
  a version-notice day (approximate), so ``published_at`` is that day's
  midnight UTC, ``available_from`` waits for the next day and never precedes
  the actual import time. A field with no source date at all is *not*
  materialized (:attr:`PeerFieldMaterialization.record` is ``None`` with an
  explicit reason); it stays in the snapshot only.
* ``verified`` requires the field's check status to be verified AND its proof
  to carry every key item (page, header, raw and parsed value, locator
  excerpt and hash, audit binding); otherwise the record degrades to
  ``candidate``. Conflicts are never judged while materializing - a supplier
  update is just a new version. :func:`peer_field_states` is the dedicated
  conflict/correction lookup: it reports pending-recheck or conflict for
  different values of the same semantics and returns correction references
  only for an explicitly declared correction basis.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import DatePrecision, EvidenceRecord
from investment_agent.evidence.snapshots import digest
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.storage.models.peer import PeerAnnualSnapshot

ADAPTER_ACTOR = "peer_snapshot_adapter"
"""Actor recorded on evidence saved through the trusted peer branch."""

UNKNOWN_PUBLICATION_TIME = "unknown_publication_time"
"""Materialization skip reason: the source states no date for the field.

Same reason string as the material layer's adapter, so pages can treat both
"stays in its own layer" outcomes uniformly."""

VERIFIED_PROOF_KEYS: tuple[str, ...] = (
    "original_ref", "pdf_hash", "report_identity", "accounting_scope", "checked_at", "check_rule_version",
    "pdf_page",
    "header",
    "raw_value",
    "parsed_value",
    "locator_excerpt",
    "locator_excerpt_sha256",
    "audit_record",
)
"""Proof items that must be non-empty before ``verified`` can be saved.

The items bind the saved value to the checked original (page, header, the
number as printed, the parsed value, the located excerpt with its hash and
the audit-record binding). Original identity, hash, period, accounting scope
and check time are mandatory. Non-empty metadata is only a prerequisite:
storage and publication re-read the owner-scoped original bytes and locator."""

_CORRECTION_KEY = "corrects"
"""Optional proof key declaring an explicit correction basis.

Its value is the ``id`` of the snapshot row this field version corrects. A
supplier refresh without this marker is only a new version: the query marks
the older value pending re-check or conflicting, never corrected."""


def peer_source_ref(row: PeerAnnualSnapshot, metric: str) -> ObjectRef:
    """The evidence source reference: this snapshot version plus field locator."""

    return ObjectRef(
        kind="peer_annual_snapshot",
        id=UUID(row.id),
        revision=1,
        field_path=f"fields.{metric}",
    )


def peer_subject_ref(row: PeerAnnualSnapshot) -> ObjectRef:
    """The listing the snapshot's facts belong to (every evidence subject)."""

    return ObjectRef(kind="listing", id=row.listing_id, revision=1)


def peer_field_operation_id(row: PeerAnnualSnapshot, metric: str) -> UUID:
    """Stable operation identity derived from the snapshot version + locator.

    One snapshot field materializes once: retries rebuild the identical
    request under this identity and replay the saved receipt instead of
    writing a second evidence row. Never derive the identity from wall-clock
    times or local paths - those change between retries by design.
    """

    return uuid5(NAMESPACE_URL, f"peer-annual-field-evidence/{row.id}/fields.{metric}")


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; stored values were written as UTC."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _proof_complete(row: PeerAnnualSnapshot, metric: str) -> bool:
    proof = row.verification.get(metric) if isinstance(row.verification, dict) else None
    if not isinstance(proof, dict):
        return False
    return all(proof.get(key) is not None and proof.get(key) != "" for key in VERIFIED_PROOF_KEYS)


@dataclass(frozen=True)
class PeerFieldMaterialization:
    """One trusted adapter outcome; ``record`` is ``None`` when not materialized."""

    snapshot_id: str
    metric: str
    record: EvidenceRecord | None
    reason: str | None

    @property
    def materialized(self) -> bool:
        return self.record is not None


def peer_field_evidence(
    row: PeerAnnualSnapshot,
    metric: str,
) -> PeerFieldMaterialization:
    """Re-derive one saved annual field as evidence; never raises for data.

    The record is rebuilt strictly from the stored row: the field entry fixes
    value, unit, currency and period; the snapshot row fixes subject, source
    version, provider and capture time; the proof decides whether ``verified``
    can be kept. The derivation is deterministic, so a rebuild of the same
    version is byte-identical and replays its idempotency receipt.

    A field whose source states no date is reported with
    ``reason=UNKNOWN_PUBLICATION_TIME`` and no record: the fact stays in the
    snapshot, does not become a citable ``EvidenceRecord`` and is never given
    a fabricated ``published_at``. A ``metric`` absent from the snapshot is a
    caller error and raises ``ValueError``.
    """

    fields = row.fields if isinstance(row.fields, dict) else {}
    if metric not in fields:
        raise ValueError(f"metric {metric!r} is not in peer annual snapshot {row.id}")
    field = PeerAnnualField.model_validate(fields[metric])

    source_day = field.disclosed_on or field.updated_on
    if source_day is None:
        # Not even a notice or revision date: no honest published_at exists.
        # The field stays snapshot-only; consumers show the explicit gap.
        return PeerFieldMaterialization(
            snapshot_id=row.id, metric=metric, record=None, reason=UNKNOWN_PUBLICATION_TIME
        )

    verified_by_proof = (
        field.verification is PeerFieldVerification.VERIFIED and _proof_complete(row, metric)
    )
    verification: Literal["candidate", "verified"] = "verified" if verified_by_proof else "candidate"
    quantity = Quantity(
        value=Decimal(field.value) if field.value is not None else None,
        unit=field.unit,
        currency=field.currency,
        unknown_reason=field.missing_reason,
    )
    # The stated day claims no hour: known only from the next day's start, and
    # never before the system actually imported the field.
    published_at = datetime.combine(source_day, time.min, UTC)
    available_from = max(published_at + timedelta(days=1), _aware(field.acquired_at))
    captured_at = _aware(row.captured_at)
    notice_semantics = (
        "version-notice date"
        if field.disclosed_on is not None
        else "supplier revision date"
    )
    quality = (
        f"peer annual {metric}; provider={row.provider}; adapter={row.adapter_version}; "
        f"source date is a {notice_semantics} (approximate day precision); "
        f"verification={field.verification.value}"
    )
    if field.verification is PeerFieldVerification.VERIFIED and not verified_by_proof:
        quality += "; verified proof incomplete, kept candidate"
    source_ref = peer_source_ref(row, metric)
    subject_ref = peer_subject_ref(row)
    payload = {
        "schema": "peer_field_evidence/1",
        "source": source_ref.model_dump(mode="json"),
        "subject": subject_ref.model_dump(mode="json"),
        "period": field.period.model_dump(mode="json"),
        "value": quantity.model_dump(mode="json"),
        "published_at": published_at.isoformat(),
        "available_from": available_from.isoformat(),
        "acquired_at": _aware(field.acquired_at).isoformat(),
        "captured_at": captured_at.isoformat(),
        "provider": row.provider,
        "adapter_version": row.adapter_version,
        "verification": verification,
        "quality": quality,
    }
    content_hash = digest(payload)
    record = EvidenceRecord(
        id=uuid5(NAMESPACE_URL, content_hash),
        revision=1,
        created_at=captured_at,
        actor=ADAPTER_ACTOR,
        origin_ref=None,
        source_ref=source_ref,
        source_kind="snapshot",
        locator=f"fields.{metric}",
        content_hash=content_hash,
        subject_ref=subject_ref,
        period=field.period,
        published_at=published_at,
        acquired_at=_aware(field.acquired_at),
        available_from=available_from,
        verification=verification,
        # Conflicts are reported by peer_field_states, never auto-judged here:
        # a supplier update is a new version, not a verdict on the old value.
        conflict="none",
        date_precision=DatePrecision.DATE,
        value=quantity,
        text=quality,
    )
    return PeerFieldMaterialization(snapshot_id=row.id, metric=metric, record=record, reason=None)


# --- conflict and correction lookup ------------------------------------------


@dataclass(frozen=True)
class PeerFieldVersionState:
    """One snapshot version's state for one metric."""

    source_ref: ObjectRef
    captured_at: datetime
    provider: str
    verification: str
    value_text: str | None
    unit: str
    currency: str | None
    period_basis: str
    accounting_scope: str | None
    state: Literal["consistent", "pending_recheck", "conflict", "corrected", "correction"]
    corrects: tuple[ObjectRef, ...]


@dataclass(frozen=True)
class PeerFieldConflictReport:
    """Dedicated conflict/correction view over one company's metric versions.

    Deliberately separate from the generic cross-source conflict machinery:
    peer snapshots never enter ``_source_conflict`` or ``document_limits``.
    ``status`` is ``consistent``; ``pending_recheck`` when versions of one
    source disagree (a supplier update is not a correction, the older value is
    just awaiting re-check); ``conflict`` when sources disagree;
    ``corrected`` when every disagreement is covered by an explicitly declared
    correction. ``correction_refs`` names the corrected snapshot versions and
    is empty unless a proof declares the basis - a materializing caller may
    connect them as evidence ``correction_refs`` only then.
    """

    company_id: int
    metric: str
    status: Literal["consistent", "pending_recheck", "conflict", "corrected"]
    versions: tuple[PeerFieldVersionState, ...]
    correction_refs: tuple[ObjectRef, ...]
    warnings: tuple[str, ...] = ()


async def peer_field_states(
    session: AsyncSession,
    *,
    company_id: int,
    metric: str,
    report_period: date | None = None,
    provider: str | None = None,
    unit: str | None = None,
    currency: str | None = None,
    period_basis: str | None = None,
    accounting_scope: str | None = None,
    cutoff: datetime | None = None,
) -> PeerFieldConflictReport:
    """Conflict/correction lookup over saved versions (read-only, awaited).

    Versions are matched by company, metric semantics, report period, unit,
    currency and accounting scope; values are compared only inside one such
    group (different units are different semantics, not a conflict). Missing
    values never participate. The query never mutates anything and never
    decides that a saved value was wrong: without an explicit correction
    basis, differing values stay ``pending_recheck`` (same provider) or
    ``conflict`` (different providers).
    """

    from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository

    repo = PeerAnnualSnapshotRepository(session)
    rows = [
        row
        for row in await repo.metric_versions(
            company_id=company_id, metric=metric, provider=provider
        )
        if report_period is None or row.report_period == report_period
    ]
    entries: list[tuple[PeerAnnualSnapshot, PeerAnnualField, dict[str, object]]] = []
    warnings: list[str] = []
    for row in rows:
        if cutoff is not None and _aware(row.captured_at) > cutoff:
            continue
        field = PeerAnnualField.model_validate(row.fields[metric])
        proof = row.verification.get(metric)
        proof = proof if isinstance(proof, dict) else {}
        entries.append((row, field, proof))

    def _in_scope(_row: PeerAnnualSnapshot, field: PeerAnnualField, proof: dict[str, object]) -> bool:
        return (
            (unit is None or field.unit == unit)
            and (currency is None or field.currency == currency)
            and (period_basis is None or field.period.basis == period_basis)
            and (accounting_scope is None or proof.get("accounting_scope") == accounting_scope)
        )

    entries = [entry for entry in entries if _in_scope(*entry)]
    by_id = {row.id: (row, field, proof) for row, field, proof in entries}

    corrections: dict[str, list[str]] = {}
    corrected_by: dict[str, list[str]] = {}
    for row, _field, proof in entries:
        target = proof.get(_CORRECTION_KEY)
        if not isinstance(target, str):
            continue
        target = target.strip()
        if target == row.id:
            warnings.append(f"snapshot {row.id} declares a correction of itself; ignored")
            continue
        if target not in by_id:
            warnings.append(
                f"snapshot {row.id} declares a correction of {target!r}, which is not part "
                "of this query's matched versions; no correction relation is built"
            )
            continue
        corrections.setdefault(row.id, []).append(target)
        corrected_by.setdefault(target, []).append(row.id)

    def _corrects_refs(row_id: str) -> tuple[ObjectRef, ...]:
        return tuple(
            peer_source_ref(by_id[target][0], metric) for target in corrections.get(row_id, ())
        )

    def _group_key(field: PeerAnnualField, proof: dict[str, object]) -> tuple[object, ...]:
        return (
            field.unit,
            field.currency,
            field.period.basis,
            field.period.start,
            field.period.end,
            proof.get("accounting_scope"),
        )

    groups: dict[
        tuple[object, ...], list[tuple[PeerAnnualSnapshot, PeerAnnualField, dict[str, object]]]
    ] = {}
    for entry in entries:
        groups.setdefault(_group_key(entry[1], entry[2]), []).append(entry)

    pending: set[str] = set()
    conflicted: set[str] = set()
    for group in groups.values():
        # Values are compared as decimals: 1.0 and 1.00 are one value; a
        # missing value never participates.
        distinct: dict[Decimal, set[str]] = {}
        for row, field, _proof in group:
            if field.value is None:
                continue
            distinct.setdefault(Decimal(field.value), set()).add(row.id)
        if len(distinct) <= 1:
            continue
        disagreeing = {row_id for ids in distinct.values() for row_id in ids}
        unresolved = [
            row_id
            for row_id in disagreeing
            if row_id not in corrections and row_id not in corrected_by
        ]
        if not unresolved:
            continue
        pending.update(unresolved)
        if len({by_id[row_id][0].provider for row_id in unresolved}) > 1:
            conflicted.update(unresolved)

    if conflicted:
        status: Literal["consistent", "pending_recheck", "conflict", "corrected"] = "conflict"
    elif pending:
        status = "pending_recheck"
    elif corrections:
        status = "corrected"
    else:
        status = "consistent"

    versions = []
    for row, field, proof in entries:
        if row.id in corrections:
            state: Literal["consistent", "pending_recheck", "conflict", "corrected", "correction"] = (
                "correction"
            )
        elif row.id in corrected_by:
            state = "corrected"
        elif row.id in conflicted:
            state = "conflict"
        elif row.id in pending:
            state = "pending_recheck"
        else:
            state = "consistent"
        versions.append(
            PeerFieldVersionState(
                source_ref=peer_source_ref(row, metric),
                captured_at=_aware(row.captured_at),
                provider=row.provider,
                verification=field.verification.value,
                value_text=str(field.value) if field.value is not None else None,
                unit=field.unit,
                currency=field.currency,
                period_basis=field.period.basis,
                accounting_scope=(
                    str(proof["accounting_scope"]) if proof.get("accounting_scope") is not None else None
                ),
                state=state,
                corrects=_corrects_refs(row.id),
            )
        )
    versions.sort(key=lambda state: state.captured_at)
    correction_refs = tuple(
        peer_source_ref(by_id[target][0], metric)
        for corrected in corrections.values()
        for target in corrected
    )
    return PeerFieldConflictReport(
        company_id=company_id,
        metric=metric,
        status=status,
        versions=tuple(versions),
        correction_refs=correction_refs,
        warnings=tuple(warnings),
    )

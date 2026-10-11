"""Material datasets into the research archive and the publication path (plan 10).

The bridge is the seam between the material layer and the existing research
archive, and it keeps three disciplines explicit:

* :func:`save_material_evidence` reads stored ``research_dataset`` rows, converts
  them with plan 07's :func:`~investment_agent.materials.evidence.material_evidence`
  and saves every convertible fact through ``EvidenceService.save_evidence``. A
  refusal from the store is an error, never a silent skip; only facts whose
  publication time is unprovable stay in the material layer, and they are
  reported one reason per fact.
* A dataset is accepted for a listing only when it is company level and names
  that listing's own company. Company facts are therefore never attached to
  another company's listing, and a dataset-level mismatch is an error rather
  than a partial batch.
* ``source_ref`` is not a fact identity: every fact of one dataset version
  shares ``(research_dataset, id, revision)``. Already-saved facts are
  recognised by their fact-level ``content_hash`` inside that version, so the
  same material version materialises the same evidence references again instead
  of duplicating rows.
* :func:`attach_material_evidence` is a thin wrapper around
  ``InvestorService.attach_artifacts``; it adds no artifact validation of its
  own.
* :func:`save_material_calculation` stores a method-owned calculation whose
  inputs are saved material facts. Its ``input_hash`` is
  :func:`material_calculation_input_hash`, which covers the method version and
  every input's exact reference, value, unit, currency and period.

Original-currency discipline: nothing here converts a value. A dataset stated
in HKD, USD or TWD reaches the published node in that currency; a conversion
remains a separate verified decision, and ``investor.publication_numbers``
still refuses one today.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord
from investment_agent.evidence.snapshots import digest
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Mutation, ObjectRef, OwnerId, Quantity
from investment_agent.investor.contracts import Case
from investment_agent.investor.enums import CalculationStatus
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.materials.contracts import SubjectKind
from investment_agent.materials.evidence import material_evidence
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchDataset

MATERIAL_CALCULATION_METHOD = "material_metrics"
"""Method name of a calculation over saved material evidence facts."""

MATERIAL_CALCULATION_VERSION = "material.metrics.v1"
"""Fixed formula version recorded on every material calculation."""


@dataclass(frozen=True)
class MaterialEvidenceBatch:
    """What one :func:`save_material_evidence` call saved and what stayed behind.

    ``refs`` are the saved evidence references in dataset and fact order;
    ``skipped`` carries one reason per fact that stayed in the material layer,
    using the reason constants of ``materials.evidence``; ``dataset_ids`` are the
    accepted dataset rows in call order.
    """

    refs: tuple[ObjectRef, ...]
    skipped: tuple[str, ...]
    dataset_ids: tuple[int, ...]


async def save_material_evidence(
    session: AsyncSession,
    *,
    listing_id: int,
    dataset_ids: Sequence[int],
    metrics: Sequence[str] | None = None,
    owner_id: OwnerId = "local_owner",
) -> MaterialEvidenceBatch:
    """Save every convertible fact of the given datasets as evidence.

    Each dataset must be a company-level row of the listing's own company
    (``materials`` does not enforce the company/listing binding, so this bridge
    does): a listing-level dataset, another company's dataset or a missing row
    raises ``InvestorError`` before anything is written, instead of attaching
    foreign facts or leaving a half-saved batch. ``metrics`` optionally restricts
    the conversion exactly as ``material_evidence`` does.

    Facts already saved from the same dataset version (same ``source_ref`` and
    ``content_hash``) are reused; everything else goes through
    ``EvidenceService.save_evidence``, whose re-materialization refusal
    propagates unchanged.
    """

    listing = await _listing(session, listing_id)
    subject_ref = _listing_ref(listing)
    evidence = EvidenceService(session)
    datasets = []
    for dataset_id in dict.fromkeys(dataset_ids):
        row = await session.get(ResearchDataset, dataset_id)
        if row is None:
            raise InvestorError(
                "invalid_reference", f"Research dataset {dataset_id} does not exist"
            )
        if row.subject_kind != SubjectKind.COMPANY.value or row.subject_id != listing.company_id:
            raise InvestorError(
                "invalid_reference",
                f"Research dataset {dataset_id} is not this listing company's material",
            )
        datasets.append(row)
    refs: list[ObjectRef] = []
    skipped: list[str] = []
    accepted: list[int] = []
    for row in datasets:
        records, reasons = material_evidence(row, subject_ref=subject_ref, metrics=metrics)
        skipped.extend(reasons)
        existing = await _saved_facts(session, owner_id, row.id, row.revision)
        for record in records:
            ref = existing.get(record.content_hash)
            if ref is None:
                ref = await evidence.save_evidence(owner_id, record, uuid4())
                existing[record.content_hash] = ref
            refs.append(ref)
        accepted.append(row.id)
    return MaterialEvidenceBatch(
        refs=tuple(refs), skipped=tuple(skipped), dataset_ids=tuple(accepted)
    )


async def attach_material_evidence(
    session: AsyncSession,
    *,
    case_id: UUID,
    refs: Sequence[ObjectRef],
    expected_revision: int | None = None,
    idempotency_key: UUID | None = None,
    owner_id: OwnerId = "local_owner",
) -> Case:
    """Attach saved evidence to the case's current episode; return the case.

    A thin wrapper around ``InvestorService.attach_artifacts``: the evidence
    must already resolve to this case's listing, the case revision is advanced
    by the attachment and the returned case is read after it. ``expected_revision``
    defaults to the current case revision; ``idempotency_key`` defaults to a
    fresh key, so a retry that must replay has to pass the same key explicitly.
    """

    foundation = InvestorService(session)
    if expected_revision is None:
        expected_revision = (await foundation.get_case(owner_id, case_id)).revision
    await foundation.attach_artifacts(
        owner_id,
        case_id,
        tuple(refs),
        Mutation(expected_revision=expected_revision, idempotency_key=idempotency_key or uuid4()),
    )
    return await foundation.get_case(owner_id, case_id)


def material_calculation_input_hash(
    records: Sequence[EvidenceRecord], *, method: str, method_version: str
) -> str:
    """Content identity of a material calculation's inputs.

    The digest covers the method and its version plus, per input and in the
    given order, the exact saved reference and the input's value, unit,
    currency and period. Changing one input value, unit, currency or period
    therefore changes the hash, and a calculation identity can never be reused
    across different inputs.
    """

    return digest(
        {
            "method": method,
            "method_version": method_version,
            "inputs": [
                {
                    "ref": {
                        "kind": "evidence",
                        "id": str(record.id),
                        "revision": record.revision,
                    },
                    "value": None
                    if record.value is None or record.value.value is None
                    else str(record.value.value),
                    "unit": None if record.value is None else record.value.unit,
                    "currency": None if record.value is None else record.value.currency,
                    "period": None
                    if record.period is None
                    else record.period.model_dump(mode="json"),
                }
                for record in records
            ],
        }
    )


async def save_material_calculation(
    session: AsyncSession,
    *,
    listing_id: int,
    fact_refs: Sequence[ObjectRef],
    results: Mapping[str, Quantity],
    as_of: datetime,
    method: str = MATERIAL_CALCULATION_METHOD,
    method_version: str = MATERIAL_CALCULATION_VERSION,
    operation_id: UUID | None = None,
    owner_id: OwnerId = "local_owner",
) -> ObjectRef:
    """Save one successful material calculation over saved evidence facts.

    Every reference must resolve, for this listing, to visible conflict-free
    evidence at ``as_of``; the record binds those exact facts and records
    :func:`material_calculation_input_hash` over them. Only an ``ok``,
    applicable result is stored here: a missing-input or not-applicable metric
    stays in the metric layer until a method owns how to present it, so no
    number without its inputs reaches publication.
    """

    if not fact_refs:
        raise InvestorError("invalid_reference", "A material calculation needs its evidence facts")
    if not results:
        raise InvestorError("invalid_reference", "A material calculation needs its results")
    listing = await _listing(session, listing_id)
    records = await _visible_facts(
        session, owner_id, _listing_ref(listing), fact_refs, as_of
    )
    record = CalculationRecord(
        id=uuid4(),
        revision=1,
        created_at=as_of,
        actor="material_calculation",
        origin_ref=None,
        method=method,
        method_version=method_version,
        fact_refs=tuple(fact_refs),
        assumption_refs=(),
        input_hash=material_calculation_input_hash(
            records, method=method, method_version=method_version
        ),
        assumptions=(),
        as_of=as_of,
        applicable=True,
        status=CalculationStatus.OK,
        results=dict(results),
    )
    return await EvidenceService(session).save_calculation(
        owner_id, record, operation_id or uuid4()
    )


async def _listing(session: AsyncSession, listing_id: int) -> Listing:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise InvestorError("invalid_reference", "A real listing is required")
    return listing


def _listing_ref(listing: Listing) -> ObjectRef:
    return ObjectRef(kind="listing", id=listing.id, revision=1)


async def _saved_facts(
    session: AsyncSession, owner_id: OwnerId, dataset_id: int, revision: int
) -> dict[str, ObjectRef]:
    """Facts already saved from one dataset version, keyed by content hash.

    Every fact of a dataset version shares its ``source_ref``, so the version
    row is only the lookup scope; the fact-level ``content_hash`` decides which
    fact is already stored.
    """

    rows = (
        await session.scalars(
            select(SavedEvidence).where(
                SavedEvidence.owner_id == owner_id,
                SavedEvidence.payload["source_kind"].as_string() == "research_dataset",
                SavedEvidence.payload["source_ref"]["id"].as_integer() == dataset_id,
                SavedEvidence.payload["source_ref"]["revision"].as_integer() == revision,
            )
        )
    ).all()
    return {
        str(row.payload["content_hash"]): ObjectRef(
            kind="evidence", id=UUID(str(row.id)), revision=row.revision
        )
        for row in rows
    }


async def _visible_facts(
    session: AsyncSession,
    owner_id: OwnerId,
    subject_ref: ObjectRef,
    refs: Sequence[ObjectRef],
    as_of: datetime,
) -> tuple[EvidenceRecord, ...]:
    """The exact visible facts behind ``refs``; any unusable input is an error."""

    evidence = EvidenceService(session)
    records: list[EvidenceRecord] = []
    for ref in refs:
        if ref.kind != "evidence" or ref.field_path not in {None, "value"}:
            raise InvestorError(
                "invalid_reference", "A material calculation input must be a saved evidence fact"
            )
        result = await evidence.get_evidence(owner_id, ref, as_of, subject_ref=subject_ref)
        if result.status != "ok" or result.data is None:
            raise InvestorError(
                "invalid_reference", "Material calculation input is not usable source evidence"
            )
        records.append(result.data)
    return tuple(records)

"""Exact local source resolution and current validity, independent of events.

Missing local bytes/rows mean unavailable, never invented legal withdrawal.
No latest-head substitution, fetch, parsing, capture, or verification upgrade.
"""

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.adapters import material as material_adapter
from investment_agent.evidence.adapters import snapshot as snapshot_adapter
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.disclosure_http import DisclosureError
from investment_agent.evidence.disclosure_profiles import require_approved_contract
from investment_agent.evidence.disclosures import DisclosureService
from investment_agent.evidence.documents import document_limits
from investment_agent.evidence.events import EventRow, event_material, event_model
from investment_agent.evidence.snapshots import UNITS, aware, materialize
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.materials.evidence import material_evidence
from investment_agent.research_events.canonical import normalize, payload_hash
from investment_agent.research_quality.contracts import MaterialMode, exact
from investment_agent.research_quality.envelope import (
    FrozenBlob,
    FrozenMaterial,
    FrozenReadEnvelope,
    evidence_material,
)
from investment_agent.storage.models.disclosure import DisclosureOriginal
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchDataset


class SourceValidityError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Frozen source is unavailable; a new case/spec may be required")


class SourceValidityService:
    def __init__(self, session: AsyncSession, owner: OwnerId = "local_owner") -> None:
        self.session, self.owner = session, owner

    async def resolve(
        self,
        ref: ObjectRef,
        listing: ObjectRef,
        cutoff: datetime,
        mode: MaterialMode = "observed_materials",
        *,
        now: datetime | None = None,
        _stack: tuple[ObjectRef, ...] = (),
    ) -> FrozenMaterial:
        cutoff = utc_timestamp(cutoff)
        at = utc_timestamp(now) if now is not None else datetime.now(UTC)
        exact(listing, "listing", immutable=True)
        if ref in _stack or len(_stack) >= 8:
            raise SourceValidityError("source_verification_failed")
        if await self.session.get(Listing, listing.id, populate_existing=True) is None:
            raise SourceValidityError("source_unavailable")
        try:
            if ref.kind == "evidence":
                result = await self._evidence(ref, listing, cutoff, at, mode, (*_stack, ref))
            elif ref.kind in {"financial_snapshot", "market_snapshot"}:
                result = await self._snapshot(ref, listing)
            elif event_model(ref.kind) is not None:
                model = event_model(ref.kind)
                assert model is not None
                row = await self.session.get(model, ref.id, populate_existing=True)
                if not isinstance(row, EventRow) or ref.revision != 1:
                    raise SourceValidityError("source_unavailable")
                record = event_material(row, ref.field_path or "text")
                result = evidence_material(
                    ref, record, category="event", source_algorithm="saved-event-materialize.v1"
                )
            elif ref.kind == "research_dataset":
                result = await self._dataset(ref, listing)
            elif ref.kind == "calculation":
                result = await self._calculation(ref, listing, cutoff, at, mode, (*_stack, ref))
            elif ref.kind == "disclosure_original":
                result = await self._original(ref, listing, cutoff, at)
                await self._original_corrections(ref, at)
            else:
                raise SourceValidityError("source_unavailable")
            if result.subject_ref != listing:
                raise SourceValidityError("source_unavailable")
            # Reconstructed labels never silently relax any of the three times.
            if max(result.published_at, result.acquired_at, result.available_from) > cutoff:
                raise SourceValidityError("source_after_cutoff")
            return result
        except SourceValidityError:
            raise
        except (InvestorError, DisclosureError, ValueError, KeyError, TypeError) as exc:
            raise SourceValidityError("source_verification_failed") from exc

    async def _snapshot(self, ref: ObjectRef, listing: ObjectRef) -> FrozenMaterial:
        model = FinancialSnapshot if ref.kind == "financial_snapshot" else MarketSnapshot
        row = await self.session.get(model, ref.id, populate_existing=True)
        if (
            not isinstance(row, FinancialSnapshot | MarketSnapshot)
            or row.version != ref.revision
            or row.listing_id != listing.id
        ):
            raise SourceValidityError("source_unavailable")
        candidates = []
        for field in UNITS:
            try:
                record = materialize(row, field)
            except InvestorError:
                continue
            if record.source_ref == ref:
                candidates.append(record)
        if len(candidates) != 1:
            raise SourceValidityError("source_unavailable")
        return evidence_material(
            ref, candidates[0], category="snapshot", source_algorithm="snapshot-materialize.v1"
        )

    async def _evidence(
        self,
        ref: ObjectRef,
        listing: ObjectRef,
        cutoff: datetime,
        now: datetime,
        mode: MaterialMode,
        stack: tuple[ObjectRef, ...],
    ) -> FrozenMaterial:
        # Populate existing first: a reused session cannot conceal lost ownership.
        row = await self.session.get(
            SavedEvidence, (str(ref.id), ref.revision), populate_existing=True
        )
        if row is None or row.owner_id != self.owner:
            raise SourceValidityError("source_unavailable")
        result = await EvidenceService(self.session).get_evidence(
            self.owner, ref, cutoff, subject_ref=listing, visible_at=now
        )
        if result.data is None:
            raise SourceValidityError("source_unavailable")
        record = result.data
        # Never treat a saved annotation as independent proof of its source.
        blob = None
        if record.source_kind == "snapshot":
            await snapshot_adapter.materialize_expected(self.session, record)
            await self.resolve(record.source_ref, listing, cutoff, mode, now=now, _stack=stack)
        elif record.source_kind == "research_dataset":
            await material_adapter.materialize_expected(self.session, record)
            await self.resolve(record.source_ref, listing, cutoff, mode, now=now, _stack=stack)
        elif record.source_kind == "document":
            original = await self._original(
                record.source_ref.model_copy(update={"field_path": None}), listing, cutoff, now
            )
            blob = original.blob
            await self._original_corrections(record.source_ref, now)
            if (
                record.document_provenance is not None
                and record.document_provenance.original_hash != original.content_hash
            ):
                raise SourceValidityError("source_verification_failed")
            if (
                record.text is not None
                and record.document_provenance is None
                and hashlib.sha256(record.text.encode()).hexdigest() != record.content_hash
            ):
                raise SourceValidityError("source_verification_failed")
            await self._corrections(record, now)
            limits = await document_limits(self.session, record, now, visible_at=now)
            if limits:
                raise SourceValidityError("source_verification_failed")
        elif record.source_kind != "user_report" or record.verification != "user_report":
            raise SourceValidityError("source_verification_failed")
        await self._corrections(record, now)
        if result.status != "ok":
            raise SourceValidityError("source_verification_failed")
        return evidence_material(
            ref, record, category="evidence", blob=blob, source_algorithm="saved-evidence-exact.v1"
        )

    async def _corrections(self, record: EvidenceRecord, now: datetime) -> None:
        # Only existing verified correction records trigger supersession. A
        # model/user title or arbitrary URL is not withdrawal/correction proof.
        rows = await self.session.scalars(
            select(SavedEvidence)
            .where(
                SavedEvidence.owner_id == self.owner,
                SavedEvidence.payload["subject_ref"]["id"].as_integer() == record.subject_ref.id,
            )
            .execution_options(populate_existing=True)
        )
        for row in rows:
            other = EvidenceRecord.model_validate(row.payload)
            if (
                other.verification == "verified"
                and other.source_kind == "document"
                and other.document_provenance is not None
                and max(
                    other.created_at, other.published_at, other.acquired_at, other.available_from
                )
                <= now
                and any(
                    ref.kind == "evidence"
                    and ref.id == record.id
                    and ref.revision == record.revision
                    for ref in other.correction_refs
                )
            ):
                # Its proof must still resolve to owned original bytes.
                await self._original(
                    other.source_ref.model_copy(update={"field_path": None}),
                    record.subject_ref,
                    now,
                    now,
                )
                raise SourceValidityError("superseded_source")

    async def _original_corrections(self, reference: ObjectRef, now: datetime) -> None:
        rows = await self.session.scalars(
            select(SavedEvidence)
            .where(
                SavedEvidence.owner_id == self.owner,
                SavedEvidence.payload["source_ref"]["kind"].as_string() == "disclosure_original",
                SavedEvidence.payload["source_ref"]["id"].as_string() == str(reference.id),
            )
            .execution_options(populate_existing=True)
        )
        for row in rows:
            await self._corrections(EvidenceRecord.model_validate(row.payload), now)

    async def _dataset(self, ref: ObjectRef, listing: ObjectRef) -> FrozenMaterial:
        row = await self.session.get(ResearchDataset, ref.id, populate_existing=True)
        owner_listing = await self.session.get(Listing, listing.id)
        if (
            row is None
            or row.revision != ref.revision
            or ref.field_path is not None
            or owner_listing is None
        ):
            raise SourceValidityError("source_unavailable")
        if (row.subject_kind == "company" and row.subject_id != owner_listing.company_id) or (
            row.subject_kind == "listing" and row.subject_id != listing.id
        ):
            raise SourceValidityError("source_unavailable")
        records, gaps = material_evidence(
            row, subject_ref=listing, created_at=aware(row.captured_at)
        )
        if not records or gaps:
            raise SourceValidityError("source_unavailable")
        if (
            await self.session.scalar(
                select(ResearchDataset.id).where(ResearchDataset.previous_id == row.id).limit(1)
            )
            is not None
        ):
            raise SourceValidityError("superseded_source")
        data = normalize(
            {
                "records": records,
                "dataset_hash": row.content_hash,
                "adapter_version": row.adapter_version,
                "schema_version": row.schema_version,
            }
        )
        assert isinstance(data, dict)
        return FrozenMaterial(
            ref=ref,
            subject_ref=listing,
            category="dataset",
            content_hash=row.content_hash,
            payload_hash=payload_hash(data),
            payload=data,
            published_at=max(record.published_at for record in records),
            acquired_at=max(record.acquired_at for record in records),
            available_from=max(record.available_from for record in records),
            locator="dataset.records",
            verification="candidate",
            source_algorithm="material-evidence.v1",
        )

    async def _calculation(
        self,
        ref: ObjectRef,
        listing: ObjectRef,
        cutoff: datetime,
        now: datetime,
        mode: MaterialMode,
        stack: tuple[ObjectRef, ...],
    ) -> FrozenMaterial:
        result = await EvidenceService(self.session).get_calculation(
            self.owner, ref, cutoff, visible_at=now
        )
        if result.status != "ok" or result.data is None:
            raise SourceValidityError("source_unavailable")
        record = result.data
        deps = tuple(dict.fromkeys((*record.fact_refs, *record.assumption_refs)))
        if not deps:
            raise SourceValidityError("source_verification_failed")
        sources = [
            await self.resolve(dep, listing, cutoff, mode, now=now, _stack=stack) for dep in deps
        ]
        data = normalize(record)
        assert isinstance(data, dict)
        return FrozenMaterial(
            ref=ref,
            subject_ref=listing,
            category="calculation",
            content_hash=payload_hash(record),
            payload_hash=payload_hash(data),
            payload=data,
            published_at=max(source.published_at for source in sources),
            acquired_at=max(record.created_at, *(source.acquired_at for source in sources)),
            available_from=max(record.as_of, *(source.available_from for source in sources)),
            locator=ref.field_path or "results",
            verification="not_applicable",
            dependencies=deps,
            source_algorithm="saved-calculation-read.v1",
        )

    async def _original(
        self, ref: ObjectRef, listing: ObjectRef, cutoff: datetime, now: datetime
    ) -> FrozenMaterial:
        if ref.revision != 1:
            raise SourceValidityError("source_unavailable")
        row = await self.session.get(DisclosureOriginal, str(ref.id), populate_existing=True)
        if row is None or row.owner_id != self.owner or row.payload.get("listing_id") != listing.id:
            raise SourceValidityError("source_unavailable")
        contract = row.payload.get("contract_hash")
        if contract is not None:
            try:
                require_approved_contract(
                    contract, now=now, protocol_hash=row.payload.get("protocol_hash")
                )
            except DisclosureError as exc:
                raise SourceValidityError(
                    "source_withdrawn"
                    if str(exc) == "source_profile_revoked"
                    else "source_unavailable"
                ) from exc
        # Read checks the actual stored blob's SHA-256, never the URL. It does
        # not make an HTTP request or parse bytes.
        original, _body = await DisclosureService(self.session, self.owner).read(
            UUID(str(ref.id)), cutoff
        )
        whole = ref.model_copy(update={"field_path": None})
        blob = FrozenBlob(original_ref=whole, content_hash=row.content_hash)
        data: dict[str, Any] = {
            "original_ref": normalize(whole),
            "content_hash": row.content_hash,
            "verification": "candidate",
            "coverage": "immutable blob reference; bytes are not copied",
        }
        if ref.field_path is not None:
            saved = list(
                await self.session.scalars(
                    select(SavedEvidence)
                    .where(
                        SavedEvidence.owner_id == self.owner,
                        SavedEvidence.payload["source_ref"]["id"].as_string() == str(ref.id),
                        SavedEvidence.payload["source_ref"]["field_path"].as_string()
                        == ref.field_path,
                    )
                    .execution_options(populate_existing=True)
                )
            )
            if len(saved) != 1:
                raise SourceValidityError("source_unavailable")
            record = EvidenceRecord.model_validate(saved[0].payload)
            if (
                record.source_ref != ref
                or record.text is None
                or hashlib.sha256(record.text.encode()).hexdigest() != record.content_hash
            ):
                raise SourceValidityError("source_verification_failed")
            if max(record.published_at, record.acquired_at, record.available_from) > cutoff:
                raise SourceValidityError("source_after_cutoff")
            return evidence_material(
                ref,
                record,
                category="original",
                blob=blob,
                source_algorithm="saved-original-fragment.v1",
            )
        return FrozenMaterial(
            ref=ref,
            subject_ref=listing,
            category="original",
            content_hash=row.content_hash,
            payload_hash=payload_hash(data),
            payload=data,
            published_at=datetime.fromisoformat(original["first_published_at"]),
            acquired_at=datetime.fromisoformat(original["acquired_at"]),
            available_from=datetime.fromisoformat(original["available_from"]),
            locator="immutable_blob",
            verification="candidate",
            blob=blob,
            source_algorithm="disclosure-read.v1",
        )

    async def validate(self, envelope: FrozenReadEnvelope, *, now: datetime | None = None) -> None:
        # Reparse to reject mutated in-memory dicts before comparing live facts.
        checked = FrozenReadEnvelope.model_validate(envelope.model_dump())
        for material in checked.materials:
            current = await self.resolve(
                material.ref,
                checked.identity.listing_ref,
                checked.cutoff,
                checked.material_mode,
                now=now,
            )
            if current != material:
                raise SourceValidityError("source_hash_changed")

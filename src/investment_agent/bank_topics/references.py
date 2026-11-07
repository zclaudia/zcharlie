"""Read-only bank-topic fact resolution, never a publication/evidence whitelist.

Every formal bank calculation entry must use this resolver. Pure observations
and dataclass flags are internal results, not source-authentication credentials.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.bank_topics.contracts import (
    AnnualPeriod,
    FactBinding,
    SourceTiming,
    TopicFactRevision,
    TopicRef,
    TopicRevision,
)
from investment_agent.bank_topics.facts import (
    FactObservation,
    bank_content_hash,
    observation_from_topic_fact,
)
from investment_agent.bank_topics.proofs import (
    CMB_FY2025_SHA256,
    BankProofError,
    check_bank_fact_proof,
)
from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.evidence.adapters import peer as peer_adapter
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.industry_peers.contracts import PeerAnnualField
from investment_agent.industry_peers.evidence import peer_field_evidence, peer_field_states
from investment_agent.industry_peers.originals import validate_field_original
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.bank_topics import TopicFactRevision as FactRow
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.bank_topics import (
    BankTopicReferenceError,
    BankTopicRepository,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _aware(value: datetime) -> datetime:
    # SQLite DateTime columns were saved as UTC and lose their offset on read.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise BankTopicReferenceError("invalid_reference")
    try:
        return utc_timestamp(datetime.fromisoformat(value))
    except ValueError as exc:
        raise BankTopicReferenceError("invalid_reference") from exc


def publication_upper(timing: SourceTiming) -> datetime | None:
    if timing.published_precision == "timestamp":
        return timing.published_at
    if timing.published_precision == "date":
        assert timing.published_date is not None
        return datetime.combine(timing.published_date + timedelta(days=1), time.min, SHANGHAI).astimezone(UTC) - timedelta(microseconds=1)
    return None


def fact_payload(row: FactRow) -> TopicFactRevision:
    """Reconstruct the declared DTO; created_at is additionally checked as visibility."""
    values = {key: getattr(row, key) for key in TopicFactRevision.model_fields
              if key not in {"proof", "timing", "unknown_reason"}}
    values["unknown_reason"] = row.reason
    values["proof"] = row.proof
    values["timing"] = {
        "published_at": _aware(row.published_at) if row.published_at is not None else None,
        "published_date": row.published_date, "published_precision": row.published_precision,
        "acquired_at": _aware(row.acquired_at) if row.acquired_at is not None else None,
        "checked_at": _aware(row.checked_at) if row.checked_at is not None else None,
        "review_actor": row.review_actor,
    }
    return TopicFactRevision.model_validate(values)


class BankTopicReferenceResolver:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = BankTopicRepository(session)

    async def resolve_fact(
        self, owner_id: str, reference: TopicRef | ObjectRef, *, listing_id: int, as_of: datetime,
    ) -> FactObservation:
        with self.session.no_autoflush:
            return await self._resolve_fact(owner_id, reference, listing_id=listing_id, as_of=as_of)

    async def _resolve_fact(
        self, owner_id: str, reference: TopicRef | ObjectRef, *, listing_id: int, as_of: datetime,
    ) -> FactObservation:
        cutoff = utc_timestamp(as_of)
        try:
            ref = TopicRef.model_validate(reference.model_dump())
        except ValidationError as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        if ref.kind != "topic_revision" or ref.field_path is None:
            raise BankTopicReferenceError("invalid_reference")
        result = await self.repository.resolve_reference(owner_id, ref, listing_id=listing_id)
        row, bound = result.revision, result.fact
        if row is None or bound is None:
            raise BankTopicReferenceError("invalid_reference")
        try:
            parent = TopicRevision.model_validate({key: _aware(getattr(row, key)) if key == "as_of" else getattr(row, key)
                                                   for key in TopicRevision.model_fields})
        except (ValidationError, ValueError) as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        if bank_content_hash(parent) != row.content_hash:
            raise BankTopicReferenceError("invalid_reference")
        listing = await self.session.get(Listing, listing_id)
        if listing is None or format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code) != "600036.SH":
            raise BankTopicReferenceError("not_found")
        public_ref = ObjectRef.model_validate(ref.model_dump())
        if bound.local_fact is not None:
            observation = await self._topic_fact(owner_id, bound.local_fact, listing_id, public_ref, cutoff)
        else:
            assert bound.external_ref is not None
            observation = await self._external(owner_id, UUID(row.case_id), bound.binding, bound.external_ref,
                                               listing_id, public_ref, cutoff, set())
        if parent.as_of > cutoff or _aware(row.created_at) > cutoff:
            return replace(observation, value=None, unknown_reason="not_visible_as_of", proof_checked=False)
        return observation

    async def resolve_many(
        self, owner_id: str, references: Sequence[TopicRef | ObjectRef], *, listing_id: int, as_of: datetime,
    ) -> tuple[FactObservation, ...]:
        if len(references) > 100:
            raise BankTopicReferenceError("invalid_reference")
        return tuple([await self.resolve_fact(owner_id, ref, listing_id=listing_id, as_of=as_of)
                      for ref in references])

    async def _topic_fact(
        self, owner_id: str, row: FactRow, listing_id: int, public_ref: ObjectRef, cutoff: datetime,
    ) -> FactObservation:
        try:
            fact = fact_payload(row)
        except (ValidationError, ValueError) as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        if bank_content_hash(fact) != row.content_hash:
            raise BankTopicReferenceError("invalid_reference")
        if any(
            ref.kind != "disclosure_original" or ref.revision != 1 or ref.field_path is not None for ref in fact.source_refs
        ):
            raise BankTopicReferenceError("invalid_reference")
        if fact.proof is None:
            return replace(observation_from_topic_fact(fact, listing_id=listing_id, source_refs=(public_ref, *fact.source_refs)),
                           value=None, unknown_reason="needs_original_proof")
        if fact.proof.original_ref not in fact.source_refs:
            raise BankTopicReferenceError("invalid_reference")
        body, source_visible = await self._original(owner_id, listing_id, fact, cutoff)
        observation = observation_from_topic_fact(fact, listing_id=listing_id,
                                                   source_refs=(public_ref, *fact.source_refs))
        upper = publication_upper(fact.timing)
        visible = source_visible and upper is not None and upper <= cutoff and fact.timing.acquired_at is not None and fact.timing.acquired_at <= cutoff and _aware(row.created_at) <= cutoff
        if fact.timing.checked_at is not None and fact.timing.checked_at > cutoff:
            visible = False
        if not visible:
            reason = "unknown_publication_time" if upper is None else "unknown_acquisition_time" if fact.timing.acquired_at is None else "not_visible_as_of"
            return replace(observation, value=None, unknown_reason=reason, proof_checked=False)
        try:
            check_bank_fact_proof(body, fact)
        except BankProofError as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        return replace(observation, proof_checked=True)

    async def _original(
        self, owner_id: str, listing_id: int, fact: TopicFactRevision, cutoff: datetime,
    ) -> tuple[bytes, bool]:
        assert fact.proof is not None
        ref = fact.proof.original_ref
        original = await self.session.get(DisclosureOriginal, str(ref.id))
        if original is None or original.owner_id != owner_id or original.payload.get("listing_id") != listing_id:
            raise BankTopicReferenceError("not_found")
        item = await self.session.get(DisclosureItem, original.item_id)
        if item is None or item.owner_id != owner_id or item.listing_id != listing_id:
            raise BankTopicReferenceError("not_found")
        payload = original.payload
        if payload.get("ticker") != "600036.SH" or item.payload.get("ticker") != "600036.SH" or payload.get("disclosure_id") != item.payload.get("disclosure_id"):
            raise BankTopicReferenceError("invalid_reference")
        title = payload.get("title")
        url = payload.get("source_url")
        if not isinstance(title, str) or "2025年度报告" not in title.replace(" ", "") or any(token in title for token in ("摘要", "英文", "H股")):
            raise BankTopicReferenceError("invalid_reference")
        if not isinstance(url, str) or url != payload.get("resolved_url") or url != item.payload.get("source_url"):
            raise BankTopicReferenceError("invalid_reference")
        from investment_agent.evidence.disclosure_http import validate_url
        try:
            validate_url(url, "original")
        except ValueError as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        blob = await self.session.get(DisclosureBlob, original.content_hash)
        if blob is None or hashlib.sha256(blob.body).hexdigest() != original.content_hash or original.content_hash != fact.proof.pdf_sha256:
            raise BankTopicReferenceError("invalid_reference")
        if payload.get("content_hash", original.content_hash) != original.content_hash or payload.get("size", len(blob.body)) != len(blob.body):
            raise BankTopicReferenceError("invalid_reference")
        times = [_stamp(payload.get(key)) for key in ("first_published_at", "acquired_at", "available_from")]
        if fact.timing.acquired_at is not None and fact.timing.acquired_at < times[1]:
            raise BankTopicReferenceError("invalid_reference")
        # The reviewed source only proves a public date, never a midnight instant.
        if times[0].astimezone(SHANGHAI).date() != date(2026, 3, 28):
            raise BankTopicReferenceError("invalid_reference")
        if fact.timing.published_precision == "timestamp" or fact.timing.published_precision == "date" and fact.timing.published_date != date(2026, 3, 28):
            raise BankTopicReferenceError("invalid_reference")
        return blob.body, all(stamp <= cutoff for stamp in times)

    async def _external(
        self, owner_id: str, case_id: UUID, binding: FactBinding, ref: ObjectRef,
        listing_id: int, public_ref: ObjectRef, cutoff: datetime, path: set[str],
    ) -> FactObservation:
        identity = f"{ref.kind}:{ref.id}:{ref.revision}:{ref.field_path}"
        if identity in path or len(path) >= 10:
            raise BankTopicReferenceError("invalid_reference")
        path = {*path, identity}
        if ref.kind == "peer_annual_snapshot":
            return await self._peer(owner_id, case_id, binding, ref, listing_id, public_ref, cutoff)
        if ref.kind != "evidence" or ref.field_path not in {None, "value"}:
            raise BankTopicReferenceError("invalid_reference")
        row = await self.session.get(SavedEvidence, (str(ref.id), ref.revision))
        if row is None or row.owner_id != owner_id:
            raise BankTopicReferenceError("not_found")
        try:
            record = EvidenceRecord.model_validate(row.payload)
        except ValidationError as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        if record.id != ref.id or record.revision != ref.revision or record.subject_ref != ObjectRef(kind="listing", id=listing_id, revision=1):
            raise BankTopicReferenceError("invalid_reference")
        # The initial legacy bridge is the already-supported bank NIM peer
        # adapter. Numeric document/snapshot adapters do not support the new
        # NII/decomposition metrics; no generic verified flag opens that scope.
        if record.source_kind != "snapshot" or record.source_ref.kind != "peer_annual_snapshot":
            raise BankTopicReferenceError("invalid_reference")
        observation = await self._external(owner_id, case_id, binding, record.source_ref,
                                           listing_id, public_ref, cutoff, path)
        try:
            await peer_adapter.materialize_expected(self.session, record)
            visible = await EvidenceService(self.session).get_evidence(
                cast(OwnerId, owner_id), ref, cutoff, subject_ref=record.subject_ref, visible_at=cutoff,
            )
        except (InvestorError, ValueError) as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        refs = tuple(dict.fromkeys((public_ref, ref, *observation.source_refs)))
        observation = replace(observation, basis_kind="evidence", source_refs=refs)
        if visible.data is None:
            return replace(observation, value=None, unknown_reason="not_visible_as_of", proof_checked=False)
        if visible.status != "ok" or record.verification != "verified":
            return replace(observation, verification="conflict" if visible.status == "conflict" else record.verification,
                           proof_checked=False, unknown_reason="source_conflict" if visible.status == "conflict" else observation.unknown_reason)
        if record.value is None or record.value.value != observation.value or record.value.unit != observation.unit:
            raise BankTopicReferenceError("invalid_reference")
        return observation

    async def _peer(
        self, owner_id: str, case_id: UUID, binding: FactBinding, ref: ObjectRef,
        listing_id: int, public_ref: ObjectRef, cutoff: datetime,
    ) -> FactObservation:
        if binding.metric != "nim" or ref.revision != 1 or ref.field_path != "fields.net_interest_margin":
            raise BankTopicReferenceError("invalid_reference")
        row = await self.session.get(PeerAnnualSnapshot, str(ref.id))
        listing = await self.session.get(Listing, listing_id)
        if row is None or listing is None or row.listing_id != listing_id or row.company_id != listing.company_id:
            raise BankTopicReferenceError("not_found")
        metric = "net_interest_margin"
        try:
            field = PeerAnnualField.model_validate(row.fields[metric])
            materialized = peer_field_evidence(row, metric)
            period = AnnualPeriod(start=field.period.start, end=field.period.end)
        except (KeyError, ValidationError, ValueError) as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        if field.metric != metric or field.period.basis not in {"annual_ratio", "period_to_date/annual"} or row.report_period != field.period.end or field.unit != "fraction" or field.currency not in {None, "CNY"}:
            raise BankTopicReferenceError("invalid_reference")
        proof = row.verification.get(metric, {})
        if not isinstance(proof, dict) or proof.get("accounting_scope") != "group_consolidated":
            scope, reason = "unknown", "scope_conflict"
        else:
            scope, reason = "consolidated", None
        source_refs: tuple[ObjectRef, ...] = (public_ref, ref)
        record = materialized.record
        visible = record is not None and max(record.available_from, record.acquired_at, _aware(row.captured_at)) <= cutoff
        checked = False
        accounting = "unknown"
        verification = field.verification.value
        if reason == "scope_conflict":
            verification = "scope_conflict"
        if record is not None and record.verification != "verified":
            verification = "candidate"
        if record is not None and record.verification == "verified" and reason is None:
            try:
                original_ref = ObjectRef.model_validate(proof.get("original_ref"))
                original = await self.session.get(DisclosureOriginal, str(original_ref.id))
                if original_ref.kind != "disclosure_original" or original_ref.revision != 1 or original_ref.field_path is not None or original is None or original.owner_id != owner_id or original.payload.get("listing_id") != listing_id:
                    raise BankTopicReferenceError("not_found")
                item = await self.session.get(DisclosureItem, original.item_id)
                blob = await self.session.get(DisclosureBlob, original.content_hash)
                if item is None or item.owner_id != owner_id or item.listing_id != listing_id:
                    raise BankTopicReferenceError("not_found")
                if blob is None or hashlib.sha256(blob.body).hexdigest() != original.content_hash or proof.get("pdf_hash") != original.content_hash:
                    raise BankTopicReferenceError("invalid_reference")
                source_refs = (*source_refs, original_ref)
                checked = await validate_field_original(self.session, row, metric, cutoff=cutoff)
                if original.content_hash != CMB_FY2025_SHA256 or row.report_period != date(2025, 12, 31):
                    # No silent other-report/restatement bridge or CAS inference.
                    checked, reason = False, "unsupported_original_profile"
                else:
                    accounting = "CAS"
            except ValidationError as exc:
                raise BankTopicReferenceError("invalid_reference") from exc
            if not checked:
                verification, reason = "pending_review", reason or "unverified_original"
        if visible:
            states = await peer_field_states(self.session, company_id=row.company_id, metric=metric,
                report_period=row.report_period, unit=field.unit, currency=field.currency,
                period_basis=field.period.basis, accounting_scope="group_consolidated", cutoff=cutoff)
            state = next((item.state for item in states.versions if item.source_ref == ref), None)
            if state in {"pending_recheck", "conflict", "corrected"}:
                verification = "stale" if state == "corrected" else "pending_review" if state == "pending_recheck" else "conflict"
                reason = "source_corrected" if state == "corrected" else "source_conflict"
        else:
            reason = materialized.reason or "not_visible_as_of"
        observation = FactObservation(owner_id, case_id, listing_id, binding.stable_fact_id, binding.metric,
            field.value if visible else None, field.unit, field.raw_value, field.raw_unit,
            period, None, scope, accounting, "CNY", verification, reason or field.missing_reason,
            source_refs, "peer_field", checked and visible)
        return replace(observation, source_period_basis=field.period.basis)

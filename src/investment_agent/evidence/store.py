"""Owner-scoped immutable storage. Caller commits the outer transaction."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord, ToolResult
from investment_agent.evidence.events import event_material
from investment_agent.evidence.snapshots import digest, materialize
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError, InvestorService, storage_conflicts
from investment_agent.storage.models.earnings_events import (
    BusinessSegmentRecord,
    DisclosureScheduleRecord,
    EarningsForecastRecord,
)
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.models.reference import Listing


class EvidenceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.receipts = InvestorService(session)

    async def get_evidence(
        self,
        user_id: OwnerId,
        ref: ObjectRef,
        as_of: datetime,
        *,
        subject_ref: ObjectRef | None = None,
        max_age: timedelta | None = None,
        visible_at: datetime | None = None,
    ) -> ToolResult[EvidenceRecord]:
        as_of = utc_timestamp(as_of)
        if ref.kind != "evidence" or ref.field_path not in {None, "value", "text"}:
            raise InvestorError("invalid_reference", "Evidence reference or field path is invalid")
        row = await self.session.get(SavedEvidence, (str(ref.id), ref.revision))
        record = (
            EvidenceRecord.model_validate(row.payload) if row and row.owner_id == user_id else None
        )
        if (
            record is None
            or max(record.available_from, record.published_at, record.acquired_at) > as_of
            or (visible_at is not None and record.created_at > utc_timestamp(visible_at))
        ):
            return ToolResult(
                status="missing", data=None, refs=(), as_of=as_of, coverage="not visible"
            )
        if subject_ref is not None and record.subject_ref != subject_ref:
            raise InvestorError("invalid_reference", "Evidence belongs to another subject")
        status: Literal["ok", "missing", "conflict", "stale"] = "ok"
        warnings: tuple[str, ...] = ()
        if record.conflict == "unresolved":
            status = "conflict"
        elif record.value is not None and record.value.value is None:
            status = "missing"
        elif max_age is not None and as_of - record.available_from > max_age:
            status = "stale"
        if record.verification != "verified":
            warnings = (f"verification={record.verification}",)
        if record.document_provenance is not None:
            from investment_agent.evidence.documents import document_limits

            current_limits = await document_limits(
                self.session, record, as_of, visible_at=visible_at
            )
            if current_limits:
                status = "conflict"
                warnings = (*warnings, *current_limits)
        return ToolResult[EvidenceRecord](
            status=status,
            data=record,
            refs=(ref,),
            as_of=as_of,
            coverage="saved immutable material; strict acquired/public visibility",
            warnings=warnings,
        )

    async def get_calculation(
        self,
        user_id: OwnerId,
        ref: ObjectRef,
        as_of: datetime,
        *,
        visible_at: datetime | None = None,
    ) -> ToolResult[CalculationRecord]:
        as_of = utc_timestamp(as_of)
        if ref.kind != "calculation":
            raise InvestorError("invalid_reference", "Calculation reference required")
        row = await self.session.get(SavedCalculation, (str(ref.id), ref.revision))
        record = (
            CalculationRecord.model_validate(row.payload)
            if row and row.owner_id == user_id
            else None
        )
        if (
            record is None
            or record.as_of > as_of
            or (visible_at is not None and record.created_at > utc_timestamp(visible_at))
        ):
            return ToolResult(
                status="missing", data=None, refs=(), as_of=as_of, coverage="not visible"
            )
        if ref.field_path is not None and (
            not ref.field_path.startswith("results.") or ref.field_path[8:] not in record.results
        ):
            raise InvestorError("invalid_reference", "Calculation result path is invalid")
        status_map: dict[str, Literal["ok", "missing", "stale", "error"]] = {
            "ok": "ok",
            "missing_inputs": "missing",
            "missing_assumptions": "missing",
            "stale_inputs": "stale",
            "not_applicable": "missing",
            "outside_search_range": "missing",
            "calculation_error": "error",
        }
        return ToolResult(
            status=status_map[record.status.value],
            data=record,
            refs=(ref,),
            as_of=as_of,
            coverage="saved calculation; no algorithm execution",
        )

    @storage_conflicts
    async def save_evidence(
        self,
        user_id: OwnerId,
        record: EvidenceRecord,
        operation_id: UUID,
    ) -> ObjectRef:
        request = {"operation": "save_evidence", "record": record.model_dump(mode="json")}
        async with self.session.begin_nested():
            replay = await self.receipts._replay(user_id, operation_id, request)
            if replay is not None:
                return ObjectRef.model_validate(replay)
            if (
                record.subject_ref.kind != "listing"
                or record.subject_ref.revision != 1
                or (
                    record.subject_ref.field_path is not None
                    or await self.session.get(Listing, record.subject_ref.id) is None
                )
            ):
                raise InvestorError("invalid_reference", "A real listing subject is required")
            if record.source_kind == "snapshot" and record.source_ref.kind in {
                "financial_snapshot",
                "market_snapshot",
            }:
                model = (
                    FinancialSnapshot
                    if record.source_ref.kind == "financial_snapshot"
                    else MarketSnapshot
                    if record.source_ref.kind == "market_snapshot"
                    else None
                )
                row = await self.session.get(model, record.source_ref.id) if model else None
                if (
                    not isinstance(row, FinancialSnapshot | MarketSnapshot)
                    or row.version != record.source_ref.revision
                ):
                    raise InvestorError("invalid_reference", "Snapshot version does not exist")
                candidates = []
                from investment_agent.evidence.snapshots import UNITS

                for field in UNITS:
                    try:
                        candidate = materialize(row, field)
                    except InvestorError:
                        continue
                    if candidate.source_ref == record.source_ref:
                        candidates.append(candidate)
                if len(candidates) != 1:
                    raise InvestorError("invalid_reference", "Snapshot field is not supported")
                expected = candidates[0]
                for name in (
                    "subject_ref",
                    "period",
                    "source_ref",
                    "locator",
                    "value",
                    "text",
                    "published_at",
                    "acquired_at",
                    "available_from",
                    "content_hash",
                    "verification",
                    "conflict",
                ):
                    if getattr(expected, name) != getattr(record, name):
                        raise InvestorError(
                            "invalid_reference", f"Snapshot {name} does not match saved source"
                        )
                actor = "snapshot_adapter"
            elif (
                record.source_kind == "snapshot"
                and record.source_ref.kind == "peer_annual_snapshot"
            ):
                # Trusted peer-annual-field branch (WP4): the record is never
                # trusted - every fact is re-derived from the stored immutable
                # snapshot row and its per-field proof before it is accepted.
                # Verified can only come out of this re-derivation, so a model
                # or client upload claiming a peer source and verified is
                # refused here, never relaxed.
                from investment_agent.industry_peers.evidence import (
                    ADAPTER_ACTOR,
                    peer_field_evidence,
                )
                from investment_agent.storage.models.peer import PeerAnnualSnapshot

                field_path = record.source_ref.field_path
                if (
                    record.source_ref.revision != 1
                    or field_path is None
                    or not field_path.startswith("fields.")
                    or len(field_path) <= len("fields.")
                ):
                    raise InvestorError("invalid_reference", "Snapshot field is not supported")
                metric = field_path.removeprefix("fields.")
                snapshot = await self.session.get(PeerAnnualSnapshot, str(record.source_ref.id))
                if snapshot is None:
                    raise InvestorError("invalid_reference", "Snapshot version does not exist")
                if metric not in (snapshot.fields or {}):
                    raise InvestorError("invalid_reference", "Snapshot field is not supported")
                materialization = peer_field_evidence(snapshot, metric)
                peer_expected = materialization.record
                if peer_expected is not None and peer_expected.verification == "verified":
                    from investment_agent.industry_peers.originals import validate_field_original
                    if not await validate_field_original(self.session, snapshot, metric):
                        raise InvestorError("invalid_reference", "Peer original proof does not match the saved original bytes")
                if peer_expected is None:
                    raise InvestorError(
                        "invalid_reference",
                        "Peer annual field is not materializable from this snapshot version "
                        f"({materialization.reason}); the fact stays in the snapshot until a "
                        "version that states its source dates exists",
                    )
                for name in (
                    "subject_ref",
                    "period",
                    "source_ref",
                    "locator",
                    "value",
                    "text",
                    "published_at",
                    "acquired_at",
                    "available_from",
                    "content_hash",
                    "verification",
                    "conflict",
                    "date_precision",
                    "document_provenance",
                ):
                    if getattr(peer_expected, name) != getattr(record, name):
                        raise InvestorError(
                            "invalid_reference",
                            f"Peer snapshot {name} does not match saved source; "
                            "re-materialize from the current snapshot version",
                        )
                actor = ADAPTER_ACTOR
            elif record.source_kind == "snapshot":
                event_models = {
                    "earnings_forecast": EarningsForecastRecord,
                    "disclosure_schedule": DisclosureScheduleRecord,
                    "business_segment": BusinessSegmentRecord,
                }
                if record.source_ref.kind in event_models:
                    event_row = await self.session.get(
                        event_models[record.source_ref.kind], record.source_ref.id
                    )
                    if not isinstance(
                        event_row,
                        EarningsForecastRecord | DisclosureScheduleRecord | BusinessSegmentRecord,
                    ):
                        raise InvestorError("invalid_reference", "Event source does not exist")
                    expected_event = event_material(
                        event_row, record.source_ref.field_path or "text"
                    )
                    for name in (
                        "source_ref",
                        "subject_ref",
                        "period",
                        "locator",
                        "value",
                        "text",
                        "content_hash",
                        "published_at",
                        "acquired_at",
                        "available_from",
                        "verification",
                        "conflict",
                    ):
                        if getattr(expected_event, name) != getattr(record, name):
                            raise InvestorError(
                                "invalid_reference", f"Event {name} does not match saved source"
                            )
                    record = record.model_copy(update={"actor": "event_adapter"})
                else:
                    raise InvestorError("invalid_reference", "Unsupported saved event source")
                actor = "event_adapter"
            elif record.source_kind == "research_dataset":
                from investment_agent.materials.evidence import material_evidence
                from investment_agent.storage.models.research_material import ResearchDataset

                if record.source_ref.kind != "research_dataset":
                    raise InvestorError(
                        "invalid_reference", "Research dataset source reference is required"
                    )
                material_row = await self.session.get(ResearchDataset, record.source_ref.id)
                if material_row is None or material_row.revision != record.source_ref.revision:
                    raise InvestorError(
                        "invalid_reference", "Research dataset version does not exist"
                    )
                # Never trust the submitted record: rebuild every fact of that
                # version from the stored payload and require an exact match.
                try:
                    materialized, _skipped = material_evidence(
                        material_row,
                        subject_ref=record.subject_ref,
                        created_at=record.created_at,
                    )
                except ValueError as exc:
                    raise InvestorError(
                        "invalid_reference", f"Research dataset cannot be materialized: {exc}"
                    ) from exc
                candidates = [
                    candidate
                    for candidate in materialized
                    if candidate.content_hash == record.content_hash
                ]
                if len(candidates) != 1:
                    raise InvestorError(
                        "invalid_reference",
                        "Research dataset fact does not match the saved source",
                    )
                expected_material = candidates[0]
                for name in (
                    "subject_ref",
                    "period",
                    "source_ref",
                    "locator",
                    "value",
                    "text",
                    "published_at",
                    "acquired_at",
                    "available_from",
                    "content_hash",
                    "verification",
                    "conflict",
                    "date_precision",
                    "document_provenance",
                ):
                    if getattr(expected_material, name) != getattr(record, name):
                        raise InvestorError(
                            "invalid_reference",
                            f"Research dataset {name} does not match saved source",
                        )
                actor = "material_adapter"
            elif record.source_kind == "user_report":
                if record.source_ref != record.subject_ref or record.verification != "user_report":
                    raise InvestorError(
                        "invalid_reference", "User report source must be its listing subject"
                    )
                now = datetime.now(UTC)
                record = record.model_copy(
                    update={"acquired_at": now, "available_from": now, "published_at": now}
                )
                record = record.model_copy(
                    update={
                        "content_hash": digest(
                            {
                                "subject": record.subject_ref.model_dump(mode="json"),
                                "period": record.period.model_dump(mode="json")
                                if record.period
                                else None,
                                "value": record.value.model_dump(mode="json")
                                if record.value
                                else None,
                                "text": record.text,
                            }
                        )
                    }
                )
                actor = "user"
            else:
                raise InvestorError("invalid_reference", "Document ingestion is not enabled")
            for correction in record.correction_refs:
                prior = await self.get_evidence(
                    user_id, correction, record.acquired_at, subject_ref=record.subject_ref
                )
                if prior.data is None or prior.data.period != record.period:
                    raise InvestorError(
                        "invalid_reference",
                        "Correction must resolve to the same subject and period",
                    )
            saved = record.model_copy(
                update={
                    "id": uuid4(),
                    "revision": 1,
                    "created_at": datetime.now(UTC),
                    "actor": actor,
                }
            )
            if saved.source_kind == "user_report":
                saved = saved.model_copy(
                    update={
                        "source_ref": ObjectRef(
                            kind="evidence",
                            id=saved.id,
                            revision=1,
                            field_path="text" if saved.text is not None else "value",
                        ),
                        "locator": "saved_user_submission.text"
                        if saved.text is not None
                        else "saved_user_submission.value",
                    }
                )
            self.session.add(
                SavedEvidence(
                    id=str(saved.id),
                    revision=1,
                    owner_id=user_id,
                    payload=saved.model_dump(mode="json"),
                )
            )
            result = ObjectRef(kind="evidence", id=saved.id, revision=1)
            self.receipts._receipt(user_id, operation_id, request, result)
            await self.session.flush()
        return result

    @storage_conflicts
    async def save_calculation(
        self,
        user_id: OwnerId,
        record: CalculationRecord,
        operation_id: UUID,
    ) -> ObjectRef:
        request = {"operation": "save_calculation", "record": record.model_dump(mode="json")}
        async with self.session.begin_nested():
            replay = await self.receipts._replay(user_id, operation_id, request)
            if replay is not None:
                return ObjectRef.model_validate(replay)
            for ref in record.fact_refs + record.assumption_refs:
                resolved = await self.get_evidence(user_id, ref, record.as_of)
                if resolved.data is None:
                    raise InvestorError("invalid_reference", "Calculation input is not visible")
                if ref in record.fact_refs and (
                    resolved.status != "ok" or resolved.data.verification == "user_report"
                ):
                    raise InvestorError(
                        "invalid_reference", "Calculation facts must be usable source evidence"
                    )
            # A selected source reference cannot hide another known conflicting source.
            from investment_agent.domain.a_share import format_listing_ticker
            from investment_agent.evidence.queries import SnapshotQueryService
            from investment_agent.factors.view import PROVIDER_FIELD_RENAMES

            for fact in record.fact_refs:
                material = (await self.get_evidence(user_id, fact, record.as_of)).data
                assert material is not None
                source = material.source_ref
                if source.kind not in {"financial_snapshot", "market_snapshot"}:
                    continue
                listing = await self.session.get(Listing, material.subject_ref.id)
                assert listing is not None and material.period is not None
                field = source.field_path or ""
                if source.kind == "financial_snapshot":
                    snapshot = await self.session.get(FinancialSnapshot, source.id)
                    assert snapshot is not None
                    stored = field.removeprefix("fields.")
                    field = PROVIDER_FIELD_RENAMES[snapshot.provider].get(stored, stored)
                query = await SnapshotQueryService(self.session).get_snapshot(
                    format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code),
                    field,
                    record.as_of,
                    report_period=material.period.end,
                )
                if query.status == "conflict":
                    raise InvestorError(
                        "invalid_reference",
                        "Calculation input has unresolved cross-source conflict",
                    )
            for assumption in record.assumptions:
                if assumption.source_ref is not None:
                    resolved = await self.get_evidence(user_id, assumption.source_ref, record.as_of)
                    if resolved.data is None or (
                        assumption.provenance == "user_scenario"
                        and resolved.data.verification != "user_report"
                    ):
                        raise InvestorError(
                            "invalid_reference", "Scenario source is not an appropriate saved input"
                        )
                elif assumption.provenance == "user_scenario":
                    raise InvestorError(
                        "invalid_reference", "User scenario requires its saved user submission"
                    )
            saved = record.model_copy(
                update={
                    "id": uuid4(),
                    "revision": 1,
                    "created_at": datetime.now(UTC),
                    "actor": "calculation_service",
                }
            )
            self.session.add(
                SavedCalculation(
                    id=str(saved.id),
                    revision=1,
                    owner_id=user_id,
                    payload=saved.model_dump(mode="json"),
                )
            )
            result = ObjectRef(kind="calculation", id=saved.id, revision=1)
            self.receipts._receipt(user_id, operation_id, request, result)
            await self.session.flush()
        return result

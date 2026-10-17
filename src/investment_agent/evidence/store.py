"""Owner-scoped immutable storage. Caller commits the outer transaction.

``save_evidence`` is a receipt/dispatch shell: the prologue replays the
operation receipt and requires a real listing subject, one adapter per source
re-derives and verifies the submitted record, and the epilogue writes the
savepoint, the saved row and the receipt exactly once.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.adapters import material, snapshot, user_report
from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord, ToolResult
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError, InvestorService, storage_conflicts
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.reference import Listing

type MaterializeExpected = Callable[
    [AsyncSession, EvidenceRecord], Awaitable[tuple[EvidenceRecord, str]]
]

_SOURCE_ADAPTERS: dict[str, MaterializeExpected] = {
    "snapshot": snapshot.materialize_expected,
    "research_dataset": material.materialize_expected,
    "user_report": user_report.materialize_expected,
}
"""One adapter per evidence source kind; anything absent is refused below."""


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
            adapter = _SOURCE_ADAPTERS.get(record.source_kind)
            if adapter is None:
                raise InvestorError("invalid_reference", "Document ingestion is not enabled")
            record, actor = await adapter(self.session, record)
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
            from investment_agent.storage.repositories.supplemental_updates import (
                SupplementalUpdatesRepository,
                supplemental_updates_journal_available,
                update_digest,
            )

            if await supplemental_updates_journal_available(self.session):
                await SupplementalUpdatesRepository(self.session).append_change(
                user_id,
                change_key=f"evidence:{saved.id}:revision:{saved.revision}",
                kind="evidence_revision",
                operation_id=operation_id,
                request_hash=update_digest(request),
                acquired_at=saved.acquired_at,
                available_from=saved.available_from,
                committed_at=saved.created_at,
                payload={
                    "schema_version": 1,
                    "source_ref": result.model_dump(mode="json"),
                    "subject_ref": saved.subject_ref.model_dump(mode="json"),
                    "source_kind": saved.source_kind,
                    "content_hash": saved.content_hash,
                    "verification": saved.verification,
                },
                )
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
            from investment_agent.factors.field_resolution import PROVIDER_FIELD_RENAMES

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

"""User-reported event creation and append-only revision service."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.admission import (
    reserve_and_replay,
    save_receipt,
)
from investment_agent.supplemental_rules.commands import (
    EventMutationResponse,
    ResearchEventCreateRequest,
    ResearchEventRevisionRequest,
)
from investment_agent.supplemental_rules.contracts import MutationReceipt, ResearchEvent


class SupplementalEventService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        owner_id: OwnerId = "local_owner",
    ) -> None:
        self.session = session
        self.settings = settings
        self.owner_id = owner_id
        self.repo = SupplementalRulesRepository(session)

    async def create(self, request: ResearchEventCreateRequest) -> EventMutationResponse:
        operation_id = request.idempotency_key
        event_id = operation_id
        target = ObjectRef(kind="research_event", id=event_id, revision=1)
        command = "research_event.create_user_report.v1"
        request_hash, prior = await reserve_and_replay(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=None,
            body=request,
        )
        if prior is not None:
            return _saved_event_response(prior)
        from investment_agent.supplemental_rules.admission import require_enabled

        await require_enabled(self.session, self.settings)
        if await self.repo.get_event_row(self.owner_id, target) is not None:
            raise InvestorError("revision_conflict", "事件UUID已经存在")
        now = datetime.now(UTC)
        event = request.event.as_user_report(
            event_id=event_id,
            revision=1,
            owner_id=self.owner_id,
            acquired_at=now,
            available_from=now,
        )
        event = await self.repo.save_research_event(self.owner_id, event, created_at=now)
        await self.repo.advance_registry(self.owner_id, events_delta=1, updated_at=now)
        run_ref, source_manifest_hash = await self._enqueue_reassessment(
            event,
            operation_id=operation_id,
            command=command,
            request=request,
            target=None,
            request_hash=request_hash,
            accepted_at=now,
        )
        receipt = MutationReceipt(
            owner_id=self.owner_id,
            operation_id=operation_id,
            result_kind="event_mutation",
            command=command,
            target_ref=None,
            accepted_at=now,
            result_ref=target,
            run_ref=run_ref,
            source_manifest_hash=source_manifest_hash,
        )
        response = EventMutationResponse(
            receipt=receipt,
            event_ref=target,
            run_ref=run_ref,
        )
        save_receipt(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            request_hash=request_hash,
            receipt=receipt,
            result={"event_mutation": response.model_dump(mode="json")},
        )
        await self.session.flush()
        return response

    async def revise(
        self, event_id: UUID, request: ResearchEventRevisionRequest
    ) -> EventMutationResponse:
        operation_id = request.idempotency_key
        command_target = ObjectRef(kind="research_event", id=event_id, revision=1)
        command = "research_event.revise_user_report.v1"
        request_hash, prior = await reserve_and_replay(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=command_target,
            body=request,
        )
        if prior is not None:
            return _saved_event_response(prior)
        from investment_agent.supplemental_rules.admission import require_enabled

        await require_enabled(self.session, self.settings)
        if (
            request.previous_ref.id != event_id
            or request.previous_ref.revision != request.expected_event_revision
        ):
            raise InvestorError("invalid_reference", "previous_ref与expected_event_revision不匹配")
        previous = await self.repo.get_event_revision(self.owner_id, request.previous_ref)
        if previous is None:
            raise InvestorError("not_found", "事件前一版本不存在")
        next_ref = ObjectRef(
            kind="research_event",
            id=event_id,
            revision=request.expected_event_revision + 1,
        )
        if await self.repo.get_event_row(self.owner_id, next_ref) is not None:
            raise InvestorError("revision_conflict", "事件已被另一修订更新")
        now = datetime.now(UTC)
        event = request.event.as_user_report(
            event_id=event_id,
            revision=request.expected_event_revision + 1,
            owner_id=self.owner_id,
            acquired_at=now,
            available_from=now,
            previous_ref=request.previous_ref,
        )
        event = await self.repo.save_research_event(self.owner_id, event, created_at=now)
        await self.repo.advance_registry(self.owner_id, events_delta=1, updated_at=now)
        run_ref, source_manifest_hash = await self._enqueue_reassessment(
            event,
            operation_id=operation_id,
            command=command,
            request=request,
            target=command_target,
            request_hash=request_hash,
            accepted_at=now,
        )
        receipt = MutationReceipt(
            owner_id=self.owner_id,
            operation_id=operation_id,
            result_kind="event_mutation",
            command=command,
            target_ref=command_target,
            accepted_at=now,
            result_ref=next_ref,
            run_ref=run_ref,
            source_manifest_hash=source_manifest_hash,
        )
        response = EventMutationResponse(
            receipt=receipt,
            event_ref=next_ref,
            run_ref=run_ref,
        )
        save_receipt(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            request_hash=request_hash,
            receipt=receipt,
            result={"event_mutation": response.model_dump(mode="json")},
        )
        await self.session.flush()
        return response

    async def _enqueue_reassessment(
        self,
        event: ResearchEvent,
        *,
        operation_id: UUID,
        command: str,
        request: ResearchEventCreateRequest | ResearchEventRevisionRequest,
        target: ObjectRef | None,
        request_hash: str,
        accepted_at: datetime,
    ) -> tuple[ObjectRef, str]:
        from investment_agent.supplemental_rules.admission import enqueue_manifest

        run_ref, source_manifest_hash = await enqueue_manifest(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=target,
            command_body=request,
            request_hash=request_hash,
            source=event,
            accepted_at=accepted_at,
            trigger="supplemental_rules_event",
            origin_ref=ObjectRef(kind="research_event", id=event.id, revision=event.revision),
        )
        # Keep the user's committed event revision and its already-admitted P2
        # run linked to one durable P4 journal entry in this transaction.
        from investment_agent.storage.repositories.supplemental_updates import (
            SupplementalUpdatesRepository,
            supplemental_updates_journal_available,
        )

        if not await supplemental_updates_journal_available(self.session):
            return run_ref, source_manifest_hash
        registry = await self.repo.get_registry_head(self.owner_id)
        await SupplementalUpdatesRepository(self.session).append_change(
            self.owner_id,
            change_key=f"p2:event:{event.id}:revision:{event.revision}:operation:{operation_id}",
            kind="research_event",
            operation_id=operation_id,
            request_hash=request_hash,
            origin_run_id=int(run_ref.id),
            source_manifest_hash=source_manifest_hash,
            acquired_at=event.acquired_at,
            available_from=event.available_from,
            committed_at=accepted_at,
            payload={
                "schema_version": 1,
                "source_ref": {
                    "kind": "research_event",
                    "id": str(event.id),
                    "revision": event.revision,
                },
                "change_type": "revision",
                "event_key": event.event_key,
                "previous_ref": event.previous_ref.model_dump(mode="json")
                if event.previous_ref
                else None,
                "content_hash": event.content_hash,
                "events_generation": registry.events_generation,
                "request_hash": request_hash,
            },
        )
        return run_ref, source_manifest_hash


def _saved_event_response(envelope: dict[str, object]) -> EventMutationResponse:
    try:
        receipt = MutationReceipt.model_validate(envelope["receipt"])
        if receipt.result_kind != "event_mutation":
            raise ValueError("receipt discriminator mismatch")
        result = envelope["result"]
        if not isinstance(result, dict):
            raise ValueError("receipt result must be a mapping")
        return EventMutationResponse.model_validate(result["event_mutation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvestorError("idempotency_conflict", "事件收据类型不匹配") from exc

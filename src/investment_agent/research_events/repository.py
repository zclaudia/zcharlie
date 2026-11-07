"""Same-session append with immutable identity, comparison and bounded savepoint.

No method commits, rolls back the caller's transaction or opens another session.
Every new event is resolved from real source rows before it can be appended.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import Field, StrictBool, ValidationError, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import Contract, OwnerId, Revision, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.research_events import canonical
from investment_agent.research_events.contracts import (
    EVENT_CONTRACTS,
    SOURCE_REF_ADAPTER,
    EventEnvelope,
    EventSourceRef,
    Hash,
    InboxRebuildState,
    NonnegativeInt,
    ReceiptState,
    RoutingPayload,
    StoredEvent,
)
from investment_agent.research_events.registry import EventRegistry, get_default_registry
from investment_agent.research_events.resolvers import event_subject, resolve_source
from investment_agent.storage.models.research_events import (
    ConsumerReceipt,
    ProjectionCursor,
    ResearchDomainEvent,
)


def _now() -> datetime:
    return datetime.now(UTC)


def stored_event(row: ResearchDomainEvent) -> StoredEvent:
    def at(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else utc_timestamp(value)

    return StoredEvent.model_validate({
        "sequence": row.sequence, "event_id": row.event_id, "event_type": row.event_type,
        "owner_id": row.owner_id, "source_ref": row.source_ref, "source_revision": row.source_revision,
        "occurred_at": at(row.occurred_at), "recorded_at": at(row.recorded_at),
        "schema_version": row.schema_version, "payload_hash": row.payload_hash, "payload": row.payload,
    })


class EventRepository:
    def __init__(self, session: AsyncSession, *, registry: EventRegistry | None = None,
                 clock: Callable[[], datetime] = _now) -> None:
        self.session = session
        self.registry = registry if registry is not None else get_default_registry()
        self.clock = clock

    async def _reserve_sqlite_outer(self) -> None:
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            active = await connection.run_sync(lambda conn: bool(getattr(conn.connection.driver_connection, "in_transaction", False)))
            if not active:
                # Ensures savepoint RELEASE cannot independently commit even
                # when the caller uses an engine without our SQLite hook.
                # It also serializes source validation and duplicate writers.
                await connection.exec_driver_sql("BEGIN IMMEDIATE")

    async def _existing(self, owner: OwnerId, event_type: str, identity: str,
                        revision: int) -> ResearchDomainEvent | None:
        result = await self.session.scalar(select(ResearchDomainEvent).where(
            ResearchDomainEvent.owner_id == owner, ResearchDomainEvent.event_type == event_type,
            ResearchDomainEvent.source_identity == identity, ResearchDomainEvent.source_revision == revision,
        ).execution_options(populate_existing=True))
        return result

    async def append_event(self, *, owner_id: OwnerId, event_type: str,
                           source_ref: EventSourceRef | dict[str, Any], source_revision: int,
                           occurred_at: datetime, payload: RoutingPayload | dict[str, Any]) -> StoredEvent:
        if owner_id != "local_owner":
            raise InvestorError("not_found", "Source is not accessible")
        if event_type not in EVENT_CONTRACTS:
            raise InvestorError("invalid_reference", "Unknown event type")
        try:
            source = SOURCE_REF_ADAPTER.validate_python(source_ref)
            identity = canonical.source_identity(source)
            identifier = canonical.event_id(owner_id, event_type, source, source_revision)
            payload_type = EVENT_CONTRACTS[event_type][1]
            if isinstance(payload, RoutingPayload) and type(payload) is not payload_type:
                raise ValueError("payload class does not match event type")
            parsed = payload_type.model_validate(payload)
            digest = canonical.payload_hash(parsed)
            occurred = utc_timestamp(occurred_at)
            self.registry.source(source.kind, event_type)
        except (ValueError, ValidationError) as exc:
            raise InvestorError("invalid_reference", "Invalid event routing contract") from exc
        try:
            await self._reserve_sqlite_outer()
            await self.session.flush()
            existing = await self._existing(owner_id, event_type, identity, source_revision)
            # Replays still re-check persistent access/source. Validate the
            # original immutable payload before comparing the new hash, so a
            # changed request yields source_conflict rather than laundering
            # changed routing fields through today's source state.
            original = stored_event(existing) if existing is not None else None
            resolved = await resolve_source(
                self.session, owner_id=owner_id, event_type=event_type, source_ref=source,
                source_revision=source_revision,
                payload=original.payload if original is not None else parsed, registry=self.registry,
            )
            expected_payload = original.payload if original is not None else parsed
            expected_time = original.occurred_at if original is not None else occurred
            if (resolved.owner_id != owner_id or resolved.source_ref != source
                    or resolved.source_revision != source_revision or resolved.subject != event_subject(expected_payload)
                    or resolved.occurred_at != expected_time):
                raise InvestorError("invalid_reference", "Source ownership, subject, version or occurrence time disagrees")
            if original is not None:
                if original.payload_hash != digest:
                    raise InvestorError("source_conflict", "The source version already has a different event payload")
                return original
            envelope = EventEnvelope.model_validate({
                "event_id": identifier, "event_type": event_type, "owner_id": owner_id,
                "source_ref": source, "source_revision": source_revision, "occurred_at": occurred,
                "recorded_at": utc_timestamp(self.clock()), "schema_version": 1,
                "payload_hash": digest, "payload": parsed,
            })
            row = ResearchDomainEvent(
                event_id=str(envelope.event_id), owner_id=owner_id, event_type=event_type,
                source_identity=identity, source_ref=cast(dict[str, Any], canonical.normalize(source)),
                source_revision=source_revision, occurred_at=envelope.occurred_at,
                recorded_at=envelope.recorded_at, schema_version=1, payload_hash=digest,
                payload=cast(dict[str, Any], canonical.normalize(parsed)),
            )
            try:
                async with self.session.begin_nested():
                    self.session.add(row)
                    await self.session.flush([row])
            except IntegrityError:
                raced = await self._existing(owner_id, event_type, identity, source_revision)
                if raced is None:
                    raise
                original = stored_event(raced)
                if original.payload_hash != digest:
                    raise InvestorError("source_conflict", "The source version already has a different event payload") from None
                return original
            return stored_event(row)
        except ValidationError as exc:
            raise InvestorError("invalid_reference", "Persisted source or event contract is invalid") from exc
        except OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            raise InvestorError("revision_conflict", "Concurrent SQLite write; retry the original transaction") from exc


async def append_event(session: AsyncSession, *, owner_id: OwnerId, event_type: str,
                       source_ref: EventSourceRef | dict[str, Any], source_revision: int,
                       occurred_at: datetime, payload: RoutingPayload | dict[str, Any]) -> StoredEvent:
    return await EventRepository(session).append_event(
        owner_id=owner_id, event_type=event_type, source_ref=source_ref,
        source_revision=source_revision, occurred_at=occurred_at, payload=payload,
    )


class EffectManifest(Contract):
    """IT supplies targets; EV only freezes and advances this bounded manifest."""

    generation: UUID
    targets: Annotated[tuple[UUID, ...], Field(max_length=10_000)]
    source_ref: EventSourceRef
    source_revision: Revision
    source_payload_hash: Hash

    @model_validator(mode="after")
    def stable_targets(self) -> EffectManifest:
        if self.source_revision != self.source_ref.revision:
            raise ValueError("manifest source revisions disagree")
        if self.targets != tuple(sorted(set(self.targets), key=str)):
            raise ValueError("manifest targets must be unique and sorted by entry ID")
        return self


class ProjectionOutcome(Contract):
    state: Literal["applied", "ignored", "retry"]
    reason_code: Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-z0-9_.:-]+$")] | None = None
    effect_refs: Annotated[tuple[dict[str, Any], ...], Field(max_length=100)] = ()
    effect_manifest: EffectManifest | None = None
    effect_cursor: NonnegativeInt | None = None

    @model_validator(mode="after")
    def explicit_outcome(self) -> ProjectionOutcome:
        if self.state != "applied" and self.reason_code is None:
            raise ValueError("ignored/pending effects require an explicit reason")
        if self.effect_manifest is None and self.effect_cursor is not None:
            raise ValueError("progress requires a frozen manifest")
        if self.state == "retry" and self.effect_manifest is None:
            raise ValueError("retry outcomes represent pending effects; failures raise exceptions")
        canonical.canonical_bytes(self.effect_refs)
        return self


class ReceiptRecord(Contract):
    consumer: str
    event_id: UUID
    state: ReceiptState
    ordering_key: str
    effect_manifest: EffectManifest | None
    effect_manifest_hash: Hash | None
    effect_cursor: NonnegativeInt | None
    attempts: NonnegativeInt
    next_retry_at: datetime | None
    last_error_code: str | None
    error_summary: str | None
    processed_at: datetime | None
    projector_version: str
    effect_refs: tuple[dict[str, Any], ...]

    @model_validator(mode="after")
    def manifest_integrity(self) -> ReceiptRecord:
        if self.effect_manifest is None:
            if self.effect_manifest_hash is not None or self.effect_cursor is not None:
                raise ValueError("manifest progress fields must be present together")
        elif (self.effect_manifest_hash != canonical.payload_hash(self.effect_manifest)
              or self.effect_cursor is None or self.effect_cursor > len(self.effect_manifest.targets)
              or (self.state in {"applied", "ignored"} and self.effect_cursor != len(self.effect_manifest.targets))):
            raise ValueError("receipt manifest hash/progress is inconsistent")
        return self


class CursorRecord(Contract):
    consumer: str
    scanned_sequence: NonnegativeInt
    revision: Revision
    updated_at: datetime
    last_success_at: datetime | None
    retry_count: NonnegativeInt
    quarantined_count: NonnegativeInt
    active_generation: UUID | None
    rebuild_state: InboxRebuildState | None


class CursorUpdate(Contract):
    scanned_sequence: NonnegativeInt | None = None
    active_generation: UUID | None = None
    rebuild_state: InboxRebuildState | None = None
    mark_success: StrictBool = False


class ProjectionCASConflictError(InvestorError):
    def __init__(self) -> None:
        super().__init__("revision_conflict", "Projection cursor changed; retry the original transaction")


def _at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return utc_timestamp(value.replace(tzinfo=UTC) if value.tzinfo is None else value)


def _cursor_record(row: ProjectionCursor) -> CursorRecord:
    return CursorRecord.model_validate({name: _at(value) if isinstance(value, datetime) else value
                                       for name in CursorRecord.model_fields
                                       for value in (getattr(row, name),)})


def _receipt_record(row: ConsumerReceipt) -> ReceiptRecord:
    return ReceiptRecord.model_validate({name: _at(value) if isinstance(value, datetime) else value
                                        for name in ReceiptRecord.model_fields
                                        for value in (getattr(row, name),)})


class DeliveryRepository(EventRepository):
    """Caller commits once per event/effect slice; all CAS failures propagate.

    apply_projection and record_failure isolate their own changes in savepoints.
    Standalone cursor/progress calls must be enclosed in the caller's unit of
    work; a rejected CAS is never permission to commit the preceding projection.
    """

    async def event_row(self, event_id: UUID) -> ResearchDomainEvent:
        row = await self.session.scalar(select(ResearchDomainEvent).where(
            ResearchDomainEvent.event_id == str(event_id)).execution_options(populate_existing=True))
        if row is None:
            raise InvestorError("not_found", "Domain event does not exist")
        return row

    async def cursor(self, consumer: str) -> CursorRecord:
        self.registry.consumer(consumer)
        await self._reserve_sqlite_outer()
        row = await self.session.get(ProjectionCursor, consumer, populate_existing=True)
        if row is None:
            async with self.session.begin_nested():
                row = ProjectionCursor(consumer=consumer, updated_at=utc_timestamp(self.clock()))
                self.session.add(row)
                await self.session.flush([row])
        return _cursor_record(row)

    async def receipt(self, consumer: str, event_id: UUID) -> ReceiptRecord | None:
        self.registry.consumer(consumer)
        row = await self.session.get(ConsumerReceipt, (consumer, str(event_id)), populate_existing=True)
        return _receipt_record(row) if row is not None else None

    async def _ensure_receipt(self, consumer: str, event_id: UUID, ordering_key: str,
                              projector_version: str) -> ConsumerReceipt:
        self.registry.consumer(consumer)
        if not ordering_key or len(ordering_key) > 250 or not projector_version or len(projector_version) > 80:
            raise InvestorError("invalid_reference", "Invalid delivery identity")
        await self.event_row(event_id)  # FK checks are disabled in SQLite.
        row = await self.session.get(ConsumerReceipt, (consumer, str(event_id)), populate_existing=True)
        if row is None:
            row = ConsumerReceipt(consumer=consumer, event_id=str(event_id), state="retry",
                                  ordering_key=ordering_key, projector_version=projector_version,
                                  effect_refs=[], attempts=0)
            self.session.add(row)
            await self.session.flush([row])
        elif row.ordering_key != ordering_key or row.projector_version != projector_version:
            raise InvestorError("source_conflict", "Frozen delivery identity changed")
        return row

    async def predecessor(self, consumer: str, event: ResearchDomainEvent,
                           ordering_key: str) -> UUID | None:
        self.registry.consumer(consumer)
        # A failed ordering resolver is an explicit conservative barrier. Until
        # repaired we cannot prove another source belongs to an independent chain.
        query = (select(ConsumerReceipt.event_id).join(ResearchDomainEvent,
                 ResearchDomainEvent.event_id == ConsumerReceipt.event_id).where(
            ConsumerReceipt.consumer == consumer,
            ResearchDomainEvent.owner_id == event.owner_id,
            ResearchDomainEvent.sequence < event.sequence,
            ConsumerReceipt.state.in_(("retry", "blocked_predecessor", "quarantined")),
            or_(ConsumerReceipt.ordering_key == ordering_key,
                ConsumerReceipt.last_error_code == "ordering_key_unavailable"),
        ).order_by(ResearchDomainEvent.sequence).limit(1))
        value = await self.session.scalar(query)
        return UUID(value) if value is not None else None

    async def compare_and_set_cursor(self, consumer: str, expected_revision: int,
                                     changes: CursorUpdate) -> CursorRecord:
        current = await self.cursor(consumer)
        if type(expected_revision) is not int or current.revision != expected_revision:
            raise ProjectionCASConflictError()
        values: dict[str, Any] = {"revision": expected_revision + 1, "updated_at": utc_timestamp(self.clock())}
        if changes.scanned_sequence is not None:
            sequence = changes.scanned_sequence
            if sequence < current.scanned_sequence:
                raise InvestorError("invalid_cursor", "Scan cursor cannot move backwards")
            # Advancing the scan requires a receipt for every actual row crossed.
            missing = await self.session.scalar(select(ResearchDomainEvent.sequence).outerjoin(
                ConsumerReceipt, and_(ConsumerReceipt.consumer == consumer,
                                      ConsumerReceipt.event_id == ResearchDomainEvent.event_id)).where(
                ResearchDomainEvent.sequence > current.scanned_sequence,
                ResearchDomainEvent.sequence <= sequence, ConsumerReceipt.event_id.is_(None)).limit(1))
            maximum = await self.session.scalar(select(func.max(ResearchDomainEvent.sequence))) or 0
            if missing is not None or sequence > maximum:
                raise InvestorError("invalid_cursor", "Scan cursor cannot skip unrecorded deliveries")
            values["scanned_sequence"] = sequence
        generation_changed = False
        if "active_generation" in changes.model_fields_set:
            if consumer != "research_inbox.v1" and changes.active_generation is not None:
                raise InvestorError("invalid_action", "Only the inbox has a projection generation")
            if current.active_generation is not None and changes.active_generation is None:
                raise InvestorError("invalid_action", "An initialized projection head cannot be cleared")
            generation_changed = changes.active_generation != current.active_generation
            if generation_changed:
                inflight = await self.session.scalar(select(ConsumerReceipt.event_id).where(
                    ConsumerReceipt.consumer == consumer, ConsumerReceipt.effect_manifest.is_not(None),
                    ConsumerReceipt.state.not_in(("applied", "ignored"))).limit(1))
                if inflight is not None:
                    raise InvestorError("revision_conflict", "Unfinished effect manifests block generation switching")
            values["active_generation"] = str(changes.active_generation) if changes.active_generation else None
        if "rebuild_state" in changes.model_fields_set:
            state = changes.rebuild_state
            active = changes.active_generation if "active_generation" in changes.model_fields_set else current.active_generation
            if state is not None and (consumer != "research_inbox.v1" or active is None
                                      or state.candidate_generation == active):
                raise InvestorError("invalid_action", "Invalid rebuild generation")
            if state is not None and current.rebuild_state is not None and (
                state.candidate_generation != current.rebuild_state.candidate_generation
                or state.domain_watermark < current.rebuild_state.domain_watermark
                or state.journal_watermark < current.rebuild_state.journal_watermark
                or state.last_replayed_sequence < current.rebuild_state.last_replayed_sequence
            ):
                raise InvestorError("invalid_cursor", "Rebuild progress cannot change target or go backwards")
            values["rebuild_state"] = canonical.normalize(state) if state is not None else None
        elif generation_changed and current.rebuild_state is not None:
            raise InvestorError("invalid_action", "Generation switch must also settle rebuild state")
        if changes.mark_success:
            values["last_success_at"] = utc_timestamp(self.clock())
        counts: dict[str, int] = {state: count for state, count in (await self.session.execute(
            select(ConsumerReceipt.state, func.count()).where(ConsumerReceipt.consumer == consumer)
            .group_by(ConsumerReceipt.state))).all()}
        values.update(retry_count=counts.get("retry", 0), quarantined_count=counts.get("quarantined", 0))
        result = await self.session.execute(update(ProjectionCursor).where(
            ProjectionCursor.consumer == consumer, ProjectionCursor.revision == expected_revision,
        ).values(**values).execution_options(synchronize_session=False))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise ProjectionCASConflictError()
        return await self.cursor(consumer)

    def _outcome(self, row: ConsumerReceipt, event: StoredEvent,
                 result: ProjectionOutcome, cursor: CursorRecord) -> None:
        # Re-parse instances to close model_copy/model_construct bypasses.
        outcome = ProjectionOutcome.model_validate(result.model_dump(mode="python"))
        manifest = EffectManifest.model_validate(row.effect_manifest) if row.effect_manifest is not None else None
        if outcome.effect_manifest is not None:
            incoming = outcome.effect_manifest
            if (incoming.source_ref != event.source_ref or incoming.source_revision != event.source_revision
                    or incoming.source_payload_hash != event.payload_hash):
                raise InvestorError("source_conflict", "Manifest does not describe this exact event")
            if row.consumer == "research_inbox.v1" and incoming.generation != cursor.active_generation:
                raise InvestorError("revision_conflict", "Manifest is not for the active generation")
            if manifest is not None and canonical.payload_hash(incoming) != row.effect_manifest_hash:
                raise InvestorError("source_conflict", "Frozen effect manifest cannot change")
            manifest = incoming
            if row.effect_manifest is None:
                row.effect_manifest = cast(dict[str, Any], canonical.normalize(incoming))
                row.effect_manifest_hash = canonical.payload_hash(incoming)
                row.effect_cursor = 0
        if manifest is not None:
            old_progress = row.effect_cursor or 0
            progress = outcome.effect_cursor if outcome.effect_cursor is not None else old_progress
            if not old_progress <= progress <= len(manifest.targets) or progress - old_progress > 50:
                raise InvestorError("invalid_cursor", "Each effect slice may only advance by at most 50 targets")
            if outcome.state in {"applied", "ignored"} and progress != len(manifest.targets):
                raise InvestorError("invalid_action", "Incomplete effects cannot be marked delivered")
            if outcome.state == "retry" and progress == len(manifest.targets):
                raise InvestorError("invalid_action", "Finished effects require a terminal outcome")
            row.effect_cursor = progress
        elif outcome.state == "retry":
            raise InvestorError("invalid_action", "Pending effects require a manifest")
        row.state = outcome.state
        incoming_refs = cast(list[dict[str, Any]], canonical.normalize(outcome.effect_refs))
        refs = {canonical.canonical_json(value): value for value in row.effect_refs}
        refs.update((canonical.canonical_json(value), value) for value in incoming_refs)
        if len(refs) > 10_000:
            raise InvestorError("invalid_action", "Receipt effect reference capacity exceeded")
        row.effect_refs = list(refs.values())
        row.last_error_code, row.error_summary = outcome.reason_code, None
        row.next_retry_at = utc_timestamp(self.clock()) if outcome.state == "retry" else None
        row.processed_at = utc_timestamp(self.clock()) if outcome.state in {"applied", "ignored"} else None

    async def apply_projection(self, consumer: str, event: StoredEvent, *, ordering_key: str,
                               expected_cursor_revision: int, projector_version: str,
                               outcome: ProjectionOutcome | None = None) -> ReceiptRecord:
        registration = self.registry.consumer(consumer)
        await self._reserve_sqlite_outer()
        async with self.session.begin_nested():
            cursor = await self.cursor(consumer)
            if cursor.revision != expected_cursor_revision:
                raise ProjectionCASConflictError()
            row = await self._ensure_receipt(consumer, event.event_id, ordering_key, projector_version)
            if row.state in {"applied", "ignored"}:
                return _receipt_record(row)
            if row.state == "quarantined":
                raise InvestorError("invalid_action", "Quarantine requires an audited repair before projection")
            persisted = stored_event(await self.event_row(event.event_id))
            if canonical.canonical_json(persisted) != canonical.canonical_json(event):
                raise InvestorError("invalid_reference", "Projection input differs from the persisted event")
            predecessor = await self.predecessor(consumer, await self.event_row(event.event_id), ordering_key)
            if predecessor is not None:
                raise InvestorError("revision_conflict", "An unresolved predecessor blocks this projection")
            if outcome is None:
                if consumer == "research_inbox.v1" and cursor.active_generation is None:
                    raise InvestorError("projection_unavailable", "Inbox generation has not been initialized")
                result = await registration.handler(self.session, event)
                if not isinstance(result, ProjectionOutcome):
                    raise ValueError("registered handler must return a ProjectionOutcome")
                outcome = result
            self._outcome(row, event, outcome, cursor)
            await self.session.flush([row])
            await self.compare_and_set_cursor(consumer, expected_cursor_revision,
                CursorUpdate(scanned_sequence=max(cursor.scanned_sequence, event.sequence),
                             mark_success=row.state in {"applied", "ignored"}))
            return _receipt_record(row)

    async def record_failure(self, consumer: str, event: ResearchDomainEvent, *, ordering_key: str,
                             expected_cursor_revision: int, projector_version: str,
                             code: str, error_kind: str, permanent: bool = False,
                             blocked: bool = False) -> ReceiptRecord:
        from datetime import timedelta

        await self._reserve_sqlite_outer()
        async with self.session.begin_nested():
            cursor = await self.cursor(consumer)
            if cursor.revision != expected_cursor_revision:
                raise ProjectionCASConflictError()
            row = await self._ensure_receipt(consumer, UUID(event.event_id), ordering_key, projector_version)
            if row.state in {"applied", "ignored", "quarantined"}:
                return _receipt_record(row)
            now = utc_timestamp(self.clock())
            if blocked:
                row.state, row.next_retry_at = "blocked_predecessor", now + timedelta(seconds=5)
            else:
                row.attempts += 1
                row.state = "quarantined" if permanent or row.attempts >= 5 else "retry"
                delay = (5, 30, 120, 600)[min(row.attempts - 1, 3)]
                row.next_retry_at = now + timedelta(seconds=delay) if row.state == "retry" else None
            row.last_error_code = code[:160]
            # Never copy exception text, traceback locals, prompts or credentials.
            row.error_summary = f"{code[:160]} ({error_kind[:100]})"
            row.processed_at = now if row.state == "quarantined" else None
            await self.session.flush([row])
            await self.compare_and_set_cursor(consumer, expected_cursor_revision,
                CursorUpdate(scanned_sequence=max(cursor.scanned_sequence, event.sequence)))
            return _receipt_record(row)

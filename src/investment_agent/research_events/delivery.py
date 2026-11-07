"""One bounded mechanical delivery attempt inside the caller's transaction.

A trusted ordering resolver accepts raw persisted metadata, so future schema
rows can be isolated BEFORE strict v1 parsing. It must resolve business chains
from controlled source identities/rows, never unknown payload text. ready_work
only selects IDs: the worker owns one commit/lease boundary per event/slice.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from logging import getLogger
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.research_events import canonical
from investment_agent.research_events.registry import EventRegistry
from investment_agent.research_events.repository import (
    DeliveryRepository,
    ProjectionCASConflictError,
    ProjectionOutcome,
    ReceiptRecord,
    _now,
    stored_event,
)
from investment_agent.storage.models.research_events import ConsumerReceipt, ResearchDomainEvent

type OrderingResolver = Callable[[AsyncSession, ResearchDomainEvent], Awaitable[str]]
logger = getLogger(__name__)


class OrderingKeyResolutionError(Exception):
    """Trusted resolver knows the controlled chain but cannot complete lookup."""

    def __init__(self, ordering_key: str) -> None:
        if not ordering_key or len(ordering_key) > 250:
            raise ValueError("invalid controlled ordering key")
        self.ordering_key = ordering_key
        super().__init__("Controlled source ordering lookup failed")


class DeliveryWork(Contract):
    new_event_ids: tuple[UUID, ...]
    retry_event_ids: tuple[UUID, ...]


class DeliveryService:
    def __init__(self, session: AsyncSession, *, registry: EventRegistry,
                 ordering_key: OrderingResolver, clock: Callable[[], datetime] = _now,
                 projector_version: str = "events/1") -> None:
        self.session, self.registry, self.ordering_key = session, registry, ordering_key
        self.clock, self.projector_version = clock, projector_version
        self.repository = DeliveryRepository(session, registry=registry, clock=clock)

    async def ready_work(self, consumer: str, *, new_limit: int = 50,
                         retry_limit: int = 20) -> DeliveryWork:
        if (type(new_limit) is not int or not 0 <= new_limit <= 50
                or type(retry_limit) is not int or not 0 <= retry_limit <= 20):
            raise ValueError("mechanical work limits are 0..50 new and 0..20 retries")
        cursor = await self.repository.cursor(consumer)
        new_rows = (await self.session.scalars(select(ResearchDomainEvent.event_id).where(
            ResearchDomainEvent.sequence > cursor.scanned_sequence).order_by(
                ResearchDomainEvent.sequence).limit(new_limit))).all() if new_limit else ()
        prior_receipt, prior_event = aliased(ConsumerReceipt), aliased(ResearchDomainEvent)
        unresolved_predecessor = (select(prior_receipt.event_id).join(prior_event,
            prior_event.event_id == prior_receipt.event_id).where(
                prior_receipt.consumer == ConsumerReceipt.consumer,
                prior_event.owner_id == ResearchDomainEvent.owner_id,
                prior_event.sequence < ResearchDomainEvent.sequence,
                prior_receipt.state.in_(("retry", "blocked_predecessor", "quarantined")),
                or_(prior_receipt.ordering_key == ConsumerReceipt.ordering_key,
                    prior_receipt.last_error_code == "ordering_key_unavailable"),
            ).correlate(ConsumerReceipt, ResearchDomainEvent).exists())
        retry_rows = (await self.session.scalars(select(ConsumerReceipt.event_id).join(
            ResearchDomainEvent, ResearchDomainEvent.event_id == ConsumerReceipt.event_id).where(
                ConsumerReceipt.consumer == consumer,
                ConsumerReceipt.state.in_(("retry", "blocked_predecessor")),
                or_(ConsumerReceipt.state == "retry", ~unresolved_predecessor),
                or_(ConsumerReceipt.next_retry_at.is_(None), ConsumerReceipt.next_retry_at <= utc_timestamp(self.clock())),
            ).order_by(ResearchDomainEvent.sequence).limit(retry_limit))).all() if retry_limit else ()
        return DeliveryWork(new_event_ids=tuple(UUID(value) for value in new_rows),
                            retry_event_ids=tuple(UUID(value) for value in retry_rows))

    async def deliver_one(self, consumer: str, event_id: UUID) -> ReceiptRecord:
        registration = self.registry.consumer(consumer)
        repository = self.repository
        cursor = await repository.cursor(consumer)
        raw = await repository.event_row(event_id)
        existing = await repository.receipt(consumer, event_id)
        if existing is not None and (existing.state in {"applied", "ignored", "quarantined"}
                or (existing.next_retry_at is not None and existing.next_retry_at > utc_timestamp(self.clock()))):
            return existing
        # New scans may not jump over an unrecorded earlier event. Retries use
        # their original sequence while scanned_sequence remains monotonic.
        earlier = await self.session.scalar(select(ResearchDomainEvent.sequence).where(
            ResearchDomainEvent.sequence > cursor.scanned_sequence,
            ResearchDomainEvent.sequence < raw.sequence).limit(1))
        if earlier is not None:
            raise InvestorError("invalid_cursor", "Deliver earlier unscanned events first")
        unresolved_key = f"unresolved:{raw.owner_id}:{canonical.payload_hash(raw.source_identity)}"
        try:
            async with self.session.begin_nested():
                key = await self.ordering_key(self.session, raw)
                if not isinstance(key, str) or not key or len(key) > 250:
                    raise ValueError("ordering resolver returned an invalid controlled key")
        except OrderingKeyResolutionError as error:
            return await self._failure(consumer, event_id, error.ordering_key, cursor.revision,
                                       "ordering_key_failed", type(error).__name__, permanent=True)
        except Exception as error:
            # The fallback is deterministic and traceable to the immutable
            # source_identity. This quarantine becomes a conservative barrier,
            # not a random alternate chain that lets later events bypass it.
            key = unresolved_key
            return await self._failure(consumer, event_id, key, cursor.revision,
                                       "ordering_key_unavailable", type(error).__name__, permanent=True)
        if existing is not None and existing.ordering_key != key:
            # Changing a frozen chain is not an automatic repair operation.
            raise InvestorError("source_conflict", "Trusted delivery chain changed")
        if raw.schema_version not in registration.schema_versions:
            return await self._failure(consumer, event_id, key, cursor.revision,
                                       "unsupported_event_schema", "SchemaVersion", permanent=True)
        try:
            event = stored_event(raw)
        except (ValidationError, ValueError) as error:
            return await self._failure(consumer, event_id, key, cursor.revision,
                                       "invalid_event_contract", type(error).__name__, permanent=True)
        if await repository.predecessor(consumer, raw, key) is not None:
            return await repository.record_failure(consumer, raw, ordering_key=key,
                expected_cursor_revision=cursor.revision, projector_version=self.projector_version,
                code="blocked_predecessor", error_kind="Predecessor", blocked=True)
        try:
            outcome = ProjectionOutcome(state="ignored", reason_code="unsupported_event_type") if event.event_type not in registration.event_types else None
            return await repository.apply_projection(consumer, event, ordering_key=key,
                expected_cursor_revision=cursor.revision, projector_version=self.projector_version, outcome=outcome)
        except ProjectionCASConflictError:
            # The savepoint already removed callback effects. The caller must
            # retry/roll back its unit of work; never obscure a competing CAS.
            raise
        except (ValidationError, ValueError) as error:
            return await self._failure(consumer, event_id, key, cursor.revision,
                                       "invalid_projection_result", type(error).__name__, permanent=True)
        except Exception as error:
            code = "projection_failed"
            permanent = False
            if isinstance(error, InvestorError):
                known_codes = {"not_found", "source_stale", "revision_conflict", "feature_disabled",
                               "projection_unavailable", "dependency_unavailable", "invalid_reference",
                               "source_conflict", "invalid_action", "invalid_cursor"}
                code = error.detail.code if error.detail.code in known_codes else "projection_failed"
                permanent = code in {"invalid_reference", "source_conflict", "invalid_action", "invalid_cursor"}
            return await self._failure(consumer, event_id, key, cursor.revision,
                                       code, type(error).__name__, permanent=permanent)

    async def _failure(self, consumer: str, event_id: UUID, key: str,
                       revision: int, code: str, error_kind: str, *, permanent: bool) -> ReceiptRecord:
        # Log a fixed message and safe identifiers, not exception messages or
        # traceback locals. The persisted receipt/returned state is explicit.
        raw = await self.repository.event_row(event_id)
        logger.warning("Research event delivery failed", extra={
            "consumer": consumer, "domain_event_id": raw.event_id,
            "error_code": code, "error_kind": error_kind,
        })
        return await self.repository.record_failure(consumer, raw, ordering_key=key,
            expected_cursor_revision=revision, projector_version=self.projector_version,
            code=code, error_kind=error_kind, permanent=permanent)

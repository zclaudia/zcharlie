"""Typed, transaction-neutral persistence for P2 rule and event records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.storage.models.supplemental_rules import (
    ResearchEventRevision,
    SupplementalRegistryHead,
    SupplementalRuleEvent,
    SupplementalRuleHead,
    SupplementalRuleRevision,
)
from investment_agent.supplemental_rules.canonical import rule_content_hash, rule_semantic_hash
from investment_agent.supplemental_rules.contracts import ResearchEvent, RuleDefinition


@dataclass(frozen=True)
class RegistryView:
    owner_id: OwnerId
    registry_revision: int = 0
    rules_generation: int = 0
    events_generation: int = 0
    semantic_probe_hash: str | None = None


class SupplementalRulesRepository:
    """Read/write validated P2 rows; the caller owns commit and orchestration."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_registry_head(self, owner_id: OwnerId) -> RegistryView:
        _check_owner(owner_id)
        # This read often follows reserve_registry's Core upsert in the same
        # session; refresh even if an older row is already identity-mapped.
        row = (await self.session.execute(
            select(SupplementalRegistryHead)
            .where(SupplementalRegistryHead.owner_id == owner_id)
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if row is None:
            return RegistryView(owner_id)
        if row.owner_id != owner_id:
            raise ValueError("registry owner does not match requested owner")
        return RegistryView(
            owner_id=owner_id,
            registry_revision=row.registry_revision,
            rules_generation=row.rules_generation,
            events_generation=row.events_generation,
            semantic_probe_hash=row.semantic_probe_hash,
        )

    async def reserve_registry(
        self, owner_id: OwnerId, *, reserved_at: datetime | None = None,
    ) -> RegistryView:
        """Acquire the SQLite writer before business reads without advancing counters."""

        _check_owner(owner_id)
        now = reserved_at or datetime.now(UTC)
        stmt = sqlite_insert(SupplementalRegistryHead).values(
            owner_id=owner_id,
            registry_revision=0,
            rules_generation=0,
            events_generation=0,
            semantic_probe_hash=None,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SupplementalRegistryHead.owner_id],
            set_={"updated_at": now},
        )
        await self.session.execute(stmt)
        return await self.get_registry_head(owner_id)

    async def advance_registry(
        self,
        owner_id: OwnerId,
        *,
        rules_delta: int = 0,
        events_delta: int = 0,
        semantic_probe_hash: str | None = None,
        updated_at: datetime,
    ) -> RegistryView:
        """Atomically advance one committed operation's registry/generation counters."""

        _check_owner(owner_id)
        if rules_delta not in (0, 1) or events_delta not in (0, 1) or (rules_delta and events_delta):
            raise ValueError("one mutation may advance at most one generation")
        row = await self.session.get(SupplementalRegistryHead, owner_id)
        if row is None:
            row = SupplementalRegistryHead(
                owner_id=owner_id,
                registry_revision=1,
                rules_generation=rules_delta,
                events_generation=events_delta,
                semantic_probe_hash=semantic_probe_hash,
                updated_at=updated_at,
            )
            self.session.add(row)
        else:
            result = await self.session.execute(
                update(SupplementalRegistryHead)
                .where(
                    SupplementalRegistryHead.owner_id == owner_id,
                    SupplementalRegistryHead.registry_revision == row.registry_revision,
                )
                .values(
                    registry_revision=row.registry_revision + 1,
                    rules_generation=row.rules_generation + rules_delta,
                    events_generation=row.events_generation + events_delta,
                    semantic_probe_hash=semantic_probe_hash or row.semantic_probe_hash,
                    updated_at=updated_at,
                )
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise ValueError("supplemental registry changed concurrently")
            await self.session.refresh(row)
        return await self.get_registry_head(owner_id)

    async def get_rule_row(self, owner_id: OwnerId, reference: ObjectRef) -> SupplementalRuleRevision | None:
        _check_ref(owner_id, reference, "supplemental_rule")
        row = await self.session.get(SupplementalRuleRevision, (str(reference.id), reference.revision))
        if row is None or row.owner_id != owner_id:
            return None
        return row

    async def get_rule_revision(self, owner_id: OwnerId, reference: ObjectRef) -> RuleDefinition | None:
        row = await self.get_rule_row(owner_id, reference)
        if row is None:
            return None
        return self._parse_rule(row, owner_id)

    async def save_rule_revision(self, owner_id: OwnerId, definition: RuleDefinition) -> RuleDefinition:
        _check_owner(owner_id)
        checked = RuleDefinition.model_validate(definition.model_dump(mode="json"))
        expected_content = rule_content_hash(checked)
        expected_semantic = rule_semantic_hash(checked)
        if checked.content_hash not in (None, expected_content) or checked.semantic_hash not in (None, expected_semantic):
            raise ValueError("rule content hashes do not match its definition")
        checked = checked.model_copy(update={"content_hash": expected_content, "semantic_hash": expected_semantic})
        key = (str(checked.id), checked.revision)
        current = await self.session.get(SupplementalRuleRevision, key)
        payload = checked.model_dump(mode="json")
        if current is not None:
            if current.owner_id != owner_id or current.payload != payload:
                raise ValueError("rule revision is immutable and already has different content")
            return self._parse_rule(current, owner_id)
        row = SupplementalRuleRevision(
            id=str(checked.id), revision=checked.revision, owner_id=owner_id,
            schema_version=checked.schema_version, content_hash=expected_content,
            semantic_hash=expected_semantic, payload=payload,
        )
        self.session.add(row)
        await self.session.flush()
        return checked

    async def get_rule_head(self, owner_id: OwnerId, rule_id: UUID | str) -> SupplementalRuleHead | None:
        _check_owner(owner_id)
        return await self.session.get(SupplementalRuleHead, (owner_id, str(rule_id)))

    async def put_rule_head(
        self,
        owner_id: OwnerId,
        rule_id: UUID | str,
        *,
        expected_head_revision: int,
        active_revision: int | None,
        active_state: str | None,
        draft_revision: int | None,
        draft_state: str | None,
        last_validation_revision: int | None,
        last_validation_hash: str | None,
        last_validation_payload: dict[str, Any] | None,
    ) -> SupplementalRuleHead:
        _check_owner(owner_id)
        ident = str(rule_id)
        if active_revision is not None and await self.get_rule_revision(owner_id, ObjectRef(kind="supplemental_rule", id=UUID(ident), revision=active_revision)) is None:
            raise ValueError("active revision is not owned by this owner/rule")
        if draft_revision is not None and await self.get_rule_revision(owner_id, ObjectRef(kind="supplemental_rule", id=UUID(ident), revision=draft_revision)) is None:
            raise ValueError("draft revision is not owned by this owner/rule")
        if last_validation_revision is not None and (
            last_validation_revision != draft_revision
            or await self.get_rule_revision(
                owner_id,
                ObjectRef(
                    kind="supplemental_rule",
                    id=UUID(ident),
                    revision=last_validation_revision,
                ),
            ) is None
        ):
            raise ValueError("last validation must reference the current draft revision")
        head = await self.get_rule_head(owner_id, ident)
        if head is None:
            if expected_head_revision != 0:
                raise ValueError("rule head revision conflict")
            head = SupplementalRuleHead(
                owner_id=owner_id,
                rule_id=ident,
                head_revision=1,
                active_revision=active_revision,
                active_state=active_state,
                draft_revision=draft_revision,
                draft_state=draft_state,
                last_validation_revision=last_validation_revision,
                last_validation_hash=last_validation_hash,
                last_validation_payload=last_validation_payload,
            )
            self.session.add(head)
        else:
            result = await self.session.execute(
                update(SupplementalRuleHead)
                .where(
                    SupplementalRuleHead.owner_id == owner_id,
                    SupplementalRuleHead.rule_id == ident,
                    SupplementalRuleHead.head_revision == expected_head_revision,
                )
                .values(
                    head_revision=expected_head_revision + 1,
                    active_revision=active_revision,
                    active_state=active_state,
                    draft_revision=draft_revision,
                    draft_state=draft_state,
                    last_validation_revision=last_validation_revision,
                    last_validation_hash=last_validation_hash,
                    last_validation_payload=last_validation_payload,
                )
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise ValueError("rule head revision conflict")
            await self.session.refresh(head)
        await self.session.flush()
        return head

    async def list_active_rules(
        self, owner_id: OwnerId, *, limit: int = 100, after_rule_id: str | None = None,
    ) -> tuple[tuple[RuleDefinition, ...], bool]:
        _check_owner(owner_id)
        if not 1 <= limit <= 5000:
            raise ValueError("rule scan limit must be in 1..5000")
        stmt = (
            select(SupplementalRuleRevision)
            .join(
                SupplementalRuleHead,
                (SupplementalRuleHead.owner_id == SupplementalRuleRevision.owner_id)
                & (SupplementalRuleHead.rule_id == SupplementalRuleRevision.id)
                & (SupplementalRuleHead.active_revision == SupplementalRuleRevision.revision),
            )
            .where(
                SupplementalRuleHead.owner_id == owner_id,
                SupplementalRuleHead.active_state == "active",
            )
            .order_by(SupplementalRuleHead.rule_id)
            .limit(limit + 1)
        )
        if after_rule_id is not None:
            stmt = stmt.where(SupplementalRuleHead.rule_id > after_rule_id)
        rows = list((await self.session.execute(stmt)).scalars())
        truncated = len(rows) > limit
        definitions = tuple(self._parse_rule(row, owner_id) for row in rows[:limit])
        return definitions, truncated

    async def append_rule_event(
        self,
        owner_id: OwnerId,
        rule_id: UUID | str,
        *,
        operation_id: UUID,
        command: str,
        rule_revision: int | None,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> SupplementalRuleEvent:
        _check_owner(owner_id)
        ident = str(rule_id)
        sequence = await self.session.scalar(
            select(func.coalesce(func.max(SupplementalRuleEvent.sequence), 0)).where(
                SupplementalRuleEvent.owner_id == owner_id,
                SupplementalRuleEvent.rule_id == ident,
            )
        )
        row = SupplementalRuleEvent(
            owner_id=owner_id, rule_id=ident, sequence=int(sequence or 0) + 1,
            rule_revision=rule_revision, operation_id=str(operation_id), command=command,
            created_at=created_at, payload=payload,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_rule_events(
        self, owner_id: OwnerId, rule_id: UUID | str, *, after_sequence: int = 0, limit: int = 100,
    ) -> tuple[tuple[SupplementalRuleEvent, ...], bool]:
        _check_owner(owner_id)
        if not 1 <= limit <= 100:
            raise ValueError("audit page size must be in 1..100")
        stmt = (
            select(SupplementalRuleEvent)
            .where(
                SupplementalRuleEvent.owner_id == owner_id,
                SupplementalRuleEvent.rule_id == str(rule_id),
                SupplementalRuleEvent.sequence > after_sequence,
            )
            .order_by(SupplementalRuleEvent.sequence)
            .limit(limit + 1)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        return tuple(rows[:limit]), len(rows) > limit

    async def get_event_row(self, owner_id: OwnerId, reference: ObjectRef) -> ResearchEventRevision | None:
        _check_ref(owner_id, reference, "research_event")
        row = await self.session.get(ResearchEventRevision, (str(reference.id), reference.revision))
        if row is None or row.owner_id != owner_id:
            return None
        return row

    async def get_event_revision(self, owner_id: OwnerId, reference: ObjectRef) -> ResearchEvent | None:
        row = await self.get_event_row(owner_id, reference)
        if row is None:
            return None
        return self._parse_event(row, owner_id)

    async def save_research_event(
        self, owner_id: OwnerId, event: ResearchEvent, *, created_at: datetime,
        aliases: tuple[str, ...] = (),
    ) -> ResearchEvent:
        _check_owner(owner_id)
        checked = ResearchEvent.model_validate(event.model_dump(mode="json"))
        if checked.owner_id != owner_id:
            raise ValueError("research event owner mismatch")
        if checked.verification != "user_report":
            raise ValueError("public repository writes can only store user_report events")
        previous = None
        if checked.revision == 1:
            duplicate = await self.session.get(ResearchEventRevision, (str(checked.id), 1))
            if duplicate is not None:
                raise ValueError("research event revision already exists")
        else:
            assert checked.previous_ref is not None
            previous = await self.get_event_revision(owner_id, checked.previous_ref)
            if previous is None or previous.id != checked.id or previous.revision != checked.revision - 1:
                raise ValueError("research event previous_ref is missing or stale")
        # Public reports become knowable only when this server accepts them.
        # Body timestamps cannot backdate an event into an earlier cutoff.
        checked = checked.model_copy(update={"acquired_at": created_at, "available_from": created_at})
        checked = ResearchEvent.model_validate(checked.model_dump(mode="json"))
        payload = checked.model_dump(mode="json")
        from investment_agent.supplemental_rules.canonical import canonical_hash
        content_hash = canonical_hash({key: value for key, value in payload.items() if key != "content_hash"})
        if checked.content_hash not in (None, content_hash):
            raise ValueError("research event content hash mismatch")
        checked = checked.model_copy(update={"content_hash": content_hash})
        row = ResearchEventRevision(
            id=str(checked.id), revision=checked.revision, owner_id=owner_id,
            previous_ref=checked.previous_ref.model_dump(mode="json") if checked.previous_ref else None,
            event_key=checked.event_key, event_aliases=list(aliases),
            published_at=checked.published_at, effective_from=checked.effective_from,
            effective_to=checked.effective_to, acquired_at=checked.acquired_at,
            available_from=checked.available_from, created_at=created_at,
            content_hash=content_hash, payload=checked.model_dump(mode="json"),
        )
        self.session.add(row)
        await self.session.flush()
        return checked

    async def list_events_visible_by(
        self, owner_id: OwnerId, *, cutoff: datetime, visible_at: datetime, limit: int = 5000,
    ) -> tuple[tuple[ResearchEvent, ...], bool]:
        _check_owner(owner_id)
        if not 1 <= limit <= 5000:
            raise ValueError("event scan limit must be in 1..5000")
        stmt = (
            select(ResearchEventRevision)
            .where(
                ResearchEventRevision.owner_id == owner_id,
                ResearchEventRevision.available_from <= cutoff,
                ResearchEventRevision.created_at <= visible_at,
            )
            .order_by(ResearchEventRevision.available_from, ResearchEventRevision.id, ResearchEventRevision.revision)
            .limit(limit + 1)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        truncated = len(rows) > limit
        events = tuple(self._parse_event(row, owner_id) for row in rows[:limit])
        return events, truncated

    def _parse_rule(self, row: SupplementalRuleRevision, owner_id: OwnerId) -> RuleDefinition:
        if row.owner_id != owner_id:
            raise ValueError("cross-owner rule row")
        definition = RuleDefinition.model_validate(row.payload)
        if definition.id != UUID(row.id) or definition.revision != row.revision:
            raise ValueError("rule identity columns do not match payload")
        if definition.schema_version != row.schema_version:
            raise ValueError("rule schema_version column does not match payload")
        expected_content = rule_content_hash(definition)
        expected_semantic = rule_semantic_hash(definition)
        if (
            definition.content_hash != row.content_hash
            or definition.semantic_hash != row.semantic_hash
            or expected_content != row.content_hash
            or expected_semantic != row.semantic_hash
        ):
            raise ValueError("rule payload hash does not match recomputed content")
        return definition

    def _parse_event(self, row: ResearchEventRevision, owner_id: OwnerId) -> ResearchEvent:
        if row.owner_id != owner_id:
            raise ValueError("cross-owner event row")
        event = ResearchEvent.model_validate(row.payload)
        if event.id != UUID(row.id) or event.revision != row.revision or event.owner_id != owner_id:
            raise ValueError("event identity columns do not match payload")
        event_payload = event.model_dump(mode="json")
        duplicated_headers = {
            "event_key": row.event_key,
            "previous_ref": row.previous_ref,
            "published_at": row.published_at,
            "effective_from": row.effective_from,
            "effective_to": row.effective_to,
            "acquired_at": row.acquired_at,
            "available_from": row.available_from,
        }
        for field, stored in duplicated_headers.items():
            payload_value = event_payload[field]
            if isinstance(stored, datetime):
                stored = _utc_datetime(stored).isoformat().replace("+00:00", "Z")
            if field == "previous_ref" and stored is not None:
                stored = ObjectRef.model_validate(stored).model_dump(mode="json")
            if payload_value != stored:
                raise ValueError(f"event {field} column does not match payload")
        if event.content_hash != row.content_hash:
            raise ValueError("event hash column does not match payload")
        from investment_agent.supplemental_rules.canonical import canonical_hash
        expected_hash = canonical_hash({
            key: value for key, value in event.model_dump(mode="json").items()
            if key != "content_hash"
        })
        if expected_hash != row.content_hash:
            raise ValueError("event payload hash does not match recomputed content")
        return event


def _check_owner(owner_id: OwnerId) -> None:
    if owner_id != "local_owner":
        raise ValueError("unsupported owner")


def _check_ref(owner_id: OwnerId, reference: ObjectRef, kind: str) -> None:
    _check_owner(owner_id)
    if reference.kind != kind or not isinstance(reference.id, UUID) or reference.field_path is not None:
        raise ValueError(f"exact {kind} UUID reference required")


def _utc_datetime(value: datetime) -> datetime:
    """SQLite drops timezone metadata; interpret its naive values as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

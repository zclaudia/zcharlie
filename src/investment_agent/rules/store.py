"""Rule store (P1-C4): versioned templates and instances (funnel design 5.5).

Templates: the latest stored version of a template id is its *current*
version (``max(version)``); rows are immutable. The database is the authoring
seat (config-docs-plan CD6): :meth:`RuleStore.create_version` appends the next
version for changed content after the sync-time validation, recording actor
and note. :meth:`RuleStore.sync_templates` is the *baseline* import of
``config/rule_templates`` (CD5): it only inserts a template id that has no
stored versions yet — a changed file never appends a version over a
DB-authored one; the drift shows in ``file_status`` and is pulled in through
the explicit 从基线导入 endpoint. Activating an older version re-stores its
content as the next version (append-only rollback, CD6).

Instances: ``draft -> active -> retired``. :meth:`RuleStore.create_draft`
and :meth:`RuleStore.edit` always insert a new version (per listing and
scope) and retire superseded drafts; :meth:`RuleStore.approve` activates a
draft and retires the previously active version in the same transaction.
An instance is validated against the exact template version it names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.rules.schema import (
    TEMPLATE_REF,
    RuleInstance,
    RuleTemplate,
    canonical_definition,
    canonical_hash,
)
from investment_agent.rules.validation import (
    RuleContext,
    RuleValidationError,
    check_instance,
    check_template,
)
from investment_agent.storage.models.rules import RuleInstanceRecord, RuleTemplateRecord
from investment_agent.storage.repositories.listing import ListingRepository

SyncAction = Literal["created", "unchanged", "skipped"]
"""``skipped``: the file differs from the store but the store wins (CD5/CD6)."""
VersionAction = Literal["created", "new_version", "unchanged"]


class RuleStoreError(ValueError):
    """Lifecycle violation (approving a non-draft, unknown template ...)."""


@dataclass(frozen=True)
class TemplateSyncOutcome:
    template_id: str
    version: int
    action: SyncAction
    hash: str
    source_path: str | None


def template_from_record(record: RuleTemplateRecord) -> RuleTemplate:
    return RuleTemplate.model_validate({**record.definition, "version": record.version})


def instance_from_record(record: RuleInstanceRecord) -> RuleInstance:
    return RuleInstance.model_validate(
        {
            **record.definition,
            "version": record.version,
            "status": record.status,
            "drafted_by": record.drafted_by,
            "draft_rationale": record.draft_rationale,
            "approved_by": record.approved_by,
            "approved_at": record.approved_at,
        }
    )


def _now() -> datetime:
    return datetime.now(UTC)


class RuleStore:
    def __init__(self, session: AsyncSession, *, context: RuleContext | None = None) -> None:
        self.session = session
        self.context = context

    async def head_token(self, symbol: str, scope: str) -> str:
        """A shared candidate/observation revision and approval-state guard."""
        latest = await self.session.scalar(select(RuleInstanceRecord).where(RuleInstanceRecord.symbol == symbol, RuleInstanceRecord.scope == scope).order_by(RuleInstanceRecord.version.desc()).limit(1))
        active = await self.session.scalar(select(RuleInstanceRecord.id).where(RuleInstanceRecord.symbol == symbol, RuleInstanceRecord.scope == scope, RuleInstanceRecord.status == "active"))
        return f"{latest.id}:{latest.status}:{active}" if latest is not None else "empty"

    # -- templates ---------------------------------------------------------- #

    async def sync_templates(
        self, templates: Sequence[tuple[Path | None, RuleTemplate]]
    ) -> list[TemplateSyncOutcome]:
        """Baseline import of template files (CD5): insert only missing ids.

        A template id that already has stored versions is never re-written
        from a file — the DB is the source of truth. A file whose content
        matches the current version is ``unchanged``; a file that differs
        (a DB-authored version is current, or the file was edited) is
        ``skipped`` with no write: the drift surfaces through ``file_status``
        and is imported explicitly via the 从基线导入 endpoint.
        """

        outcomes: list[TemplateSyncOutcome] = []
        for path, template in sorted(templates, key=lambda item: item[1].id):
            issues = check_template(template, self.context)
            if issues:
                raise RuleValidationError(issues, source=str(path) if path else template.id)
            digest = canonical_hash(template)
            source = str(path) if path is not None else None
            latest = await self.latest_template_record(template.id)
            if latest is not None:
                outcomes.append(
                    TemplateSyncOutcome(
                        template.id,
                        latest.version,
                        "unchanged" if latest.hash == digest else "skipped",
                        latest.hash,
                        source,
                    )
                )
                continue
            record, _action = await self.create_version(template, source_path=source)
            # No stored versions exist in this branch, so the action is "created".
            outcomes.append(
                TemplateSyncOutcome(template.id, record.version, "created", record.hash, source)
            )
        return outcomes

    async def create_version(
        self,
        template: RuleTemplate,
        *,
        actor: str | None = None,
        note: str | None = None,
        source_path: str | None = None,
    ) -> tuple[RuleTemplateRecord, VersionAction]:
        """Validate ``template`` and append it as the next immutable version.

        The same validation as sync time runs first (semantic checks over the
        factor registry and the DB industry groups); an equal content hash to
        the current version is a no-op returning that version
        (``unchanged``). Authored versions carry ``actor`` / ``note`` (CD7).
        """

        issues = check_template(template, self.context)
        if issues:
            raise RuleValidationError(issues, source=template.id)
        digest = canonical_hash(template)
        latest = await self.latest_template_record(template.id)
        if latest is not None and latest.hash == digest:
            return latest, "unchanged"
        version = 1 if latest is None else latest.version + 1
        record = RuleTemplateRecord(
            id=template.id,
            version=version,
            name=template.name,
            definition=canonical_definition(template, include_lifecycle=False),
            hash=digest,
            calibration_status=template.calibration_status,
            source_path=source_path,
            created_by=actor,
            note=note,
        )
        self.session.add(record)
        await self.session.flush()
        return record, ("created" if latest is None else "new_version")

    async def activate_template(
        self, template_id: str, version: int, *, actor: str | None = None, note: str | None = None
    ) -> tuple[RuleTemplateRecord, VersionAction]:
        """Make ``version`` the current version by re-storing it (CD6).

        "Current" is ``max(version)``, so activation is append-only rollback:
        the target version's content becomes the next version row with the
        activation's actor / note. Activating the current version is a no-op
        returning it (``unchanged``). The re-stored content is validated
        again, so a version whose industry groups were since removed cannot
        be re-activated.
        """

        target = await self.session.get(RuleTemplateRecord, (template_id, version))
        if target is None:
            raise RuleStoreError(f"no template {template_id}@{version}")
        latest = await self.latest_template_record(template_id)
        if latest is not None and latest.version == version:
            return latest, "unchanged"
        return await self.create_version(
            template_from_record(target), actor=actor, note=note
        )

    async def latest_template_record(self, template_id: str) -> RuleTemplateRecord | None:
        stmt = (
            select(RuleTemplateRecord)
            .where(RuleTemplateRecord.id == template_id)
            .order_by(RuleTemplateRecord.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def template_records(self, template_id: str | None = None) -> list[RuleTemplateRecord]:
        stmt = select(RuleTemplateRecord).order_by(
            RuleTemplateRecord.id, RuleTemplateRecord.version
        )
        if template_id is not None:
            stmt = stmt.where(RuleTemplateRecord.id == template_id)
        return list((await self.session.execute(stmt)).scalars())

    async def get_template(self, template_id: str, version: int | None = None) -> RuleTemplate | None:
        if version is None:
            record = await self.latest_template_record(template_id)
        else:
            record = await self.session.get(RuleTemplateRecord, (template_id, version))
        return template_from_record(record) if record is not None else None

    async def template_by_ref(self, ref: str) -> RuleTemplate | None:
        """``quality_growth@2`` (exact) or ``quality_growth`` (latest)."""

        match = TEMPLATE_REF.fullmatch(ref)
        if match is not None:
            return await self.get_template(match["id"], int(match["version"]))
        return await self.get_template(ref)

    async def latest_templates(self) -> list[RuleTemplate]:
        """The current version of every template id, sorted by id."""

        latest = (
            select(RuleTemplateRecord.id, func.max(RuleTemplateRecord.version).label("version"))
            .group_by(RuleTemplateRecord.id)
            .subquery()
        )
        stmt = (
            select(RuleTemplateRecord)
            .join(
                latest,
                (RuleTemplateRecord.id == latest.c.id)
                & (RuleTemplateRecord.version == latest.c.version),
            )
            .order_by(RuleTemplateRecord.id)
        )
        return [template_from_record(row) for row in (await self.session.execute(stmt)).scalars()]

    # -- instances ---------------------------------------------------------- #

    async def _template_for(self, instance: RuleInstance) -> RuleTemplate:
        parsed = instance.template_id_version()
        if parsed is None:
            raise RuleStoreError(f"template_ref {instance.template_ref!r} is not <id>@<version>")
        template = await self.get_template(*parsed)
        if template is None:
            raise RuleStoreError(f"unknown template {instance.template_ref!r}")
        return template

    async def _next_version(self, listing_id: int, scope: str) -> int:
        stmt = select(func.max(RuleInstanceRecord.version)).where(
            RuleInstanceRecord.listing_id == listing_id, RuleInstanceRecord.scope == scope
        )
        current = (await self.session.execute(stmt)).scalar_one_or_none()
        return (current or 0) + 1

    async def _retire_drafts(self, listing_id: int, scope: str, at: datetime) -> None:
        stmt = select(RuleInstanceRecord).where(
            RuleInstanceRecord.listing_id == listing_id,
            RuleInstanceRecord.scope == scope,
            RuleInstanceRecord.status == "draft",
        )
        for record in (await self.session.execute(stmt)).scalars():
            record.status = "retired"
            record.retired_at = at

    async def create_draft(
        self, instance: RuleInstance, *, now: datetime | None = None
    ) -> RuleInstanceRecord:
        """Validate and store ``instance`` as the next draft version.

        Lifecycle fields of ``instance`` (version, status, approvals) are
        ignored. Earlier drafts of the same listing and scope are retired.
        """

        template = await self._template_for(instance)
        issues = check_instance(instance, template, self.context)
        if issues:
            raise RuleValidationError(issues, source=f"{instance.symbol}/{instance.scope}")
        listing = await ListingRepository(self.session).get_a_share_listing(instance.symbol)
        if listing is None:
            raise RuleStoreError(f"no listing for {instance.symbol}")
        at = now or _now()
        await self._retire_drafts(listing.id, instance.scope, at)
        record = RuleInstanceRecord(
            listing_id=listing.id,
            symbol=instance.symbol,
            scope=instance.scope,
            template_id=template.id,
            template_version=template.version,
            version=await self._next_version(listing.id, instance.scope),
            status="draft",
            definition=canonical_definition(instance, include_lifecycle=False),
            hash=canonical_hash(instance),
            drafted_by=instance.drafted_by,
            draft_rationale=instance.draft_rationale,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def edit(
        self, record_id: int, instance: RuleInstance, *, now: datetime | None = None
    ) -> RuleInstanceRecord:
        """A new draft version replacing ``record_id`` (same symbol and scope)."""

        base = await self._required(record_id)
        if (instance.symbol, instance.scope) != (base.symbol, base.scope):
            raise RuleStoreError("an edit keeps the symbol and scope of the edited rule")
        if base.status == "retired":
            raise RuleStoreError(f"rule instance {record_id} is retired")
        return await self.create_draft(instance, now=now)

    async def approve(
        self, record_id: int, *, approved_by: str, now: datetime | None = None
    ) -> RuleInstanceRecord:
        """Activate a draft; the previously active version is retired."""

        record = await self._required(record_id)
        if record.status != "draft":
            raise RuleStoreError(f"only drafts can be approved (#{record_id} is {record.status})")
        at = now or _now()
        current = await self.active_instance(record.listing_id, record.scope)
        if current is not None:
            current.status = "retired"
            current.retired_at = at
            await self.session.flush()
        record.status = "active"
        record.approved_by = approved_by
        record.approved_at = at
        await self.session.flush()
        return record

    async def retire(self, record_id: int, *, now: datetime | None = None) -> RuleInstanceRecord:
        record = await self._required(record_id)
        if record.status == "retired":
            return record
        record.status = "retired"
        record.retired_at = now or _now()
        await self.session.flush()
        return record

    async def active_instance(self, listing_id: int, scope: str) -> RuleInstanceRecord | None:
        stmt = select(RuleInstanceRecord).where(
            RuleInstanceRecord.listing_id == listing_id,
            RuleInstanceRecord.scope == scope,
            RuleInstanceRecord.status == "active",
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def instances(
        self, *, symbol: str | None = None, status: str | None = None
    ) -> list[RuleInstanceRecord]:
        stmt = select(RuleInstanceRecord).order_by(
            RuleInstanceRecord.symbol, RuleInstanceRecord.scope, RuleInstanceRecord.version
        )
        if symbol is not None:
            stmt = stmt.where(RuleInstanceRecord.symbol == symbol.strip().upper())
        if status is not None:
            stmt = stmt.where(RuleInstanceRecord.status == status)
        return list((await self.session.execute(stmt)).scalars())

    async def get_instance(self, record_id: int) -> RuleInstanceRecord | None:
        return await self.session.get(RuleInstanceRecord, record_id)

    async def _required(self, record_id: int) -> RuleInstanceRecord:
        record = await self.get_instance(record_id)
        if record is None:
            raise RuleStoreError(f"no rule instance {record_id}")
        return record

"""Persistence API for immutable P4 receipts and CAS state projections."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, literal_column, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.supplemental_updates import (
    SupplementalChangeReceiptRow,
    SupplementalRefreshStateRow,
    SupplementalSourceStateRow,
)
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.update_contracts import (
    SupplementalChangeKind,
    SupplementalChangeReceipt,
    SupplementalCoverageState,
    SupplementalRefreshState,
    SupplementalSourceState,
)


class SupplementalUpdatesRepository:
    """Transaction-neutral repository; caller controls transaction commit."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve(self, owner_id: OwnerId) -> None:
        """Acquire the SQLite writer before a hook's business reads."""

        _check_owner(owner_id)
        # A no-match UPDATE acquires SQLite's writer reservation without
        # touching a receipt or firing its row-level immutability trigger.
        # Existing P2 hooks also already hold the writer via their registry
        # reservation; this keeps independent journal writes race-safe.
        await self.session.execute(
            update(SupplementalChangeReceiptRow)
            .where(SupplementalChangeReceiptRow.sequence == 0)
            .values(sequence=SupplementalChangeReceiptRow.sequence)
        )

    async def append_change(
        self,
        owner_id: OwnerId,
        *,
        change_key: str,
        kind: SupplementalChangeKind,
        payload: dict[str, Any],
        receipt_id: UUID | str | None = None,
        acquired_at: datetime | None = None,
        available_from: datetime | None = None,
        committed_at: datetime | None = None,
        operation_id: UUID | str | None = None,
        request_hash: str | None = None,
        parent_sequence: int | None = None,
        listing_id: int | None = None,
        context: str | None = None,
        origin_run_id: int | None = None,
        source_manifest_hash: str | None = None,
    ) -> SupplementalChangeReceipt:
        _check_owner(owner_id)
        if receipt_id is not None:
            try:
                receipt_id = UUID(str(receipt_id))
            except (TypeError, ValueError) as exc:
                raise ValueError("receipt_id must be a UUID") from exc
        await self.reserve(owner_id)
        now = datetime.now(UTC)
        supplied_times = (acquired_at, available_from, committed_at)
        acquired_at = acquired_at or now
        available_from = available_from or acquired_at
        committed_at = committed_at or now
        body_hash = canonical_hash(payload)
        prior = (
            await self.session.execute(
                select(SupplementalChangeReceiptRow).where(
                    SupplementalChangeReceiptRow.owner_id == owner_id,
                    SupplementalChangeReceiptRow.change_key == change_key,
                )
            )
        ).scalar_one_or_none()
        if prior is not None:
            if not _same_change(
                prior,
                kind=kind,
                payload_hash=body_hash,
                payload=payload,
                operation_id=operation_id,
                request_hash=request_hash,
                parent_sequence=parent_sequence,
                listing_id=listing_id,
                context=context,
                origin_run_id=origin_run_id,
                source_manifest_hash=source_manifest_hash,
            ) or not _same_supplied_times(prior, supplied_times) or (
                receipt_id is not None and prior.id != str(receipt_id)
            ):
                raise InvestorError("idempotency_conflict", "变化收据身份已用于不同内容")
            return _receipt(prior)

        if operation_id is not None:
            operation_row = (
                await self.session.execute(
                    select(SupplementalChangeReceiptRow).where(
                        SupplementalChangeReceiptRow.owner_id == owner_id,
                        SupplementalChangeReceiptRow.operation_id == str(operation_id),
                    )
                )
            ).scalar_one_or_none()
            if operation_row is not None:
                raise InvestorError("idempotency_conflict", "操作ID已用于另一条变化收据")
        if kind == "scope_member":
            member_row = (
                await self.session.execute(
                    select(SupplementalChangeReceiptRow).where(
                        SupplementalChangeReceiptRow.kind == "scope_member",
                        SupplementalChangeReceiptRow.parent_sequence == parent_sequence,
                        SupplementalChangeReceiptRow.listing_id == listing_id,
                        SupplementalChangeReceiptRow.context == context,
                    )
                )
            ).scalar_one_or_none()
            if member_row is not None:
                if (
                    member_row.change_key == change_key
                    and _same_change(
                        member_row,
                        kind=kind,
                        payload_hash=body_hash,
                        payload=payload,
                        operation_id=operation_id,
                        request_hash=request_hash,
                        parent_sequence=parent_sequence,
                        listing_id=listing_id,
                        context=context,
                        origin_run_id=origin_run_id,
                        source_manifest_hash=source_manifest_hash,
                    )
                    and _same_supplied_times(member_row, supplied_times)
                ):
                    return _receipt(member_row)
                raise InvestorError("idempotency_conflict", "scope成员身份已用于不同冻结内容")

        row = SupplementalChangeReceiptRow(
            id=str(receipt_id or uuid4()),
            owner_id=owner_id,
            change_key=change_key,
            schema_version=1,
            kind=kind,
            operation_id=_uuid_or_none(operation_id),
            request_hash=request_hash,
            parent_sequence=parent_sequence,
            listing_id=listing_id,
            context=context,
            origin_run_id=origin_run_id,
            source_manifest_hash=source_manifest_hash,
            acquired_at=acquired_at,
            available_from=available_from,
            committed_at=committed_at,
            payload_hash=body_hash,
            payload=payload,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError as exc:
            raise InvestorError("idempotency_conflict", "变化收据身份与并发写入冲突") from exc
        return _receipt(row)

    async def get_change(
        self,
        owner_id: OwnerId,
        receipt_id: UUID | str | int,
    ) -> SupplementalChangeReceipt | None:
        _check_owner(owner_id)
        column = (
            SupplementalChangeReceiptRow.sequence
            if isinstance(receipt_id, int)
            else SupplementalChangeReceiptRow.id
        )
        value = receipt_id if isinstance(receipt_id, int) else str(receipt_id)
        row = (
            await self.session.execute(
                select(SupplementalChangeReceiptRow).where(
                    SupplementalChangeReceiptRow.owner_id == owner_id,
                    column == value,
                )
            )
        ).scalar_one_or_none()
        return _receipt(row) if row is not None else None

    async def get_change_by_key(
        self, owner_id: OwnerId, change_key: str,
    ) -> SupplementalChangeReceipt | None:
        """Resolve one exact idempotency key without scanning journal history."""
        _check_owner(owner_id)
        if not change_key or len(change_key) > 200:
            raise ValueError("invalid change key")
        row = (
            await self.session.execute(
                select(SupplementalChangeReceiptRow).where(
                    SupplementalChangeReceiptRow.owner_id == owner_id,
                    SupplementalChangeReceiptRow.change_key == change_key,
                )
            )
        ).scalar_one_or_none()
        return _receipt(row) if row is not None else None

    async def list_changes(
        self,
        owner_id: OwnerId,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        kinds: Iterable[str] | None = None,
        listing_id: int | None = None,
        context: str | None = None,
        parent_sequence: int | None = None,
    ) -> list[SupplementalChangeReceipt]:
        _check_owner(owner_id)
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid change receipt page")
        stmt = select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.owner_id == owner_id,
            SupplementalChangeReceiptRow.sequence > after_sequence,
        )
        if kinds is not None:
            kinds_tuple = tuple(kinds)
            if not kinds_tuple:
                return []
            stmt = stmt.where(SupplementalChangeReceiptRow.kind.in_(kinds_tuple))
        if listing_id is not None:
            stmt = stmt.where(SupplementalChangeReceiptRow.listing_id == listing_id)
        if context is not None:
            stmt = stmt.where(SupplementalChangeReceiptRow.context == context)
        if parent_sequence is not None:
            stmt = stmt.where(SupplementalChangeReceiptRow.parent_sequence == parent_sequence)
        stmt = stmt.order_by(SupplementalChangeReceiptRow.sequence).limit(limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_receipt(row) for row in rows]

    async def get_latest_member_outcome(
        self, owner_id: OwnerId, member_sequence: int,
    ) -> SupplementalChangeReceipt | None:
        """Read the newest immutable outcome proof for one scope member."""
        _check_owner(owner_id)
        if member_sequence < 1:
            raise ValueError("member sequence must be positive")
        row = (
            await self.session.execute(
                select(SupplementalChangeReceiptRow).where(
                    SupplementalChangeReceiptRow.owner_id == owner_id,
                    SupplementalChangeReceiptRow.kind == "scope_outcome",
                    func.json_extract(
                        SupplementalChangeReceiptRow.payload, "$.member_sequence"
                    ) == member_sequence,
                ).order_by(SupplementalChangeReceiptRow.sequence.desc()).limit(1)
            )
        ).scalar_one_or_none()
        return _receipt(row) if row is not None else None

    async def get_previous_completed_scope_member(
        self, owner_id: OwnerId, listing_id: int, context: str, before_sequence: int,
    ) -> SupplementalChangeReceipt | None:
        """Find the latest completed member for semantic comparison, indexed by scope."""
        _check_owner(owner_id)
        outcome = aliased(SupplementalChangeReceiptRow)
        outcome_latest = aliased(SupplementalChangeReceiptRow)
        latest_outcome_sequence = (
            select(outcome_latest.sequence)
            .where(
                outcome_latest.owner_id == owner_id,
                outcome_latest.kind == "scope_outcome",
                func.json_extract(
                    outcome_latest.payload, "$.member_sequence"
                ) == SupplementalChangeReceiptRow.sequence,
            )
            .order_by(outcome_latest.sequence.desc())
            .limit(1)
            .correlate(SupplementalChangeReceiptRow)
            .scalar_subquery()
        )
        row = (
            await self.session.execute(
                select(SupplementalChangeReceiptRow)
                .join(outcome, outcome.sequence == latest_outcome_sequence)
                .where(
                    SupplementalChangeReceiptRow.owner_id == owner_id,
                    SupplementalChangeReceiptRow.kind == "scope_member",
                    SupplementalChangeReceiptRow.listing_id == listing_id,
                    SupplementalChangeReceiptRow.context == context,
                    SupplementalChangeReceiptRow.sequence < before_sequence,
                    func.json_extract(outcome.payload, "$.state").in_(
                        ("completed", "research_required_no_case")
                    ),
                )
                .order_by(SupplementalChangeReceiptRow.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _receipt(row) if row is not None else None

    async def ensure_source(
        self,
        owner_id: OwnerId,
        feed_id: str,
        scope_key: str,
        config_hash: str,
        *,
        listing_id: int | None = None,
        context: str | None = None,
        payload: dict[str, Any] | None = None,
        contract_version: str = "v1",
    ) -> SupplementalSourceState:
        _check_owner(owner_id)
        stmt = (
            sqlite_insert(SupplementalSourceStateRow)
            .values(
                owner_id=owner_id,
                feed_id=feed_id,
                scope_key=scope_key,
                revision=0,
                listing_id=listing_id,
                context=context,
                config_hash=config_hash,
                contract_version=contract_version,
                scope_hash=canonical_hash(
                    {
                        "feed_id": feed_id,
                        "scope_key": scope_key,
                        "listing_id": listing_id,
                        "context": context,
                    }
                ),
                coverage_state="unknown",
                success_sequence=0,
                success_receipt_hash=None,
                source_refs=[],
                health_limit_seconds=0,
                payload=payload or {},
            )
            .on_conflict_do_nothing(
                index_elements=["owner_id", "feed_id", "scope_key"],
            )
        )
        await self.session.execute(stmt)
        row = (
            await self.session.execute(
                select(SupplementalSourceStateRow).where(
                    SupplementalSourceStateRow.owner_id == owner_id,
                    SupplementalSourceStateRow.feed_id == feed_id,
                    SupplementalSourceStateRow.scope_key == scope_key,
                )
            )
        ).scalar_one()
        if row.listing_id != listing_id or row.context != context:
            raise InvestorError("idempotency_conflict", "来源scope身份与已保存范围不一致")
        if row.config_hash != config_hash:
            raise InvestorError("revision_conflict", "来源配置已变化，必须经CAS写入新配置")
        if row.contract_version != contract_version:
            raise InvestorError("revision_conflict", "来源合同版本与已保存范围不一致")
        return _source_state(row)

    async def get_source(
        self,
        owner_id: OwnerId,
        feed_id: str,
        scope_key: str,
        *,
        for_update: bool = False,
    ) -> SupplementalSourceState | None:
        _check_owner(owner_id)
        stmt = select(SupplementalSourceStateRow).where(
            SupplementalSourceStateRow.owner_id == owner_id,
            SupplementalSourceStateRow.feed_id == feed_id,
            SupplementalSourceStateRow.scope_key == scope_key,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return _source_state(row) if row is not None else None

    async def list_sources(
        self,
        owner_id: OwnerId,
        listing_id: int,
        context: str,
        *,
        for_update: bool = False,
    ) -> list[SupplementalSourceState]:
        """Return only source states explicitly declared for this exact scope."""
        _check_owner(owner_id)
        stmt = (
            select(SupplementalSourceStateRow)
            .where(
                SupplementalSourceStateRow.owner_id == owner_id,
                SupplementalSourceStateRow.listing_id == listing_id,
                SupplementalSourceStateRow.context == context,
            )
            .order_by(SupplementalSourceStateRow.feed_id)
        )
        if for_update:
            stmt = stmt.with_for_update()
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_source_state(row) for row in rows]

    async def save_source(
        self,
        state: SupplementalSourceState,
        *,
        expected_revision: int,
    ) -> SupplementalSourceState:
        _check_owner(state.owner_id)
        if state.revision != expected_revision + 1:
            raise ValueError("saved source revision must advance exactly once")
        try:
            result = await self.session.execute(
                update(SupplementalSourceStateRow)
                .where(
                    SupplementalSourceStateRow.owner_id == state.owner_id,
                    SupplementalSourceStateRow.feed_id == state.feed_id,
                    SupplementalSourceStateRow.scope_key == state.scope_key,
                    SupplementalSourceStateRow.revision == expected_revision,
                )
                .values(
                    revision=state.revision,
                    config_hash=state.config_hash,
                    contract_version=state.contract_version,
                    scope_hash=state.scope_hash,
                    window_start=state.window_start,
                    window_end=state.window_end,
                    complete_watermark=state.complete_watermark,
                    coverage_state=state.coverage_state,
                    success_sequence=state.success_sequence,
                    success_receipt_hash=state.success_receipt_hash,
                    source_refs=state.source_refs,
                    last_attempt_at=state.last_attempt_at,
                    last_success_at=state.last_success_at,
                    failure_code=state.failure_code,
                    retry_at=state.retry_at,
                    health_limit_seconds=state.health_limit_seconds,
                    payload=state.payload,
                )
            )
        except OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise InvestorError("revision_conflict", "来源状态已被另一写入更新") from exc
            raise
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise InvestorError("revision_conflict", "来源状态已被另一写入更新")
        return state

    async def ensure_refresh(
        self,
        owner_id: OwnerId,
        listing_id: int,
        context: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> SupplementalRefreshState:
        _check_owner(owner_id)
        stmt = (
            sqlite_insert(SupplementalRefreshStateRow)
            .values(
                owner_id=owner_id,
                listing_id=listing_id,
                context=context,
                revision=0,
                confirmed_sequence=0,
                requested_sequence=0,
                active_run_id=None,
                next_due_at=None,
                last_success=None,
                blocked_reason=None,
                payload=payload or {},
            )
            .on_conflict_do_nothing(
                index_elements=["owner_id", "listing_id", "context"],
            )
        )
        await self.session.execute(stmt)
        row = (
            await self.session.execute(
                select(SupplementalRefreshStateRow).where(
                    SupplementalRefreshStateRow.owner_id == owner_id,
                    SupplementalRefreshStateRow.listing_id == listing_id,
                    SupplementalRefreshStateRow.context == context,
                )
            )
        ).scalar_one()
        return _refresh_state(row)

    async def get_refresh(
        self,
        owner_id: OwnerId,
        listing_id: int,
        context: str,
    ) -> SupplementalRefreshState | None:
        _check_owner(owner_id)
        row = (
            await self.session.execute(
                select(SupplementalRefreshStateRow).where(
                    SupplementalRefreshStateRow.owner_id == owner_id,
                    SupplementalRefreshStateRow.listing_id == listing_id,
                    SupplementalRefreshStateRow.context == context,
                )
            )
        ).scalar_one_or_none()
        return _refresh_state(row) if row is not None else None

    async def save_refresh(
        self,
        state: SupplementalRefreshState,
        *,
        expected_revision: int,
    ) -> SupplementalRefreshState:
        _check_owner(state.owner_id)
        if state.revision != expected_revision + 1:
            raise ValueError("saved refresh revision must advance exactly once")
        try:
            result = await self.session.execute(
                update(SupplementalRefreshStateRow)
                .where(
                    SupplementalRefreshStateRow.owner_id == state.owner_id,
                    SupplementalRefreshStateRow.listing_id == state.listing_id,
                    SupplementalRefreshStateRow.context == state.context,
                    SupplementalRefreshStateRow.revision == expected_revision,
                )
                .values(
                    revision=state.revision,
                    confirmed_sequence=state.confirmed_sequence,
                    requested_sequence=state.requested_sequence,
                    active_run_id=state.active_run_id,
                    next_due_at=state.next_due_at,
                    last_success=state.last_success,
                    blocked_reason=state.blocked_reason,
                    payload=state.payload,
                )
            )
        except OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise InvestorError("revision_conflict", "复核状态已被另一写入更新") from exc
            raise
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise InvestorError("revision_conflict", "复核状态已被另一写入更新")
        return state


def _same_change(
    row: SupplementalChangeReceiptRow,
    *,
    kind: str,
    payload_hash: str,
    payload: dict[str, Any],
    operation_id: UUID | str | None,
    request_hash: str | None,
    parent_sequence: int | None,
    listing_id: int | None,
    context: str | None,
    origin_run_id: int | None,
    source_manifest_hash: str | None,
) -> bool:
    return (
        row.kind == kind
        and row.payload_hash == payload_hash
        and row.payload == payload
        and row.operation_id == _uuid_or_none(operation_id)
        and row.request_hash == request_hash
        and row.parent_sequence == parent_sequence
        and row.listing_id == listing_id
        and row.context == context
        and row.origin_run_id == origin_run_id
        and row.source_manifest_hash == source_manifest_hash
    )


def _same_supplied_times(
    row: SupplementalChangeReceiptRow,
    supplied: tuple[datetime | None, datetime | None, datetime | None],
) -> bool:
    stored = (row.acquired_at, row.available_from, row.committed_at)
    for expected, actual in zip(supplied, stored, strict=True):
        if expected is None:
            continue
        if expected.tzinfo is None or expected.utcoffset() is None:
            raise ValueError("receipt timestamps must include a timezone")
        if _aware(actual) != expected:
            return False
    return True


def _receipt(row: SupplementalChangeReceiptRow) -> SupplementalChangeReceipt:
    try:
        if row.schema_version != 1 or not isinstance(row.payload, dict):
            raise ValueError("unsupported receipt header or payload shape")
        if canonical_hash(row.payload) != row.payload_hash:
            raise ValueError("receipt payload hash does not match its body")
        receipt = SupplementalChangeReceipt(
            sequence=row.sequence,
            id=UUID(row.id),
            owner_id=cast(OwnerId, row.owner_id),
            change_key=row.change_key,
            kind=cast(SupplementalChangeKind, row.kind),
            operation_id=UUID(row.operation_id) if row.operation_id else None,
            request_hash=row.request_hash,
            parent_sequence=row.parent_sequence,
            listing_id=row.listing_id,
            context=row.context,
            origin_run_id=row.origin_run_id,
            source_manifest_hash=row.source_manifest_hash,
            acquired_at=_aware(row.acquired_at),
            available_from=_aware(row.available_from),
            committed_at=_aware(row.committed_at),
            payload_hash=row.payload_hash,
            payload=row.payload,
        )
        if receipt.kind == "scope_member" and (
            receipt.parent_sequence is None
            or receipt.listing_id is None
            or receipt.context is None
        ):
            raise ValueError("scope member receipt is missing its immutable scope header")
        return receipt
    except Exception as exc:
        if isinstance(exc, InvestorError):
            raise
        raise InvestorError("invalid_reference", "变化收据头部或内容完整性校验失败") from exc


def _source_state(row: SupplementalSourceStateRow) -> SupplementalSourceState:
    return SupplementalSourceState(
        owner_id=cast(OwnerId, row.owner_id),
        feed_id=row.feed_id,
        scope_key=row.scope_key,
        revision=row.revision,
        listing_id=row.listing_id,
        context=row.context,
        config_hash=row.config_hash,
        contract_version=row.contract_version,
        scope_hash=row.scope_hash,
        window_start=_aware(row.window_start) if row.window_start else None,
        window_end=_aware(row.window_end) if row.window_end else None,
        complete_watermark=_aware(row.complete_watermark) if row.complete_watermark else None,
        coverage_state=cast(SupplementalCoverageState, row.coverage_state),
        success_sequence=row.success_sequence,
        success_receipt_hash=row.success_receipt_hash,
        source_refs=row.source_refs,
        last_attempt_at=_aware(row.last_attempt_at) if row.last_attempt_at else None,
        last_success_at=_aware(row.last_success_at) if row.last_success_at else None,
        failure_code=row.failure_code,
        retry_at=_aware(row.retry_at) if row.retry_at else None,
        health_limit_seconds=row.health_limit_seconds,
        payload=row.payload,
    )


def _refresh_state(row: SupplementalRefreshStateRow) -> SupplementalRefreshState:
    return SupplementalRefreshState(
        owner_id=cast(OwnerId, row.owner_id),
        listing_id=row.listing_id,
        context=row.context,
        revision=row.revision,
        confirmed_sequence=row.confirmed_sequence,
        requested_sequence=row.requested_sequence,
        active_run_id=row.active_run_id,
        next_due_at=_aware(row.next_due_at) if row.next_due_at else None,
        last_success=_aware(row.last_success) if row.last_success else None,
        blocked_reason=row.blocked_reason,
        payload=row.payload,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _coverage_checkpoint_document_refs(checkpoint: Any) -> list[dict[str, Any]] | None:
    """Extract at most 200 unique exact leaves from a frozen poll checkpoint."""
    if not isinstance(checkpoint, dict):
        return None
    refs: dict[tuple[int, str], dict[str, Any]] = {}

    def collect(raw: Any) -> bool:
        if raw is None:
            return True
        if not isinstance(raw, list):
            return False
        for item in raw:
            if not isinstance(item, dict):
                return False
            sequence, receipt_id, digest = item.get("sequence"), item.get("id"), item.get("payload_hash")
            if (type(sequence) is not int or sequence < 1 or not isinstance(receipt_id, str)
                    or not receipt_id or not isinstance(digest, str)
                    or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
                return False
            key = (sequence, receipt_id)
            normalized = {"sequence": sequence, "id": receipt_id, "payload_hash": digest}
            if key in refs and refs[key] != normalized:
                return False
            refs[key] = normalized
        return True

    if not collect(checkpoint.get("document_refs")) or not collect(checkpoint.get("page_document_refs")):
        return None
    current_page = checkpoint.get("current_primary_page")
    if current_page is not None and (
        not isinstance(current_page, dict) or not collect(current_page.get("document_refs"))
    ):
        return None
    pages = checkpoint.get("primary_pages", [])
    if not isinstance(pages, list):
        return None
    for page in pages:
        if not isinstance(page, dict) or not collect(page.get("document_refs")):
            return None
    if len(refs) > 200:
        return None
    return [refs[key] for key in sorted(refs)]


def _uuid_or_none(value: UUID | str | None) -> str | None:
    return str(value) if value is not None else None


def _check_owner(owner_id: OwnerId) -> None:
    if not isinstance(owner_id, str) or not 1 <= len(owner_id) <= 32:
        raise ValueError("invalid supplemental update owner")


def update_digest(value: Any) -> str:
    """P2-canonical digest seam for layers that must not import P2 services."""
    return canonical_hash(value)


async def supplemental_updates_journal_available(session: AsyncSession) -> bool:
    """Explicit schema-capability check for legacy databases at revision 052."""
    connection = await session.connection()
    return await connection.run_sync(
        lambda sync_connection: sa_inspect(sync_connection).has_table(
            "supplemental_change_receipt"
        )
    )

async def load_poll_window_proof(
    session: AsyncSession,
    *,
    owner: OwnerId,
    listing_id: int,
    context: str,
    cutoff: datetime,
    expected_source_state: SupplementalSourceState | None = None,
    expected_final_ref: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Load and verify the latest bounded poll proof visible at ``cutoff``.

    This deliberately reads one final, at most 20 direct page receipts, and
    their directly referenced document leaves. It never walks the journal
    recursively or scans prior poll history.
    """

    from investment_agent.storage.models.disclosure import (
        DisclosureBlob,
        DisclosureItem,
        DisclosureOriginal,
    )
    from investment_agent.storage.models.evidence import SavedEvidence
    from investment_agent.storage.models.supplemental_rules import ResearchEventRevision
    from investment_agent.supplemental_rules.canonical import canonical_hash, canonical_json
    from investment_agent.supplemental_rules.contracts import (
        CoverageReceiptRef,
        PollWindowSourceProof,
    )

    if listing_id < 1 or not context or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        return None
    cutoff = cutoff.astimezone(UTC)
    repository = SupplementalUpdatesRepository(session)
    if expected_final_ref is not None:
        try:
            expected_ref = CoverageReceiptRef.model_validate(expected_final_ref)
        except ValueError:
            return None
        row = await repository.get_change(owner, expected_ref.sequence)
    else:
        row_record = (
            await session.execute(
                select(SupplementalChangeReceiptRow)
                .where(
                    SupplementalChangeReceiptRow.owner_id == owner,
                    SupplementalChangeReceiptRow.kind == literal_column("'source_page'"),
                    SupplementalChangeReceiptRow.listing_id == listing_id,
                    SupplementalChangeReceiptRow.context == context,
                    SupplementalChangeReceiptRow.committed_at <= cutoff,
                    func.json_extract(
                        SupplementalChangeReceiptRow.payload, "$.record_type"
                    ) == literal_column("'poll_final_v1'"),
                )
                .order_by(
                    SupplementalChangeReceiptRow.committed_at.desc(),
                    SupplementalChangeReceiptRow.sequence.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        candidate = _receipt(row_record) if row_record is not None else None
        row = await repository.get_change(owner, candidate.sequence) if candidate is not None else None
    if row is None:
        return None
    if (
        row.kind != "source_page" or row.listing_id != listing_id
        or row.context != context or row.committed_at > cutoff
    ):
        return None
    final_ref = {
        "sequence": row.sequence,
        "id": str(row.id),
        "payload_hash": row.payload_hash,
    }
    if expected_final_ref is not None:
        try:
            if expected_ref.model_dump(mode="json") != final_ref:
                return None
        except ValueError:
            return None
    body = row.payload

    def _iso_utc(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)

    async def _blob_matches(digest: Any) -> bool:
        if not isinstance(digest, str) or len(digest) != 64:
            return False
        blob = await session.get(DisclosureBlob, digest)
        return bool(
            blob is not None
            and hashlib.sha256(blob.body).hexdigest() == digest
        )

    try:
        if (
            body.get("record_type") != "poll_final_v1"
            or body.get("proof_type") != "poll_window_v1"
            or "final_ref" in body
            or body.get("source_id") != "cninfo_szse_v1"
            or body.get("listing_ref", {}).get("id") != listing_id
            or body.get("listing_ref", {}).get("kind") != "listing"
            or body.get("context") != context
            or _iso_utc(body.get("committed_at")) != row.committed_at
            or _iso_utc(body.get("acquired_at")) != row.acquired_at
            or _iso_utc(body.get("available_from")) != row.available_from
        ):
            return None
        bridge_refs = tuple(CoverageReceiptRef.model_validate(item) for item in body["child_receipts"])
        if not bridge_refs or len(bridge_refs) > 20:
            return None
        bridge_ref_wire = [ref.model_dump(mode="json") for ref in bridge_refs]
        if body.get("page_chain_root") != canonical_hash(bridge_ref_wire):
            return None
        if body.get("page_count") != len(bridge_refs):
            return None
        if body.get("poll_ref") is None or body.get("poll_id") is None:
            return None
        poll_root = await repository.get_change(owner, UUID(str(body["poll_ref"])))
        if (
            poll_root is None or poll_root.listing_id != listing_id
            or poll_root.context != context or poll_root.kind != "source_attempt"
            or poll_root.committed_at > row.committed_at
            or poll_root.payload.get("record_type") != "poll_root_v1"
            or poll_root.payload.get("poll_id") != str(body["poll_id"])
            or poll_root.payload.get("authorization_id") != body.get("authorization_id")
            or poll_root.payload.get("authorization_revision") != body.get("authorization_revision")
            or poll_root.payload.get("authorization_hash") != body.get("authorization_hash")
            or poll_root.payload.get("contract_hash") != body.get("contract_hash")
            or poll_root.payload.get("source_scope_hash") != body.get("source_scope_hash")
            or poll_root.payload.get("attempt_scope_hash") != body.get("attempt_scope_hash")
            or poll_root.payload.get("listing_ref") != body.get("listing_ref")
            or poll_root.payload.get("window_start") != body.get("publication_window_start")
            or poll_root.payload.get("window_end") != body.get("publication_window_end")
        ):
            return None
        if (
            body.get("predecessor_snapshot_ref")
            != poll_root.payload.get("predecessor_snapshot_ref")
            or body.get("predecessor_snapshot_refs")
            != poll_root.payload.get("predecessor_snapshot_refs", [])
            or body.get("predecessor_pending_ids_hash")
            != poll_root.payload.get("predecessor_pending_ids_hash")
        ):
            return None
        root_target_end = _iso_utc(
            poll_root.payload.get("coverage_target_end", poll_root.payload.get("window_end"))
        )
        root_window_end = _iso_utc(poll_root.payload.get("window_end"))
        # Admission freezes the target and requested window in the root. The
        # authorization's immutable history start is checked against its grant
        # receipt below; it is intentionally not duplicated into the root.
        root_window_start = _iso_utc(poll_root.payload.get("window_start"))
        root_history_start = _iso_utc(body.get("history_start"))
        if (
            root_target_end is None or root_window_end is None or root_window_start is None
            or root_history_start is None or root_target_end < root_history_start
            or root_window_start < root_history_start or root_window_end > root_target_end
        ):
            return None
        # Pending predecessors are immutable, bounded snapshots.  Validate the
        # root's direct list; never follow snapshot references recursively.
        raw_snapshot_refs = poll_root.payload.get("predecessor_snapshot_refs", [])
        raw_latest_snapshot_ref = poll_root.payload.get("predecessor_snapshot_ref")
        raw_predecessor_ids = poll_root.payload.get("predecessor_pending_native_ids", [])
        predecessor_ids: set[str] = set()
        predecessor_start: datetime | None = None
        predecessor_end: datetime | None = None
        snapshot_rows: list[tuple[CoverageReceiptRef, SupplementalChangeReceipt]] = []
        if (
            not isinstance(raw_snapshot_refs, list) or len(raw_snapshot_refs) > 20
            or not isinstance(raw_predecessor_ids, list)
            or any(not isinstance(item, str) or not item for item in raw_predecessor_ids)
            or len(raw_predecessor_ids) > 200
            or len(set(raw_predecessor_ids)) != len(raw_predecessor_ids)
        ):
            return None
        if raw_snapshot_refs:
            if raw_latest_snapshot_ref != raw_snapshot_refs[-1]:
                return None
            seen_snapshot_refs: set[tuple[int, UUID]] = set()
            for raw_snapshot_ref in raw_snapshot_refs:
                snapshot_ref = CoverageReceiptRef.model_validate(raw_snapshot_ref)
                ref_key = (snapshot_ref.sequence, snapshot_ref.id)
                if ref_key in seen_snapshot_refs:
                    return None
                seen_snapshot_refs.add(ref_key)
                snapshot_row = await repository.get_change(owner, snapshot_ref.sequence)
                if (
                    snapshot_row is None or snapshot_row.id != snapshot_ref.id
                    or snapshot_row.payload_hash != snapshot_ref.payload_hash
                    or snapshot_row.kind != "source_attempt"
                    or snapshot_row.listing_id != listing_id or snapshot_row.context != context
                    or snapshot_row.committed_at > row.committed_at
                    or snapshot_row.payload.get("record_type") != "poll_checkpoint_snapshot_v1"
                    or snapshot_row.payload.get("listing_ref") != body.get("listing_ref")
                    or snapshot_row.payload.get("context") != context
                ):
                    return None
                snapshot_payload = snapshot_row.payload
                old_root_ref = CoverageReceiptRef.model_validate(snapshot_payload["old_root_ref"])
                old_root_row = await repository.get_change(owner, old_root_ref.sequence)
                try:
                    UUID(str(snapshot_payload.get("superseded_by")))
                except (TypeError, ValueError):
                    return None
                if (
                    old_root_row is None or old_root_row.id != old_root_ref.id
                    or old_root_row.payload_hash != old_root_ref.payload_hash
                    or old_root_row.kind != "source_attempt"
                    or old_root_row.listing_id != listing_id or old_root_row.context != context
                    or old_root_row.committed_at > snapshot_row.committed_at
                    or old_root_row.payload.get("record_type") != "poll_root_v1"
                    or snapshot_payload.get("poll_id") != str(old_root_row.id)
                    or _iso_utc(snapshot_payload.get("window_start"))
                    != _iso_utc(old_root_row.payload.get("window_start"))
                    or _iso_utc(snapshot_payload.get("window_end"))
                    != _iso_utc(old_root_row.payload.get("window_end"))
                ):
                    return None
                ids = snapshot_payload.get("pending_native_ids")
                start = _iso_utc(snapshot_payload.get("pending_window_start"))
                end = _iso_utc(snapshot_payload.get("pending_window_end"))
                if (
                    not isinstance(ids, list) or len(ids) > 200
                    or any(not isinstance(item, str) or not item for item in ids)
                    or len(set(ids)) != len(ids)
                    or snapshot_payload.get("pending_native_ids_hash") != canonical_hash(ids)
                    or start is None or end is None or end <= start
                    or start < root_history_start or end > root_target_end
                ):
                    return None
                predecessor_ids.update(ids)
                predecessor_start = start if predecessor_start is None else min(predecessor_start, start)
                predecessor_end = end if predecessor_end is None else max(predecessor_end, end)
                snapshot_rows.append((snapshot_ref, snapshot_row))
            latest_payload = snapshot_rows[-1][1].payload
            latest_old_root_ref = CoverageReceiptRef.model_validate(latest_payload["old_root_ref"])
            latest_old_root = await repository.get_change(owner, latest_old_root_ref.sequence)
            if (
                latest_old_root is None or latest_old_root.id != latest_old_root_ref.id
                or latest_old_root.payload_hash != latest_old_root_ref.payload_hash
                or latest_old_root.payload.get("record_type") != "poll_root_v1"
                or str(latest_old_root.id) != str(poll_root.payload.get("predecessor_poll_ref"))
                or latest_payload.get("poll_id") != str(latest_old_root.id)
                or not isinstance(latest_payload.get("superseded_by"), str)
                or _iso_utc(latest_payload.get("window_start"))
                != _iso_utc(latest_old_root.payload.get("window_start"))
                or _iso_utc(latest_payload.get("window_end"))
                != _iso_utc(latest_old_root.payload.get("window_end"))
            ):
                return None
            latest_ids = latest_payload.get("pending_native_ids")
            if (
                not isinstance(latest_ids, list)
                or not predecessor_ids.issubset(set(latest_ids))
                or set(latest_ids) != set(raw_predecessor_ids)
                or len(latest_ids) > 200
            ):
                return None
            prior_aggregate = latest_payload.get("prior_pending_predecessor")
            if isinstance(prior_aggregate, dict):
                prior_refs = prior_aggregate.get("snapshot_refs", [])
                prior_ids = prior_aggregate.get("pending_native_ids", [])
                prior_start = _iso_utc(prior_aggregate.get("window_start"))
                prior_end = _iso_utc(prior_aggregate.get("window_end"))
                if (
                    not isinstance(prior_refs, list) or len(prior_refs) > 19
                    or not isinstance(prior_ids, list)
                    or any(not isinstance(item, str) or not item for item in prior_ids)
                    or len(set(prior_ids)) != len(prior_ids)
                    or prior_aggregate.get("pending_native_ids_hash") != canonical_hash(prior_ids)
                    or prior_start is None or prior_end is None or prior_end <= prior_start
                    or [CoverageReceiptRef.model_validate(ref).model_dump(mode="json") for ref in prior_refs]
                    != [ref.model_dump(mode="json") for ref, _ in snapshot_rows[:-1]]
                ):
                    return None
                prior_snapshot_ids: set[str] = set()
                prior_snapshot_start: datetime | None = None
                prior_snapshot_end: datetime | None = None
                for _, prior_snapshot_row in snapshot_rows[:-1]:
                    prior_snapshot_payload = prior_snapshot_row.payload
                    prior_snapshot_ids.update(prior_snapshot_payload.get("pending_native_ids", []))
                    item_start = _iso_utc(prior_snapshot_payload.get("pending_window_start"))
                    item_end = _iso_utc(prior_snapshot_payload.get("pending_window_end"))
                    if item_start is None or item_end is None:
                        return None
                    prior_snapshot_start = item_start if prior_snapshot_start is None else min(
                        prior_snapshot_start, item_start
                    )
                    prior_snapshot_end = item_end if prior_snapshot_end is None else max(
                        prior_snapshot_end, item_end
                    )
                if (
                    not set(prior_ids).issubset(set(latest_ids))
                    or set(prior_ids) != prior_snapshot_ids
                    or (prior_snapshot_start is None) != (prior_start is None)
                    or (prior_snapshot_end is None) != (prior_end is None)
                    or (prior_start is not None and prior_snapshot_start != prior_start)
                    or (prior_end is not None and prior_snapshot_end != prior_end)
                ):
                    return None
            elif len(snapshot_rows) > 1:
                return None
            if (
                set(raw_predecessor_ids) != set(latest_ids)
                or poll_root.payload.get("predecessor_pending_ids_hash") != canonical_hash(
                    sorted(set(raw_predecessor_ids))
                )
            ):
                return None
        elif (
            raw_latest_snapshot_ref is not None or raw_predecessor_ids
            or poll_root.payload.get("predecessor_poll_ref") is not None
            or poll_root.payload.get("predecessor_pending_ids_hash") != canonical_hash([])
        ):
            return None
        semantic_generation_base = poll_root.payload.get("semantic_generation_base")
        if type(semantic_generation_base) is not int or semantic_generation_base < 0:
            return None
        base_final_ref_raw = poll_root.payload.get("semantic_generation_base_final_ref")
        base_final_sequence = 0
        base_final_generation = 0
        if base_final_ref_raw is not None:
            try:
                base_final_ref = CoverageReceiptRef.model_validate(base_final_ref_raw)
            except ValueError:
                return None
            base_final_row = await repository.get_change(owner, base_final_ref.sequence)
            if (
                base_final_row is None or base_final_row.id != base_final_ref.id
                or base_final_row.payload_hash != base_final_ref.payload_hash
                or base_final_row.kind != "source_page"
                or base_final_row.listing_id != listing_id or base_final_row.context != context
                or base_final_row.payload.get("record_type") != "poll_final_v1"
                or base_final_row.payload.get("proof_type") != "poll_window_v1"
                or base_final_row.payload.get("source_id") != "cninfo_szse_v1"
                or type(base_final_row.payload.get("semantic_generation")) is not int
            ):
                return None
            base_final_sequence = base_final_row.sequence
            base_final_generation = base_final_row.payload["semantic_generation"]
        inherited_leaves: dict[tuple[int, str], tuple[dict[str, Any], str]] = {}
        for _, snapshot_row in snapshot_rows:
            checkpoint = snapshot_row.payload.get("checkpoint")
            refs = _coverage_checkpoint_document_refs(checkpoint)
            snapshot_poll_id = snapshot_row.payload.get("poll_id")
            if refs is None or not isinstance(snapshot_poll_id, str):
                return None
            for ref in refs:
                key = (ref["sequence"], ref["id"])
                existing = inherited_leaves.get(key)
                if existing is not None and existing[0] != ref:
                    return None
                inherited_leaves[key] = (ref, snapshot_poll_id)
        if len(inherited_leaves) > 200:
            return None
        inherited_semantic_delta = 0
        for (sequence, _), (ref, snapshot_poll_id) in inherited_leaves.items():
            if sequence <= base_final_sequence:
                continue
            leaf = await repository.get_change(owner, sequence)
            if (
                leaf is None or str(leaf.id) != ref["id"]
                or leaf.payload_hash != ref["payload_hash"]
                or leaf.kind != "source_attempt" or leaf.listing_id != listing_id
                or leaf.context != context
                or leaf.payload.get("record_type") != "poll_document_v1"
                or leaf.payload.get("poll_id") != snapshot_poll_id
                or type(leaf.payload.get("semantic_change")) is not bool
            ):
                return None
            inherited_semantic_delta += int(leaf.payload["semantic_change"])
        if semantic_generation_base != base_final_generation + inherited_semantic_delta:
            return None
        documents: list[CoverageReceiptRef] = []
        document_rows: list[SupplementalChangeReceipt] = []
        expected_auth = (
            body.get("authorization_id"), body.get("authorization_revision"),
            body.get("authorization_hash"), body.get("contract_hash"),
            body.get("source_scope_hash"), body.get("protocol_profile"),
            body.get("protocol_status"), body.get("poll_id"),
        )

        async def _validate_http_attempt_refs(
            raw_refs: Any, *, request_kind: str, logical_request_key: str,
            expected_response_hash: str,
        ) -> bool:
            if not isinstance(raw_refs, list) or not 3 <= len(raw_refs) <= 9 or len(raw_refs) % 3:
                return False
            prior_attempt: str | None = None
            prior_time: datetime | None = None
            terminal_result: SupplementalChangeReceipt | None = None
            for hop_ordinal in range(len(raw_refs) // 3):
                phase_rows: list[SupplementalChangeReceipt] = []
                for phase_index, phase in enumerate(("reserved", "sent", "result")):
                    ref = CoverageReceiptRef.model_validate(raw_refs[hop_ordinal * 3 + phase_index])
                    receipt = await repository.get_change(owner, ref.sequence)
                    if (
                        receipt is None or receipt.id != ref.id
                        or receipt.payload_hash != ref.payload_hash
                        or receipt.kind != "source_attempt" or receipt.committed_at > cutoff
                        or receipt.payload.get("stage") != phase
                    ):
                        return False
                    phase_rows.append(receipt)
                reserved, sent, result = phase_rows
                permit = reserved.payload.get("permit")
                identity = permit.get("identity") if isinstance(permit, dict) else None
                if not isinstance(permit, dict) or not isinstance(identity, dict):
                    return False
                attempt_id = identity.get("attempt_id")
                permit_id = permit.get("permit_id")
                hop_id = identity.get("hop_id")
                if (
                    identity.get("poll_id") != str(body.get("poll_id"))
                    or identity.get("authorization_id") != body.get("authorization_id")
                    or identity.get("authorization_revision") != body.get("authorization_revision")
                    or identity.get("authorization_hash") != body.get("authorization_hash")
                    or identity.get("contract_hash") != body.get("contract_hash")
                    or identity.get("scope_hash") != body.get("attempt_scope_hash")
                    or identity.get("listing_ref") != body.get("listing_ref")
                    or _iso_utc(identity.get("window_start"))
                    != _iso_utc(body.get("publication_window_start"))
                    or _iso_utc(identity.get("window_end"))
                    != _iso_utc(body.get("publication_window_end"))
                    or identity.get("request_kind") != request_kind
                    or identity.get("logical_request_key") != logical_request_key
                    or identity.get("hop_ordinal") != hop_ordinal
                    or not isinstance(attempt_id, str) or not isinstance(permit_id, str)
                    or not isinstance(hop_id, str)
                    or (prior_attempt is not None and prior_attempt != attempt_id)
                    or reserved.committed_at > sent.committed_at
                    or sent.committed_at > result.committed_at
                    or sent.payload.get("permit_id") != permit_id
                    or sent.payload.get("attempt_id") != attempt_id
                    or sent.payload.get("hop_id") != hop_id
                    or sent.payload.get("hop_ordinal") != hop_ordinal
                    or result.payload.get("permit_id") != permit_id
                    or result.payload.get("attempt_id") != attempt_id
                    or result.payload.get("hop_id") != hop_id
                    or result.payload.get("hop_ordinal") != hop_ordinal
                    or result.payload.get("result_code") != "http_200"
                    or not isinstance(result.payload.get("response_hash"), str)
                    or (prior_time is not None and reserved.committed_at < prior_time)
                ):
                    return False
                prior_attempt = attempt_id
                prior_time = result.committed_at
                terminal_result = result
            return bool(
                terminal_result is not None
                and terminal_result.payload.get("response_hash") == expected_response_hash
            )

        directory_ref = CoverageReceiptRef.model_validate(body["directory_ref"])
        directory = await repository.get_change(owner, directory_ref.sequence)
        if (
            directory is None or directory.id != directory_ref.id
            or directory.payload_hash != directory_ref.payload_hash
            or directory.kind != "source_attempt" or directory.listing_id != listing_id
            or directory.context != context or directory.committed_at > cutoff
            or directory.payload.get("record_type") != "poll_directory_v1"
            or directory.payload.get("poll_id") != body.get("poll_id")
            or directory.payload.get("issuer_id") != body.get("issuer_id")
            or directory.payload.get("directory_hash") is None
            or directory.payload.get("response_hash") != directory.payload.get("directory_hash")
            or directory.payload.get("raw_blob_hash") != directory.payload.get("directory_hash")
            or not await _blob_matches(directory.payload.get("directory_hash"))
            or not await _validate_http_attempt_refs(
                directory.payload.get("http_attempt_refs"), request_kind="directory",
                logical_request_key="directory",
                expected_response_hash=directory.payload.get("directory_hash", ""),
            )
        ):
            return None
        prior_index_ref: dict[str, Any] | None = None
        prior_verify_index_ref: dict[str, Any] | None = None
        primary_last_committed_at: datetime | None = None
        first_verify_sent_at: datetime | None = None
        expected_total: int | None = None
        native_ids: set[str] = set()
        for ordinal, bridge_ref in enumerate(bridge_refs, 1):
            bridge = await repository.get_change(owner, bridge_ref.sequence)
            if (
                bridge is None or bridge.id != bridge_ref.id
                or bridge.payload_hash != bridge_ref.payload_hash
                or bridge.kind != "source_page" or bridge.listing_id != listing_id
                or bridge.context != context or bridge.committed_at > cutoff
                or bridge.payload.get("record_type") != "poll_page_v1"
            ):
                return None
            bridge_payload = bridge.payload
            bridge_auth = (
                bridge_payload.get("authorization_id"), bridge_payload.get("authorization_revision"),
                bridge_payload.get("authorization_hash"), bridge_payload.get("contract_hash"),
                bridge_payload.get("source_scope_hash"), bridge_payload.get("protocol_profile"),
                bridge_payload.get("protocol_status"), bridge_payload.get("poll_id"),
            )
            if (
                bridge_auth != expected_auth or bridge_payload.get("page_num") != ordinal
                or bridge_payload.get("listing_ref") != body.get("listing_ref")
                or bridge_payload.get("window_start") != body.get("publication_window_start")
                or bridge_payload.get("window_end") != body.get("publication_window_end")
                or bridge_payload.get("poll_ref") != str(body.get("poll_ref"))
                or bridge_payload.get("has_more") is not (ordinal < len(bridge_refs))
                or bridge_payload.get("previous_page_ref") != (
                    None if ordinal == 1 else bridge_ref_wire[ordinal - 2]
                )
                or bridge_payload.get("seal_status") != "complete"
            ):
                return None
            index_ref = CoverageReceiptRef.model_validate(bridge_payload["index_ref"])
            index = await repository.get_change(owner, index_ref.sequence)
            if (
                index is None or index.id != index_ref.id or index.payload_hash != index_ref.payload_hash
                or index.kind != "source_page" or index.listing_id != listing_id
                or index.context != context or index.committed_at > cutoff
                or index.payload.get("record_type") != "poll_index_v1"
                or index.payload.get("raw_blob_hash") != index.payload.get("response_hash")
                or not await _blob_matches(index.payload.get("raw_blob_hash"))
            ):
                return None
            payload = index.payload
            index_auth = (
                payload.get("authorization_id"), payload.get("authorization_revision"),
                payload.get("authorization_hash"), payload.get("contract_hash"),
                payload.get("source_scope_hash"), payload.get("protocol_profile"),
                payload.get("protocol_status"), payload.get("poll_id"),
            )
            if (
                index_auth != expected_auth or payload.get("page_num") != ordinal
                or payload.get("query_pass") != "primary"
                or payload.get("listing_ref") != body.get("listing_ref")
                or payload.get("window_start") != body.get("publication_window_start")
                or payload.get("window_end") != body.get("publication_window_end")
                or payload.get("poll_ref") != str(body.get("poll_ref"))
                or payload.get("has_more") is not (ordinal < len(bridge_refs))
                or payload.get("previous_index_ref") != prior_index_ref
                or bridge_payload.get("has_more") is not payload.get("has_more")
                or payload.get("identity_directory_hash") != directory.payload.get("directory_hash")
                or payload.get("issuer_id") != directory.payload.get("issuer_id")
                or not await _validate_http_attempt_refs(
                    payload.get("http_attempt_refs"), request_kind="query",
                    logical_request_key=f"page:{ordinal}",
                    expected_response_hash=payload.get("response_hash", ""),
                )
            ):
                return None
            page_total = payload.get("total")
            if type(page_total) is not int or page_total < 0:
                return None
            if expected_total is None:
                expected_total = page_total
            elif page_total != expected_total:
                return None
            primary_last_committed_at = index.committed_at
            index_ref_wire = index_ref.model_dump(mode="json")
            verify_ref = CoverageReceiptRef.model_validate(
                bridge_payload["verification_index_ref"]
            )
            verify_index = await repository.get_change(owner, verify_ref.sequence)
            if (
                verify_index is None or verify_index.id != verify_ref.id
                or verify_index.payload_hash != verify_ref.payload_hash
                or verify_index.kind != "source_page" or verify_index.listing_id != listing_id
                or verify_index.context != context or verify_index.committed_at > cutoff
                or verify_index.payload.get("record_type") != "poll_index_v1"
                or verify_index.payload.get("raw_blob_hash")
                != verify_index.payload.get("response_hash")
                or not await _blob_matches(verify_index.payload.get("raw_blob_hash"))
            ):
                return None
            verify_payload = verify_index.payload
            if ordinal == 1:
                verify_attempt_refs = verify_payload.get("http_attempt_refs")
                if not isinstance(verify_attempt_refs, list) or len(verify_attempt_refs) < 2:
                    return None
                first_sent_ref = CoverageReceiptRef.model_validate(verify_attempt_refs[1])
                first_sent = await repository.get_change(owner, first_sent_ref.sequence)
                if (
                    first_sent is None or first_sent.id != first_sent_ref.id
                    or first_sent.payload_hash != first_sent_ref.payload_hash
                    or first_sent.payload.get("stage") != "sent"
                ):
                    return None
                first_verify_sent_at = first_sent.committed_at
            verify_auth = (
                verify_payload.get("authorization_id"), verify_payload.get("authorization_revision"),
                verify_payload.get("authorization_hash"), verify_payload.get("contract_hash"),
                verify_payload.get("source_scope_hash"), verify_payload.get("protocol_profile"),
                verify_payload.get("protocol_status"), verify_payload.get("poll_id"),
            )
            verify_index_ref_wire = verify_ref.model_dump(mode="json")
            if (
                verify_auth != expected_auth or verify_payload.get("query_pass") != "verification"
                or verify_payload.get("page_num") != ordinal
                or verify_payload.get("listing_ref") != body.get("listing_ref")
                or verify_payload.get("window_start") != body.get("publication_window_start")
                or verify_payload.get("window_end") != body.get("publication_window_end")
                or verify_payload.get("poll_ref") != str(body.get("poll_ref"))
                or verify_payload.get("has_more") is not payload.get("has_more")
                or verify_payload.get("total") != payload.get("total")
                or verify_payload.get("previous_index_ref") != prior_verify_index_ref
                or verify_payload.get("identity_directory_hash") != directory.payload.get("directory_hash")
                or verify_payload.get("issuer_id") != directory.payload.get("issuer_id")
                or verify_payload.get("ordered_document_keys") != payload.get("ordered_document_keys")
                or verify_payload.get("ordered_document_keys_hash")
                != payload.get("ordered_document_keys_hash")
                or bridge_payload.get("verification_ordered_document_keys_hash")
                != verify_payload.get("ordered_document_keys_hash")
                or bridge_payload.get("verification_response_hash") != verify_payload.get("response_hash")
                or bridge_payload.get("verification_http_attempt_refs")
                != verify_payload.get("http_attempt_refs")
                or not await _validate_http_attempt_refs(
                    verify_payload.get("http_attempt_refs"), request_kind="query",
                    logical_request_key=f"verify-page:{ordinal}",
                    expected_response_hash=verify_payload.get("response_hash", ""),
                )
            ):
                return None
            refs = bridge_payload.get("document_refs")
            if not isinstance(refs, list) or len(refs) > 20:
                return None
            page_documents: list[CoverageReceiptRef] = []
            page_leaf_rows: list[SupplementalChangeReceipt] = []
            page_eligible = page_filtered = 0
            for raw_ref in refs:
                leaf_ref = CoverageReceiptRef.model_validate(raw_ref)
                leaf = await repository.get_change(owner, leaf_ref.sequence)
                if (
                    leaf is None or leaf.id != leaf_ref.id or leaf.payload_hash != leaf_ref.payload_hash
                    or leaf.kind != "source_attempt" or leaf.listing_id != listing_id
                    or leaf.context != context or leaf.committed_at > cutoff
                    or leaf.payload.get("record_type") != "poll_document_v1"
                    or leaf.payload.get("poll_id") != body.get("poll_id")
                    or leaf.payload.get("poll_ref") != str(body.get("poll_ref"))
                    or leaf.payload.get("index_ref") != index_ref_wire
                    or type(leaf.payload.get("semantic_change")) is not bool
                ):
                    return None
                if leaf.payload.get("content_hash") is not None and not await _validate_http_attempt_refs(
                    leaf.payload.get("http_attempt_refs"), request_kind="fetch",
                    logical_request_key=f"document:{leaf.payload.get('native_announcement_id')}",
                    expected_response_hash=leaf.payload.get("content_hash", ""),
                ):
                    return None
                leaf_auth = (
                    leaf.payload.get("authorization_id"), leaf.payload.get("authorization_revision"),
                    leaf.payload.get("authorization_hash"), leaf.payload.get("contract_hash"),
                    leaf.payload.get("source_scope_hash"), leaf.payload.get("protocol_profile"),
                    leaf.payload.get("protocol_status"), leaf.payload.get("poll_id"),
                )
                if leaf_auth != expected_auth:
                    return None
                disposition = leaf.payload.get("coverage_disposition")
                if disposition == "eligible":
                    try:
                        item_ref = leaf.payload["item_ref"]
                        if (
                            not isinstance(item_ref, dict)
                            or item_ref.get("kind") != "disclosure_item"
                            or item_ref.get("revision") != 1
                            or not isinstance(item_ref.get("id"), str)
                        ):
                            return None
                        UUID(item_ref["id"])
                        original_ref = ObjectRef.model_validate(leaf.payload["original_ref"])
                        evidence_ref = ObjectRef.model_validate(leaf.payload["evidence_ref"])
                        event_ref = ObjectRef.model_validate(leaf.payload["event_ref"])
                    except (KeyError, ValueError):
                        return None
                    if (
                        original_ref.kind != "disclosure_original" or original_ref.revision != 1
                        or evidence_ref.kind != "evidence" or evidence_ref.revision != 1
                        or event_ref.kind != "research_event" or event_ref.revision is None
                        or not isinstance(leaf.payload.get("content_hash"), str)
                        or not await _blob_matches(leaf.payload.get("content_hash"))
                    ):
                        return None
                    item = await session.get(DisclosureItem, item_ref["id"])
                    original = await session.get(DisclosureOriginal, str(original_ref.id))
                    evidence = await session.get(
                        SavedEvidence, (str(evidence_ref.id), evidence_ref.revision),
                    )
                    event_row = await session.get(
                        ResearchEventRevision, (str(event_ref.id), event_ref.revision),
                    )
                    observation_at = _iso_utc(body.get("observation_at"))
                    if observation_at is None or original is None or event_row is None:
                        return None
                    original_acquired_at = _iso_utc(original.payload.get("acquired_at"))
                    original_available_from = _iso_utc(original.payload.get("available_from"))
                    if original_acquired_at is None or original_available_from is None:
                        return None
                    if (
                        item is None or item.owner_id != owner or item.listing_id != listing_id
                        or original.owner_id != owner
                        or original.item_id != item_ref["id"]
                        or original.content_hash != leaf.payload.get("content_hash")
                        or original.payload.get("evidence_ref") != evidence_ref.model_dump(mode="json")
                        or original.payload.get("listing_id") != listing_id
                        or evidence is None or evidence.owner_id != owner
                        or evidence.payload.get("content_hash") != leaf.payload.get("content_hash")
                        or evidence.payload.get("source_ref") != original_ref.model_dump(mode="json")
                        or evidence.payload.get("verification") != "candidate"
                        or event_row.owner_id != owner
                        or event_row.content_hash != canonical_hash({
                            key: value for key, value in event_row.payload.items()
                            if key != "content_hash"
                        })
                        or event_row.payload.get("content_hash") != event_row.content_hash
                        or event_row.payload.get("verification") != "candidate"
                        or original_acquired_at > observation_at
                        or original_available_from > observation_at
                        or _aware(event_row.acquired_at) > observation_at
                        or _aware(event_row.available_from) > observation_at
                    ):
                        return None
                    page_eligible += 1
                elif disposition == "filtered":
                    if any(leaf.payload.get(key) is not None for key in (
                        "content_hash", "item_ref", "original_ref", "evidence_ref", "event_ref",
                    )):
                        return None
                    page_filtered += 1
                else:
                    return None
                page_documents.append(leaf_ref)
                page_leaf_rows.append(leaf)
            page_doc_wire = [item.model_dump(mode="json") for item in page_documents]
            ordered_keys = payload.get("ordered_document_keys")
            actual_keys = [
                {
                    "native_announcement_id": leaf.payload.get("native_announcement_id"),
                    "identity_hash": leaf.payload.get("identity_hash"),
                }
                for leaf in page_leaf_rows
            ]
            page_native_ids = [item.get("native_announcement_id") for item in actual_keys]
            if (
                not isinstance(ordered_keys, list)
                or payload.get("ordered_document_keys_hash") != canonical_hash(ordered_keys)
                or len(ordered_keys) != len(actual_keys)
                or len({canonical_json(item) for item in ordered_keys}) != len(ordered_keys)
                or {canonical_json(item) for item in ordered_keys}
                != {canonical_json(item) for item in actual_keys}
                or any(not isinstance(item, str) or not item for item in page_native_ids)
                or len(set(page_native_ids)) != len(page_native_ids)
                or any(item in native_ids for item in page_native_ids)
                or bridge_payload.get("document_count") != len(page_documents)
                or bridge_payload.get("eligible_count") != page_eligible
                or bridge_payload.get("filtered_count") != page_filtered
                or page_eligible + page_filtered != len(page_documents)
                or bridge_payload.get("seal_hash") != canonical_hash({
                    "index_ref": index_ref_wire,
                    "document_refs": page_doc_wire,
                    "ordered_document_keys_hash": payload.get("ordered_document_keys_hash"),
                    "response_hash": payload.get("response_hash"),
                    "verification_ordered_document_keys_hash": verify_payload.get(
                        "ordered_document_keys_hash"
                    ),
                    "verification_response_hash": verify_payload.get("response_hash"),
                    "verification_index_ref": verify_index_ref_wire,
                })
            ):
                return None
            document_rows.extend(page_leaf_rows)
            documents.extend(page_documents)
            native_ids.update(item for item in page_native_ids if isinstance(item, str))
            if len(documents) > 200:
                return None
            prior_index_ref = index_ref_wire
            prior_verify_index_ref = verify_index_ref_wire
        document_wire = [ref.model_dump(mode="json") for ref in documents]
        if (
            primary_last_committed_at is None or first_verify_sent_at is None
            or first_verify_sent_at < primary_last_committed_at
            or expected_total != len(documents)
        ):
            return None
        if len(set((ref.sequence, ref.id) for ref in documents)) != len(documents):
            return None
        if (
            body.get("document_count") != len(documents)
            or body.get("eligible_count") != sum(
                row.payload.get("coverage_disposition") == "eligible" for row in document_rows
            )
            or body.get("filtered_count") != sum(
                row.payload.get("coverage_disposition") == "filtered" for row in document_rows
            )
            or body.get("eligible_count", 0) + body.get("filtered_count", 0) != len(documents)
            or body.get("document_manifest_hash") != canonical_hash(document_wire)
        ):
            return None
        current_semantic_delta = sum(
            int(leaf.payload["semantic_change"]) for leaf in document_rows
        )
        if body.get("semantic_generation") != semantic_generation_base + current_semantic_delta:
            return None
        has_predecessor = bool(snapshot_rows)
        predecessor_covered = bool(
            has_predecessor
            and predecessor_start is not None and predecessor_end is not None
            and root_window_start <= predecessor_start
            and root_window_end >= predecessor_end
            and set(raw_predecessor_ids).issubset(native_ids)
        )
        expected_predecessor_resolution = (
            "none" if not has_predecessor else "resolved" if predecessor_covered else "pending"
        )
        if body.get("predecessor_resolution") != expected_predecessor_resolution:
            return None
        attempt_scope_hash = body.get("attempt_scope_hash")
        from investment_agent.supplemental_rules.disclosure_contracts import (
            authorization_scope_hash,
        )
        try:
            expected_attempt_scope_hash = authorization_scope_hash(
                UUID(str(body["authorization_id"])), body["authorization_revision"],
                body["authorization_hash"], body["contract_hash"],
                ObjectRef.model_validate(body["listing_ref"]),
                datetime.fromisoformat(body["publication_window_start"]),
                datetime.fromisoformat(body["publication_window_end"]),
            )
        except (TypeError, ValueError):
            return None
        if attempt_scope_hash != expected_attempt_scope_hash:
            return None
        authorization_id = body.get("authorization_id")
        grant_receipt_ref = CoverageReceiptRef.model_validate(body["grant_receipt_ref"])
        if body.get("grant_ref") != authorization_id or body.get("grant_hash") != body.get("authorization_hash"):
            return None
        # Bind the V3 proof to the immutable authorization receipt, not just a
        # mutable source-state status field.
        grant = await repository.get_change(owner, grant_receipt_ref.sequence)
        if (
            grant is None or grant.id != grant_receipt_ref.id
            or grant.payload_hash != grant_receipt_ref.payload_hash
            or grant.kind != "source_authorization"
            or grant.payload.get("authorization_id") != str(authorization_id)
            or grant.payload.get("authorization", {}).get("revision") != body.get("authorization_revision")
            or grant.payload.get("authorization", {}).get("authorization_hash") != body.get("authorization_hash")
        ):
            return None
        grant_window = grant.payload.get("authorization", {})
        grant_start = _iso_utc(grant_window.get("history_start"))
        grant_end = _iso_utc(grant_window.get("history_end"))
        if (
            grant_start is None or grant_end is None
            or root_history_start < grant_start or root_target_end > grant_end
        ):
            return None
        proof_data = {
            key: value for key, value in body.items()
            if key not in {"record_type", "context", "listing_ref", "authorization_id",
                           "authorization_revision", "authorization_hash", "source_scope_hash",
                           "attempt_scope_hash", "grant_receipt_ref", "directory_ref"}
        }
        proof_data["scope_hash"] = body["source_scope_hash"]
        proof_data["final_ref"] = final_ref
        proof = PollWindowSourceProof.model_validate(proof_data)
        previous_final_ref = proof.previous_final_ref
        current_continuous = (
            proof.continuous_index_watermark,
            proof.continuous_original_watermark,
            proof.continuous_complete_watermark,
        )
        if previous_final_ref is None:
            # A final without a predecessor can only establish a frontier by
            # itself when it starts at the authorized history boundary. It
            # cannot inherit a mutable source-state watermark.
            if any(value is not None for value in current_continuous) and (
                proof.publication_window_start > proof.history_start
                or any(value != proof.publication_window_end for value in current_continuous)
            ):
                return None
        else:
            previous_row = await repository.get_change(owner, previous_final_ref.sequence)
            if (
                previous_row is None or previous_row.id != previous_final_ref.id
                or previous_row.payload_hash != previous_final_ref.payload_hash
                or previous_row.kind != "source_page" or previous_row.listing_id != listing_id
                or previous_row.context != context or previous_row.committed_at >= row.committed_at
                or previous_row.payload.get("record_type") != "poll_final_v1"
                or previous_row.payload.get("proof_type") != "poll_window_v1"
                or previous_row.payload.get("source_id") != proof.source_id
                or previous_row.payload.get("contract_version") != proof.contract_version
                or previous_row.payload.get("contract_hash") != proof.contract_hash
                or previous_row.payload.get("source_scope_hash") != proof.scope_hash
                or previous_row.payload.get("listing_ref") != body.get("listing_ref")
                or previous_row.payload.get("context") != context
                or previous_row.payload.get("issuer_id") != proof.issuer_id
                or _iso_utc(previous_row.payload.get("history_start")) != proof.history_start
                or previous_row.payload.get("history_basis_hash") != proof.history_basis_hash
                or previous_row.payload.get("history_epoch") != str(proof.history_epoch)
                or previous_row.payload.get("epoch_ordinal") != proof.epoch_ordinal
                or previous_row.payload.get("semantic_generation") != base_final_generation
                or previous_row.payload.get("coverage_anchor") != proof.coverage_anchor
                or base_final_ref_raw is None
                or previous_final_ref.model_dump(mode="json") != base_final_ref_raw
            ):
                return None
            prior_continuous = tuple(
                _iso_utc(previous_row.payload.get(name))
                for name in (
                    "continuous_index_watermark",
                    "continuous_original_watermark",
                    "continuous_complete_watermark",
                )
            )
            if all(value is None for value in prior_continuous):
                # The first window that reaches the authorized history
                # boundary can establish the frontier. A later local window
                # cannot jump the uncovered gap; it must remain all-null.
                starts_at_history = proof.publication_window_start == proof.history_start
                may_establish = (
                    starts_at_history and proof.predecessor_resolution != "pending"
                )
                expected_continuous: tuple[datetime | None, datetime | None, datetime | None] = (
                    (proof.publication_window_end,) * 3
                    if may_establish else (None, None, None)
                )
                if current_continuous != expected_continuous:
                    return None
            elif any(value is None for value in prior_continuous):
                return None
            else:
                prior_index, prior_original, prior_complete = prior_continuous
                assert prior_index is not None and prior_original is not None and prior_complete is not None
                contiguous = proof.publication_window_start <= prior_complete
                should_advance = contiguous and proof.predecessor_resolution != "pending"
                expected_continuous = (
                    (
                        max(prior_index, proof.publication_window_end),
                        max(prior_original, proof.publication_window_end),
                        max(prior_complete, proof.publication_window_end),
                    )
                    if should_advance
                    else (prior_index, prior_original, prior_complete)
                )
                if current_continuous != expected_continuous:
                    return None
        if expected_source_state is not None:
            state = expected_source_state
            state_target_end = _iso_utc(state.payload.get("coverage_target_end"))
            state_history_frontier = _iso_utc(state.payload.get("history_frontier"))
            state_continuous_index = _iso_utc(state.payload.get("continuous_index_watermark"))
            state_continuous_original = _iso_utc(
                state.payload.get("continuous_original_watermark")
            )
            state_continuous_complete = _iso_utc(
                state.payload.get("continuous_complete_watermark")
            )
            if (
                state.owner_id != owner or state.feed_id != proof.source_id
                or state.listing_id != listing_id or state.context != context
                or state.config_hash != proof.contract_hash or state.scope_hash != proof.scope_hash
                or state.contract_version != proof.contract_version
                or state.payload.get("issuer_id") != proof.issuer_id
                or state.payload.get("history_basis_hash") != proof.history_basis_hash
                or state.payload.get("history_epoch") != str(proof.history_epoch)
                or state.payload.get("epoch_ordinal") != proof.epoch_ordinal
                or state.payload.get("semantic_generation") != proof.semantic_generation
                or state.payload.get("coverage_anchor") != proof.coverage_anchor
                or _iso_utc(state.payload.get("history_start")) != proof.history_start
                or state.complete_watermark is None
                or state.complete_watermark < proof.complete_watermark
                or proof.continuous_index_watermark is None
                or proof.continuous_original_watermark is None
                or proof.continuous_complete_watermark is None
                or proof.continuous_index_watermark < root_target_end
                or proof.continuous_original_watermark < root_target_end
                or proof.continuous_complete_watermark < root_target_end
                or state.coverage_state != "complete"
                or proof.predecessor_resolution == "pending"
            ):
                return None
            if expected_final_ref is None:
                if (
                    state_continuous_index != proof.continuous_index_watermark
                    or state_continuous_original != proof.continuous_original_watermark
                    or state_continuous_complete != proof.continuous_complete_watermark
                ):
                    return None
            elif (
                state_continuous_index is None or proof.continuous_index_watermark is None
                or state_continuous_original is None or proof.continuous_original_watermark is None
                or state_continuous_complete is None or proof.continuous_complete_watermark is None
                or state_continuous_index < proof.continuous_index_watermark
                or state_continuous_original < proof.continuous_original_watermark
                or state_continuous_complete < proof.continuous_complete_watermark
            ):
                return None
            if expected_final_ref is None and (
                state.payload.get("latest_final_sequence") != row.sequence
                or state.payload.get("latest_final_id") != str(row.id)
                or state.payload.get("latest_final_hash") != row.payload_hash
                or proof.continuous_index_watermark is None
                or proof.continuous_original_watermark is None
                or proof.continuous_complete_watermark is None
                or state_target_end is None
                or state_target_end != root_target_end
                or state_history_frontier is None
                or state_history_frontier < state_target_end
                or state.payload.get("history_gap") is True
            ):
                return None
        return proof.model_dump(mode="json")
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

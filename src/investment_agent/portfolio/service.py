"""Atomic authenticated fact append and deterministic bitemporal projection.

Caller owns commit. No investment mandate, recommendation or broker dependency.
Balances include earlier facts in (occurred_at, recorded revision) order.
Equal-time facts sort by recorded revision; reversals remove an original on rebuild, never
rewrite either its body or an earlier snapshot.
"""
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import localcontext
from typing import Any
from uuid import UUID, uuid4, uuid5

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError, storage_conflicts
from investment_agent.portfolio.contracts import (
    Account,
    AppendEvent,
    AppendResult,
    ApplyCorrection,
    Balance,
    CashFact,
    CorporateAction,
    CorrectionResult,
    CreateAccount,
    EventFact,
    EventPage,
    EventRecord,
    ImportBatch,
    ImportPreview,
    ImportResult,
    ImportRowEffect,
    Reversal,
    Snapshot,
    Trade,
)
from investment_agent.portfolio.prices import SessionPriceLookup
from investment_agent.portfolio.projection import (
    COST_METHOD_VERSION as COST_METHOD_VERSION,
)
from investment_agent.portfolio.projection import (
    Projection,
    ProjectionEngine,
)
from investment_agent.portfolio.projection_state import fact_refs
from investment_agent.storage.models.portfolio import (
    PortfolioAccount,
    PortfolioEvent,
    PortfolioRequest,
    PortfolioSnapshot,
)
from investment_agent.storage.models.reference import Listing, Market

log = structlog.get_logger(__name__)


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class PortfolioService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner") -> None:
        self.session = session
        self.owner = owner

    async def _reserve(self) -> None:
        # DML before SELECT: SQLite obtains its writer reservation without
        # upgrading a stale read transaction (BUSY_SNAPSHOT).
        await self.session.execute(update(PortfolioAccount).where(PortfolioAccount.owner_id == self.owner).values(revision=PortfolioAccount.revision))

    async def _account(self, account_id: UUID) -> PortfolioAccount:
        row = await self.session.scalar(select(PortfolioAccount).where(PortfolioAccount.id == str(account_id), PortfolioAccount.owner_id == self.owner).execution_options(populate_existing=True))
        if row is None:
            raise InvestorError("not_found", "Portfolio account not found")
        return row

    @storage_conflicts
    async def create_account(self, request: CreateAccount) -> Account:
        await self._reserve()
        payload = request.model_dump(mode="json")
        row = await self.session.get(PortfolioAccount, str(request.idempotency_key))
        if row is not None:
            if row.owner_id != self.owner:
                raise InvestorError("not_found", "Portfolio account not found")
            if row.input_hash != digest(payload):
                raise InvestorError("idempotency_conflict", "Account key reused with another body")
            return Account.model_validate(row.payload)
        account = Account(id=request.idempotency_key, name=request.name, currency=request.currency, revision=0, current_snapshot_id=None)
        self.session.add(PortfolioAccount(id=str(account.id), owner_id=self.owner, revision=0, current_snapshot_id=None, input_hash=digest(payload), payload=account.model_dump(mode="json")))
        await self.session.flush()
        return account

    async def accounts(self) -> tuple[Account, ...]:
        rows = await self.session.scalars(select(PortfolioAccount).where(PortfolioAccount.owner_id == self.owner).order_by(PortfolioAccount.id))
        return tuple(Account.model_validate(row.payload) for row in rows)

    async def events(self, account_id: UUID, *, after_revision: int = 0, limit: int = 100) -> EventPage:
        if after_revision < 0 or not 1 <= limit <= 200:
            raise InvestorError("invalid_reference", "Invalid ledger pagination")
        account = await self._account(account_id)
        rows = list(await self.session.scalars(select(PortfolioEvent).where(PortfolioEvent.account_id == account.id, PortfolioEvent.revision > after_revision, PortfolioEvent.revision <= account.revision).order_by(PortfolioEvent.revision).limit(limit + 1)))
        values = tuple(EventRecord.model_validate(row.payload) for row in rows[:limit])
        return EventPage(items=values, next_after_revision=values[-1].revision if len(rows) > limit else None, observed_revision=account.revision)

    async def event(self, account_id: UUID, event_id: UUID) -> EventRecord:
        account = await self._account(account_id)
        row = await self.session.scalar(select(PortfolioEvent).where(PortfolioEvent.account_id == account.id, PortfolioEvent.id == str(event_id)))
        if row is None:
            raise InvestorError("not_found", "Portfolio event not found")
        return EventRecord.model_validate(row.payload)

    async def snapshot(self, account_id: UUID, *, snapshot_id: UUID | None = None, revision: int | None = None) -> Snapshot:
        # Current head and immutable snapshot must be read consistently.
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            active = await connection.run_sync(lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False)))
            if not active:
                await connection.exec_driver_sql("BEGIN")
        account = await self._account(account_id)
        query = select(PortfolioSnapshot).where(PortfolioSnapshot.account_id == account.id)
        if snapshot_id is not None:
            query = query.where(PortfolioSnapshot.id == str(snapshot_id))
        elif revision is not None:
            query = query.where(PortfolioSnapshot.revision == revision)
        else:
            query = query.where(PortfolioSnapshot.id == account.current_snapshot_id)
        row = await self.session.scalar(query)
        if row is None:
            raise InvestorError("not_found", "Portfolio snapshot not found")
        value = Snapshot.model_validate(row.payload)
        if revision is not None and value.revision != revision:
            raise InvestorError("not_found", "Portfolio snapshot version not found")
        return value

    async def _result(self, row: PortfolioEvent) -> AppendResult:
        snapshot = await self.session.scalar(select(PortfolioSnapshot).where(PortfolioSnapshot.account_id == row.account_id, PortfolioSnapshot.revision == row.revision))
        if snapshot is None:
            raise RuntimeError("committed event has no atomic snapshot")
        return AppendResult(event=EventRecord.model_validate(row.payload), snapshot=Snapshot.model_validate(snapshot.payload))

    @storage_conflicts
    async def append(self, request: AppendEvent) -> AppendResult:
        await self._reserve()
        account = await self._account(request.account_id)
        payload = request.model_dump(mode="json")
        body_hash = digest(payload)
        fact_hash = digest(request.fact.model_dump(mode="json"))
        receipt = await self.session.get(PortfolioRequest, (account.id, str(request.idempotency_key)))
        if receipt is not None:
            if receipt.input_hash != body_hash:
                raise InvestorError("idempotency_conflict", "Event identity reused with another body")
            prior_event = await self.session.get(PortfolioEvent, receipt.event_id)
            if prior_event is None:
                raise RuntimeError("request receipt has no event")
            return await self._result(prior_event)
        conditions = [PortfolioEvent.idempotency_key == str(request.idempotency_key)]
        if request.batch_id is not None:
            conditions.append((PortfolioEvent.batch_id == str(request.batch_id)) & (PortfolioEvent.row_id == request.row_id))
        prior = list(await self.session.scalars(select(PortfolioEvent).where(PortfolioEvent.account_id == account.id, or_(*conditions))))
        if prior:
            if len(prior) != 1:
                raise InvestorError("idempotency_conflict", "Request identities refer to different events")
            row = prior[0]
            if row.idempotency_key == str(request.idempotency_key):
                same = row.input_hash == body_hash
            else:
                same = row.fact_hash == fact_hash
            if not same:
                raise InvestorError("idempotency_conflict", "Event identity reused with another body")
            self.session.add(PortfolioRequest(account_id=account.id, idempotency_key=str(request.idempotency_key), event_id=row.id, input_hash=body_hash))
            await self.session.flush()
            return await self._result(row)  # replay precedes stale revision CAS
        if request.expected_revision != account.revision:
            raise InvestorError("revision_conflict", "Reload current account revision")
        records = [EventRecord.model_validate(row.payload) for row in await self.session.scalars(select(PortfolioEvent).where(PortfolioEvent.account_id == account.id).order_by(PortfolioEvent.revision))]
        fact = request.fact
        if fact.occurred_at > datetime.now(UTC):
            raise InvestorError("invalid_reference", "Ledger accepts only already occurred facts")
        if not records and (not isinstance(fact, Balance) or fact.kind != "opening_balance"):
            raise InvestorError("invalid_reference", "First fact must confirm opening balance")
        if records and isinstance(fact, Balance) and fact.kind == "opening_balance":
            reversed_ids = {r.request.fact.original_event_id for r in records if isinstance(r.request.fact, Reversal)}
            if any(isinstance(r.request.fact, Balance) and r.request.fact.kind == "opening_balance" and r.id not in reversed_ids for r in records):
                raise InvestorError("invalid_reference", "At most one active opening balance per account")
        if isinstance(fact, Reversal):
            original = next((r for r in records if r.id == fact.original_event_id), None)
            if original is None or isinstance(original.request.fact, Reversal):
                raise InvestorError("invalid_reference", "Original event must belong to this account and cannot be a reversal")
            if any(isinstance(r.request.fact, Reversal) and r.request.fact.original_event_id == fact.original_event_id for r in records):
                raise InvestorError("idempotency_conflict", "Original event already reversed")
        refs: list[ObjectRef] = []
        if isinstance(fact, Balance):
            refs = [h.listing_ref for h in fact.holdings]
        elif isinstance(fact, Trade):
            refs = [fact.holding.listing_ref]
        elif isinstance(fact, (CashFact, CorporateAction)) and fact.listing_ref is not None:
            refs = [fact.listing_ref]
        for ref in refs:
            listing = await self.session.get(Listing, ref.id)
            market = await self.session.get(Market, listing.market_code) if listing else None
            # First release has no FX or foreign-currency settlement support.
            if listing is None or market is None or market.currency != "CNY" or listing.market_code not in {"CN", "SH", "SZ", "BJ", "SSE", "SZSE", "BSE"}:
                raise InvestorError("invalid_reference", "Listing unavailable or outside CNY settlement scope")
        recorded = datetime.now(UTC)
        record = EventRecord(id=uuid4(), revision=account.revision + 1, recorded_at=recorded, request=request)
        projection = await self._project(request.account_id, records + [record], recorded, new_event_ids=frozenset({record.id}))
        if projection.confirmable_deficits:
            # Plan 2.8: a normal sell beyond the confirmed basis is rejected at
            # commit; fix the facts through a correction instead of recording a
            # negative position.
            raise InvestorError("invalid_reference", "Sell exceeds the confirmable quantity; correct the facts first")
        snapshot = projection.snapshot
        result = await self.session.execute(update(PortfolioAccount).where(PortfolioAccount.id == account.id, PortfolioAccount.owner_id == self.owner, PortfolioAccount.revision == request.expected_revision).values(revision=record.revision, current_snapshot_id=str(snapshot.id), payload=Account(id=request.account_id, name=account.payload["name"], currency="CNY", revision=record.revision, current_snapshot_id=snapshot.id).model_dump(mode="json")))
        if not result.rowcount:  # type: ignore[attr-defined]
            raise InvestorError("revision_conflict", "Concurrent portfolio append")
        self.session.add(PortfolioEvent(id=str(record.id), account_id=account.id, revision=record.revision, idempotency_key=str(request.idempotency_key), batch_id=str(request.batch_id) if request.batch_id else None, row_id=request.row_id, reverses_id=str(fact.original_event_id) if isinstance(fact, Reversal) else None, input_hash=body_hash, fact_hash=fact_hash, payload=record.model_dump(mode="json")))
        self.session.add(PortfolioSnapshot(id=str(snapshot.id), account_id=account.id, revision=record.revision, payload=snapshot.model_dump(mode="json")))
        await self.session.flush()
        self.session.add(PortfolioRequest(account_id=account.id, idempotency_key=str(request.idempotency_key), event_id=str(record.id), input_hash=body_hash))
        await self.session.flush()
        return AppendResult(event=record, snapshot=snapshot)

    # -- whole-batch import and preview (plan 2.8, WP4) ---------------------- #

    async def _records(self, account_id: UUID | str) -> list[EventRecord]:
        rows = await self.session.scalars(
            select(PortfolioEvent).where(PortfolioEvent.account_id == str(account_id)).order_by(PortfolioEvent.revision)
        )
        return [EventRecord.model_validate(row.payload) for row in rows]

    def _import_records(
        self, request: ImportBatch, revision: int, *, recorded: datetime
    ) -> list[EventRecord]:
        return [
            EventRecord(
                id=uuid4(),
                revision=revision + 1 + index,
                recorded_at=recorded,
                request=AppendEvent(
                    account_id=request.account_id,
                    expected_revision=request.expected_revision,
                    idempotency_key=uuid5(request.operation_id, row.row_id),
                    batch_id=request.operation_id,
                    row_id=row.row_id,
                    fact=row.fact,
                ),
            )
            for index, row in enumerate(request.rows)
        ]

    async def _import_conflicts(
        self,
        records: list[EventRecord],
        new_records: list[EventRecord],
        *,
        now: datetime,
    ) -> list[str]:
        """Structural conflicts of a batch, named per row where possible."""

        conflicts: list[str] = []
        for record in new_records:
            row_id = record.request.row_id
            fact = record.request.fact
            if fact.occurred_at > now:
                conflicts.append(f"future_fact:{row_id}")
            if fact.occurred_at.tzinfo is None:
                conflicts.append(f"naive_occurred_at:{row_id}")
            for ref in fact_refs(fact):
                listing = await self.session.get(Listing, ref.id)
                market = await self.session.get(Market, listing.market_code) if listing else None
                if listing is None or market is None or market.currency != "CNY" or listing.market_code not in {"CN", "SH", "SZ", "BJ", "SSE", "SZSE", "BSE"}:
                    conflicts.append(f"listing_outside_scope:{row_id}:{ref.id}")
        if not records:
            first = new_records[0].request.fact
            if not (isinstance(first, Balance) and first.kind == "opening_balance"):
                conflicts.append(
                    f"first_fact_must_be_opening_balance:{new_records[0].request.row_id}"
                )
        reversed_ids = {r.request.fact.original_event_id for r in records if isinstance(r.request.fact, Reversal)}
        openings = sum(
            isinstance(r.request.fact, Balance)
            and r.request.fact.kind == "opening_balance"
            and r.id not in reversed_ids
            for r in records
        ) + sum(
            isinstance(r.request.fact, Balance) and r.request.fact.kind == "opening_balance"
            for r in new_records
        )
        if openings > 1:
            conflicts.append("multiple_opening_balance")
        return conflicts

    async def _import_plan(
        self, request: ImportBatch, records: list[EventRecord], *, now: datetime
    ) -> tuple[list[EventRecord], Projection, dict[UUID, dict[str, Any]], list[str], list[str]]:
        """The batch's whole replay: new records, projection, per-row trace, conflicts."""

        new_records = self._import_records(request, records[-1].revision if records else 0, recorded=now)
        conflicts = await self._import_conflicts(records, new_records, now=now)
        trace: dict[UUID, dict[str, Any]] = {record.id: {} for record in new_records}
        # An empty account has no before-state to project: its first batch must
        # open the balance (checked above), so the replay starts from the batch.
        before = await self._project(request.account_id, records, now, trace=None) if records else None
        after = await self._project(
            request.account_id,
            records + new_records,
            now,
            new_event_ids=frozenset(trace),
            trace=trace,
        )
        baseline = set(before.snapshot.conflicts) if before is not None else set()
        introduced = [issue for issue in after.snapshot.conflicts if issue not in baseline]
        conflicts.extend(f"projection:{issue}" for issue in introduced)
        row_of = {record.id: record.request.row_id for record in new_records}
        conflicts.extend(
            f"sell_exceeds_confirmable_quantity:{row_of[event_id]}"
            for event_id, _ref in after.confirmable_deficits
            if event_id in row_of
        )
        return new_records, after, trace, conflicts, introduced

    async def preview_import(self, request: ImportBatch) -> ImportPreview:
        """Server-computed preview on the caller's base revision; no writes.

        A preview older than the current facts is refused (recompute it), and
        every row reports its inclusion - ``audit_prior_history`` rows stay
        history only and never change quantity, cost or cash (plan 2.6, 2.8).
        """

        account = await self._account(request.account_id)
        if request.expected_revision != account.revision:
            raise InvestorError("revision_conflict", "预览基准事实版本已变化，请重新预览整批")
        now = datetime.now(UTC)
        records = await self._records(account.id)
        new_records, projection, trace, conflicts, _introduced = await self._import_plan(
            request, records, now=now
        )
        rejected: dict[str, list[str]] = {}
        for conflict in conflicts:
            for conflict_row in {str(row.row_id) for row in request.rows}:
                if conflict.endswith(f":{conflict_row}") or f":{conflict_row}:" in conflict:
                    rejected.setdefault(conflict_row, []).append(conflict)
        rows: list[ImportRowEffect] = []
        for record in new_records:
            row_id = str(record.request.row_id)
            effect = trace.get(record.id, {})
            inclusion = effect.get("inclusion", "applied")
            rows.append(
                ImportRowEffect(
                    row_id=row_id,
                    occurred_at=record.request.fact.occurred_at,
                    kind=record.request.fact.kind,
                    inclusion=inclusion,
                    accepted=row_id not in rejected,
                    reasons=tuple(rejected.get(row_id, ())),
                    quantity_delta=effect.get("quantity_delta", {}),
                    cash_delta=effect.get("cash_delta"),
                    cost_remaining=effect.get("cost_remaining", {}),
                )
            )
        return ImportPreview(
            account_id=request.account_id,
            base_revision=request.expected_revision,
            batch_hash=digest(request.model_dump(mode="json")),
            ready=not conflicts,
            rows=tuple(rows),
            conflicts=tuple(conflicts),
            resulting_revision=projection.snapshot.revision,
        )

    @storage_conflicts
    async def apply_import(self, request: ImportBatch) -> ImportResult:
        """Commit the whole batch under one CAS, or nothing (plan 2.8).

        The rows keep their ``batch_id``/``row_id`` identity, one snapshot is
        written for the final revision, and a retried identical operation
        returns the original receipt instead of half a batch.
        """

        await self._reserve()
        account = await self._account(request.account_id)
        payload = request.model_dump(mode="json")
        body_hash = digest(payload)
        receipt = await self.session.get(PortfolioRequest, (account.id, str(request.operation_id)))
        if receipt is not None:
            if receipt.input_hash != body_hash:
                raise InvestorError("idempotency_conflict", "Import key reused with another body")
            stored = list(
                await self.session.scalars(
                    select(PortfolioEvent)
                    .where(
                        PortfolioEvent.account_id == account.id,
                        PortfolioEvent.batch_id == str(request.operation_id),
                    )
                    .order_by(PortfolioEvent.revision)
                )
            )
            if not stored:
                raise RuntimeError("import receipt has no events")
            final_row = stored[-1]
            snapshot_row = await self.session.scalar(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.account_id == account.id,
                    PortfolioSnapshot.revision == final_row.revision,
                )
            )
            if snapshot_row is None:
                raise RuntimeError("committed import has no atomic snapshot")
            events = tuple(EventRecord.model_validate(row.payload) for row in stored)
            # The audit replay must use the same projection basis as the first
            # commit - the account's whole history, not just this batch's rows.
            audit = await self._audit_row_ids(events, await self._records(account.id))
            return ImportResult(
                operation_id=request.operation_id,
                events=events,
                snapshot=Snapshot.model_validate(snapshot_row.payload),
                audit_only_row_ids=tuple(audit),
            )
        if request.expected_revision != account.revision:
            raise InvestorError("revision_conflict", "Reload current account revision before importing")
        now = datetime.now(UTC)
        records = await self._records(account.id)
        new_records, projection, trace, conflicts, _introduced = await self._import_plan(
            request, records, now=now
        )
        if conflicts:
            raise InvestorError(
                "invalid_reference",
                "Import batch would not replay validly: " + "; ".join(sorted(conflicts)[:8]),
            )
        snapshot = projection.snapshot
        final = new_records[-1]
        result = await self.session.execute(
            update(PortfolioAccount)
            .where(
                PortfolioAccount.id == account.id,
                PortfolioAccount.owner_id == self.owner,
                PortfolioAccount.revision == request.expected_revision,
            )
            .values(
                revision=final.revision,
                current_snapshot_id=str(snapshot.id),
                payload=Account(
                    id=request.account_id,
                    name=account.payload["name"],
                    currency="CNY",
                    revision=final.revision,
                    current_snapshot_id=snapshot.id,
                ).model_dump(mode="json"),
            )
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            raise InvestorError("revision_conflict", "Concurrent portfolio append")
        for record in new_records:
            fact = record.request.fact
            self.session.add(
                PortfolioEvent(
                    id=str(record.id),
                    account_id=account.id,
                    revision=record.revision,
                    idempotency_key=str(record.request.idempotency_key),
                    batch_id=str(request.operation_id),
                    row_id=record.request.row_id,
                    reverses_id=None,
                    input_hash=body_hash,
                    fact_hash=digest(fact.model_dump(mode="json")),
                    payload=record.model_dump(mode="json"),
                )
            )
        self.session.add(
            PortfolioSnapshot(
                id=str(snapshot.id),
                account_id=account.id,
                revision=final.revision,
                payload=snapshot.model_dump(mode="json"),
            )
        )
        await self.session.flush()
        self.session.add(
            PortfolioRequest(
                account_id=account.id,
                idempotency_key=str(request.operation_id),
                event_id=str(final.id),
                input_hash=body_hash,
            )
        )
        await self.session.flush()
        audit = [
            str(record.request.row_id)
            for record in new_records
            if trace.get(record.id, {}).get("inclusion") == "audit_prior_history"
        ]
        log.info(
            "portfolio.import.applied",
            account_id=str(account.id),
            operation_id=str(request.operation_id),
            rows=len(new_records),
            audit_only=len(audit),
        )
        return ImportResult(
            operation_id=request.operation_id,
            events=tuple(new_records),
            snapshot=snapshot,
            audit_only_row_ids=tuple(audit),
        )

    async def _audit_row_ids(
        self, batch_records: Sequence[EventRecord], all_records: list[EventRecord]
    ) -> list[str]:
        """Prior-history row ids of an already-committed batch (replay path)."""

        trace: dict[UUID, dict[str, Any]] = {record.id: {} for record in batch_records}
        await self._project(
            batch_records[0].request.account_id,
            all_records,
            datetime.now(UTC),
            trace=trace,
        )
        return [
            str(record.request.row_id)
            for record in batch_records
            if trace.get(record.id, {}).get("inclusion") == "audit_prior_history"
        ]

    @storage_conflicts
    async def apply_correction(self, request: ApplyCorrection) -> CorrectionResult:
        """One reversal plus replacements, atomically and idempotently (plan 2.8).

        The operation is bound to one UUID, the revision the user previewed and
        the exact body; a retried identical request returns the original result.
        Every event is written in this transaction (or none is), the account
        head only ever exposes the final snapshot, and a replay that would leave
        a confirmable sell basis exceeded is rejected whole.
        """
        await self._reserve()
        account = await self._account(request.account_id)
        payload = request.model_dump(mode="json")
        body_hash = digest(payload)
        receipt = await self.session.get(PortfolioRequest, (account.id, str(request.operation_id)))
        if receipt is not None:
            if receipt.input_hash != body_hash:
                raise InvestorError("idempotency_conflict", "Correction key reused with another body")
            rows = list(await self.session.scalars(select(PortfolioEvent).where(PortfolioEvent.account_id == account.id, PortfolioEvent.batch_id == str(request.operation_id)).order_by(PortfolioEvent.revision)))
            if not rows:
                raise RuntimeError("correction receipt has no events")
            final_row = rows[-1]
            snapshot_row = await self.session.scalar(select(PortfolioSnapshot).where(PortfolioSnapshot.account_id == account.id, PortfolioSnapshot.revision == final_row.revision))
            if snapshot_row is None:
                raise RuntimeError("committed correction has no atomic snapshot")
            return CorrectionResult(
                operation_id=request.operation_id,
                events=tuple(EventRecord.model_validate(row.payload) for row in rows),
                snapshot=Snapshot.model_validate(snapshot_row.payload),
            )
        if request.expected_revision != account.revision:
            raise InvestorError("revision_conflict", "Reload current account revision")
        records = [EventRecord.model_validate(row.payload) for row in await self.session.scalars(select(PortfolioEvent).where(PortfolioEvent.account_id == account.id).order_by(PortfolioEvent.revision))]
        reversed_now: set[UUID] = set()
        for target in request.reversals:
            original = next((r for r in records if r.id == target), None)
            if original is None or isinstance(original.request.fact, Reversal):
                raise InvestorError("invalid_reference", "Reversed events must belong to this account and cannot be reversals")
            if any(isinstance(r.request.fact, Reversal) and r.request.fact.original_event_id == target for r in records):
                raise InvestorError("idempotency_conflict", "Original event already reversed")
            reversed_now.add(target)
        now = datetime.now(UTC)
        facts: list[EventFact] = [
            Reversal(kind="reversal", occurred_at=now, source="user correction", user_confirmed=True, original_event_id=target, reason=request.reason)
            for target in request.reversals
        ] + list(request.replacements)
        for fact in facts:
            if fact.occurred_at > now:
                raise InvestorError("invalid_reference", "Ledger accepts only already occurred facts")
        reversed_ids = {r.request.fact.original_event_id for r in records if isinstance(r.request.fact, Reversal)} | reversed_now
        openings = sum(isinstance(r.request.fact, Balance) and r.request.fact.kind == "opening_balance" and r.id not in reversed_ids for r in records)
        openings += sum(isinstance(fact, Balance) and fact.kind == "opening_balance" for fact in facts)
        if openings > 1:
            raise InvestorError("invalid_reference", "At most one active opening balance per account")
        for fact in facts:
            refs: list[ObjectRef] = []
            if isinstance(fact, Balance):
                refs = [h.listing_ref for h in fact.holdings]
            elif isinstance(fact, Trade):
                refs = [fact.holding.listing_ref]
            elif isinstance(fact, (CashFact, CorporateAction)) and fact.listing_ref is not None:
                refs = [fact.listing_ref]
            for ref in refs:
                listing = await self.session.get(Listing, ref.id)
                market = await self.session.get(Market, listing.market_code) if listing else None
                if listing is None or market is None or market.currency != "CNY" or listing.market_code not in {"CN", "SH", "SZ", "BJ", "SSE", "SZSE", "BSE"}:
                    raise InvestorError("invalid_reference", "Listing unavailable or outside CNY settlement scope")
        new_records = [
            EventRecord(
                id=uuid4(),
                revision=account.revision + 1 + index,
                recorded_at=now,
                request=AppendEvent(
                    account_id=request.account_id,
                    expected_revision=request.expected_revision,
                    idempotency_key=uuid5(request.operation_id, row_id),
                    batch_id=request.operation_id,
                    row_id=row_id,
                    fact=fact,
                ),
            )
            for index, (row_id, fact) in enumerate([*( (f"reversal:{i}", fact) for i, fact in enumerate(facts[: len(request.reversals)]) ), *[(f"replacement:{i}", fact) for i, fact in enumerate(request.replacements)]])
        ]
        projection = await self._project(request.account_id, records + new_records, now, new_event_ids=frozenset(r.id for r in new_records))
        if projection.confirmable_deficits:
            # The whole replay must stay valid: a reversal that strands a later
            # sell needs its companion facts in the same operation (plan 2.8).
            raise InvestorError("invalid_reference", "Correction would leave an invalid replay; include the companion corrections")
        snapshot = projection.snapshot
        final = new_records[-1]
        result = await self.session.execute(update(PortfolioAccount).where(PortfolioAccount.id == account.id, PortfolioAccount.owner_id == self.owner, PortfolioAccount.revision == request.expected_revision).values(revision=final.revision, current_snapshot_id=str(snapshot.id), payload=Account(id=request.account_id, name=account.payload["name"], currency="CNY", revision=final.revision, current_snapshot_id=snapshot.id).model_dump(mode="json")))
        if not result.rowcount:  # type: ignore[attr-defined]
            raise InvestorError("revision_conflict", "Concurrent portfolio append")
        for record in new_records:
            fact = record.request.fact
            self.session.add(
                PortfolioEvent(
                    id=str(record.id),
                    account_id=account.id,
                    revision=record.revision,
                    idempotency_key=str(record.request.idempotency_key),
                    batch_id=str(request.operation_id),
                    row_id=record.request.row_id,
                    reverses_id=str(fact.original_event_id) if isinstance(fact, Reversal) else None,
                    input_hash=body_hash,
                    fact_hash=digest(fact.model_dump(mode="json")),
                    payload=record.model_dump(mode="json"),
                )
            )
        self.session.add(PortfolioSnapshot(id=str(snapshot.id), account_id=account.id, revision=final.revision, payload=snapshot.model_dump(mode="json")))
        await self.session.flush()
        self.session.add(PortfolioRequest(account_id=account.id, idempotency_key=str(request.operation_id), event_id=str(final.id), input_hash=body_hash))
        await self.session.flush()
        return CorrectionResult(operation_id=request.operation_id, events=tuple(new_records), snapshot=snapshot)

    async def _project(
        self,
        account_id: UUID,
        records: list[EventRecord],
        recorded: datetime,
        *,
        new_event_ids: frozenset[UUID] = frozenset(),
        trace: dict[UUID, dict[str, Any]] | None = None,
    ) -> Projection:
        """The only projection entry point: owner of precision, records and trace.

        ``new_event_ids`` is kept for callers that already pass it (and for the
        signature the projection has always had); the replay names new rows
        through ``trace`` alone.
        """

        with localcontext() as context:
            # Bounded arithmetic precision, no binary floats or silent rounding.
            context.prec = max(128, 80 * len(records))
            engine = ProjectionEngine(SessionPriceLookup(self.session))
            return await engine.project(account_id, records, recorded, trace=trace)

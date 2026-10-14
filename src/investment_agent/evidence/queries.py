"""Ordinary Python query adapters. Read operations never enqueue or fetch upstream."""

from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.evidence.contracts import EvidenceRecord, ToolResult
from investment_agent.evidence.events import EventRow, event_material, event_model
from investment_agent.evidence.snapshots import materialize
from investment_agent.evidence.store import EvidenceService
from investment_agent.factors.field_sources import beyond_tolerance, default_field_sources
from investment_agent.investor.common import Contract, ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError, storage_conflicts
from investment_agent.snapshots.health import DataHealth, HealthSettings, build_data_health
from investment_agent.snapshots.jobs import (
    FINANCIAL_SNAPSHOT,
    MARKET_SNAPSHOT,
    FinancialSnapshotPayload,
    MarketSnapshotPayload,
)
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.reference import Company, Market
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    PipelineRunRepository,
)
from investment_agent.storage.repositories.watchlist import WatchlistRepository


class SecurityIdentity(Contract):
    listing_ref: ObjectRef
    ticker: str
    market: str
    company_name: str
    currency: str


class SnapshotQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve_security(self, ticker: str, as_of: datetime) -> ToolResult[SecurityIdentity]:
        as_of = utc_timestamp(as_of)
        try:
            listing = await ListingRepository(self.session).get_a_share_listing(ticker)
        except ValueError as exc:
            raise InvestorError("invalid_reference", "Canonical A-share ticker required") from exc
        if listing is None:
            return ToolResult(
                status="missing",
                data=None,
                refs=(),
                as_of=as_of,
                coverage="no stored listing; identity is never invented",
            )
        company = await self.session.get(Company, listing.company_id)
        market = await self.session.get(Market, listing.market_code)
        assert company is not None and market is not None
        ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        data = SecurityIdentity(
            listing_ref=ref,
            ticker=ticker,
            market=market.code,
            company_name=company.canonical_name,
            currency=market.currency,
        )
        return ToolResult(
            status="ok",
            data=data,
            refs=(ref,),
            as_of=as_of,
            coverage="current stored security identity; name is not historical",
        )

    async def get_snapshot(
        self,
        ticker: str,
        field: str,
        as_of: datetime,
        *,
        report_period: date | None = None,
        max_age: timedelta | None = None,
    ) -> ToolResult[tuple[EvidenceRecord, ...]]:
        as_of = utc_timestamp(as_of)
        identity = await self.resolve_security(ticker, as_of)
        if identity.data is None:
            return ToolResult(
                status="missing", data=None, refs=(), as_of=as_of, coverage=identity.coverage
            )
        listing_id = int(identity.data.listing_ref.id)
        registry = default_field_sources()
        source = registry.get(field)
        if source is None or source.table not in {"financial", "market"}:
            raise InvestorError(
                "invalid_reference", "Only registered direct snapshot fields are supported"
            )
        day = as_of.astimezone(SHANGHAI_TZ).date()
        records: list[EvidenceRecord] = []
        if source.table == "market":
            if report_period is not None:
                day = report_period
                if day > as_of.astimezone(SHANGHAI_TZ).date():
                    return ToolResult(
                        status="missing", data=None, refs=(), as_of=as_of, coverage="future period"
                    )
            rows = await MarketSnapshotRepository(self.session).series_by_provider(
                listing_id,
                day - timedelta(days=400) if report_period is None else day,
                day,
                as_of,
                adjust="none",
                include_backfill=False,
            )
            if rows:
                records = [materialize(row, field) for row in rows[max(rows)].values()]
        else:
            conditions = [
                FinancialSnapshot.listing_id == listing_id,
                FinancialSnapshot.available_from <= day,
                FinancialSnapshot.availability != "unknown",
                FinancialSnapshot.captured_at <= as_of,
                FinancialSnapshot.report_period <= day,
            ]
            if report_period is not None:
                conditions.append(FinancialSnapshot.report_period == report_period)
            rows_fin = list(
                await self.session.scalars(
                    select(FinancialSnapshot)
                    .where(*conditions)
                    .order_by(FinancialSnapshot.report_period, FinancialSnapshot.version)
                )
            )
            if rows_fin:
                period = max(row.report_period for row in rows_fin)
                latest: dict[tuple[str, str], FinancialSnapshot] = {}
                for row in rows_fin:
                    if row.report_period == period:
                        latest[(row.provider, str(row.fields.get("part", "")))] = row
                records = [materialize(row, field) for row in latest.values()]
        records = [
            record for record in records if max(record.available_from, record.acquired_at) <= as_of
        ]
        if not records:
            return ToolResult(
                status="missing",
                data=None,
                refs=(),
                as_of=as_of,
                coverage="no strictly visible source material",
            )
        known = [r for r in records if r.value is not None and r.value.value is not None]
        values = [r.value.value for r in known if r.value is not None and r.value.value is not None]
        conflict = any(r.conflict == "unresolved" for r in records) or any(
            beyond_tolerance(float(a), float(b), source.tolerance) for a in values for b in values
        )

        status: Literal["ok", "missing", "conflict", "stale"] = (
            "conflict"
            if conflict
            else "missing"
            if not known
            else "stale"
            if max_age is not None and as_of - max(r.available_from for r in records) > max_age
            else "ok"
        )
        return ToolResult(
            status=status,
            data=tuple(records),
            refs=tuple(r.source_ref for r in records),
            as_of=as_of,
            coverage="source candidates; strict acquisition/public time; no backfill reconstruction",
            warnings=tuple(dict.fromkeys(r.text or "" for r in records)),
        )

    async def compare_stocks(
        self,
        tickers: tuple[str, ...],
        field: str,
        as_of: datetime,
        *,
        report_period: date,
    ) -> ToolResult[dict[str, ToolResult[tuple[EvidenceRecord, ...]]]]:
        if not 2 <= len(set(tickers)) <= 20:
            raise InvestorError(
                "invalid_reference", "Comparison requires 2 to 20 distinct securities"
            )
        results = {
            ticker: await self.get_snapshot(ticker, field, as_of, report_period=report_period)
            for ticker in tickers
        }
        status: Literal["ok", "missing", "conflict"] = (
            "conflict"
            if any(r.status == "conflict" for r in results.values())
            else "missing"
            if any(r.status != "ok" for r in results.values())
            else "ok"
        )
        periods = {r.period for result in results.values() for r in result.data or ()}
        if len(periods) > 1:
            status = "conflict"
        return ToolResult(
            status=status,
            data=results,
            refs=tuple(ref for r in results.values() for ref in r.refs),
            as_of=as_of,
            coverage="same registered field/unit and exact requested period; no ranking",
        )

    async def get_data_health(
        self, config: HealthSettings, *, now: datetime
    ) -> ToolResult[DataHealth]:
        now = utc_timestamp(now)
        health = await build_data_health(
            self.session, config, now=now, today=now.astimezone(SHANGHAI_TZ).date()
        )
        return ToolResult(
            status="ok",
            data=health,
            refs=(),
            as_of=now,
            coverage="current operational health; not a historical evidence source",
        )

    async def get_event(self, ref: ObjectRef, as_of: datetime) -> ToolResult[EvidenceRecord]:
        as_of = utc_timestamp(as_of)
        model = event_model(ref.kind)
        if model is None or ref.revision != 1:
            raise InvestorError("invalid_reference", "A saved event reference is required")
        row = await self.session.get(model, ref.id)
        if not isinstance(row, EventRow):
            return ToolResult(
                status="missing", data=None, refs=(), as_of=as_of, coverage="event absent"
            )
        material = event_material(row, ref.field_path or "text")
        if max(material.available_from, material.acquired_at) > as_of:
            return ToolResult(
                status="missing", data=None, refs=(), as_of=as_of, coverage="event not yet visible"
            )
        return ToolResult(
            status="missing"
            if material.value is not None and material.value.value is None
            else "ok",
            data=material,
            refs=(material.source_ref,),
            as_of=as_of,
            coverage="saved event candidate; no original document verification",
            warnings=(material.text or "",),
        )

    @storage_conflicts
    async def capture_snapshot(
        self,
        user_id: OwnerId,
        ticker: str,
        field: str,
        as_of: datetime,
        operation_id: UUID,
        *,
        report_period: date | None = None,
    ) -> ToolResult[tuple[ObjectRef, ...]]:
        from uuid import uuid5

        store = EvidenceService(self.session)
        request = {
            "operation": "capture_snapshot",
            "ticker": ticker,
            "field": field,
            "as_of": utc_timestamp(as_of).isoformat(),
            "report_period": report_period.isoformat() if report_period else None,
        }
        async with self.session.begin_nested():
            replay = await store.receipts._replay(user_id, operation_id, request)
            if replay is not None:
                return ToolResult[tuple[ObjectRef, ...]].model_validate(replay)
            result = await self.get_snapshot(ticker, field, as_of, report_period=report_period)
            refs = []
            for record in result.data or ():
                key = uuid5(operation_id, record.content_hash)
                refs.append(await store.save_evidence(user_id, record, key))
            saved = ToolResult[tuple[ObjectRef, ...]](
                status=result.status,
                data=tuple(refs),
                refs=tuple(refs),
                as_of=as_of,
                coverage=result.coverage,
                warnings=result.warnings,
            )
            store.receipts._receipt(user_id, operation_id, request, saved)
            await self.session.flush()
        return saved


class RefreshService:
    """Only queue the existing worker; caller supplies executor-authorized scope."""

    def __init__(
        self, session: AsyncSession, *, authorized_scope: Literal["watchlist"] | None = None
    ) -> None:
        self.session = session
        self.authorized_scope = authorized_scope

    @storage_conflicts
    async def request_data_refresh(
        self,
        user_id: OwnerId,
        ticker: str,
        as_of: date,
        operation_id: UUID,
        *,
        report_period: date | None = None,
    ) -> ToolResult[ObjectRef]:
        listing = await ListingRepository(self.session).get_a_share_listing(ticker)
        watch = (
            await WatchlistRepository(self.session).get_by_listing(listing.id) if listing else None
        )
        if (
            listing is None
            or watch is None
            or not watch.enabled
            or self.authorized_scope != "watchlist"
        ):
            raise InvestorError(
                "invalid_reference", "Refresh is outside the executor-authorized listing scope"
            )
        payload: MarketSnapshotPayload | FinancialSnapshotPayload
        if report_period is None:
            payload = MarketSnapshotPayload(
                trade_date=as_of, provider="baostock", scope="watchlist"
            )
            kind = MARKET_SNAPSHOT
        else:
            if report_period > as_of:
                raise InvestorError(
                    "invalid_reference", "Refresh report period cannot be in the future"
                )
            payload = FinancialSnapshotPayload(
                report_period=report_period, as_of=as_of, provider="baostock", scope="watchlist"
            )
            kind = FINANCIAL_SNAPSHOT
        request = {
            "operation": "request_data_refresh",
            "ticker": ticker,
            "scope": self.authorized_scope,
            "as_of": as_of.isoformat(),
            "report_period": report_period.isoformat() if report_period else None,
            "kind": kind,
            "payload": payload.model_dump(mode="json"),
        }
        receipts = EvidenceService(self.session).receipts
        async with self.session.begin_nested():
            replay = await receipts._replay(user_id, operation_id, request)
            if replay is not None:
                return ToolResult[ObjectRef].model_validate(replay)
            try:
                run, _ = await PipelineRunRepository(self.session).enqueue(
                    kind=kind,
                    idempotency_key=f"investor:{user_id}:{operation_id}",
                    triggered_by="investor",
                    listing_id=listing.id,
                    request_json=payload.model_dump(mode="json"),
                )
            except IdempotencyPayloadMismatchError as exc:
                raise InvestorError(
                    "idempotency_conflict", "Refresh key was used for different parameters"
                ) from exc
            ref = ObjectRef(kind="pipeline_run", id=run.id, revision=1)
            result = ToolResult[ObjectRef](
                status="pending",
                data=ref,
                refs=(ref,),
                as_of=datetime.now(UTC),
                coverage="queued baostock collection of the authorized watchlist scope; existing worker",
                dependency_id=f"pipeline_run:{run.id}",
            )
            receipts._receipt(user_id, operation_id, request, result)
            await self.session.flush()
        return result

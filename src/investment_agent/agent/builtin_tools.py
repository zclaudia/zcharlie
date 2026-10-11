"""Small trusted adapters for AG05's existing queries and watchlist refresh.

No arbitrary URLs, SQL, code, publication or approval capabilities.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import ExecutionDeniedError
from investment_agent.agent.tools import ToolRegistration
from investment_agent.evidence.queries import RefreshService, SnapshotQueryService
from investment_agent.factors.field_sources import default_field_sources
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.repositories.listing import ListingRepository


class SnapshotParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    field: str = Field(json_schema_extra={"enum": [name for name, source in default_field_sources().fields.items() if source.table in {"financial", "market"}]})
    as_of: datetime
    report_period: date | None = None

    @field_validator("field")
    @classmethod
    def registered_field(cls, value: str) -> str:
        source = default_field_sources().get(value)
        if source is None or source.table not in {"financial", "market"}:
            raise ValueError("Use an exact registered direct snapshot field from the schema enum")
        return value


class RefreshParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    as_of: date
    report_period: date | None = None


async def listing_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
    assert isinstance(business, (SnapshotParameters, RefreshParameters))
    listing = await ListingRepository(session).get_a_share_listing(business.ticker)
    if listing is None:
        raise ExecutionDeniedError("listing_unavailable")
    return (ObjectRef(kind="listing", id=listing.id, revision=1),)


async def snapshot(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
    del operation
    assert isinstance(business, SnapshotParameters)
    return (await SnapshotQueryService(session).get_snapshot(business.ticker, business.field,
        business.as_of, report_period=business.report_period)).model_dump(mode="json")


async def refresh(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
    assert isinstance(business, RefreshParameters)
    return (await RefreshService(session, authorized_scope="watchlist").request_data_refresh(
        "local_owner", business.ticker, business.as_of, operation,
        report_period=business.report_period)).model_dump(mode="json")


def builtin_registrations() -> tuple[ToolRegistration, ...]:
    from investment_agent.evidence.search import SavedComparisonRequest, SearchRequest
    return (
        ToolRegistration("compare_saved_sources", SavedComparisonRequest, saved_comparison_scope, database=saved_comparison, description="Compare exact saved evidence references only when verified, current, same period/unit/currency/metric/reporting scope. Mismatch explicitly not comparable; never hard ranking."),
        ToolRegistration("search_saved_sources", SearchRequest, search_scope, database=search_sources, description="Bounded literal keyword search of already saved authorized listing evidence and parse manifests at task cutoff. Exact refs, hashes and adjacent context; discovery leads only, relevance is not verification. No network or parsing. Same-original copies never increase independent support."),
        ToolRegistration("get_evidence", EvidenceParameters, evidence_scope, database=evidence, description="Read an existing saved evidence ref (kind evidence, UUID id), never listing/case. First discover stored fields with get_snapshot and save a candidate with capture_snapshot; use its returned evidence ref. subject_ref must be the exact authorized listing."),
        ToolRegistration("get_event", EventParameters, event_scope, database=event),
        ToolRegistration("compare_stocks", ComparisonParameters, comparison_scope, database=comparison),
        ToolRegistration("get_snapshot", SnapshotParameters, listing_scope, database=snapshot, description="Discover one stored field for a canonical ticker (e.g. 600036.SH), at task cutoff. Fields include close, roe, operating_cash_flow, capital_expenditure, net_profit_parent, net_profit_total, revenue_main, revenue_total. This is read-only provider material, not saved verified evidence. Use capture_snapshot to obtain a saved evidence ref before citing."),
        ToolRegistration("request_data_refresh", RefreshParameters, listing_scope,
                         database=refresh, whole_watchlist=True),
    )


class EvidenceRef(ObjectRef):
    kind: Literal["evidence"] = "evidence"
    id: UUID


class ListingRef(ObjectRef):
    kind: Literal["listing"] = "listing"
    id: Annotated[int, Field(strict=True, ge=1)]


class EventRef(ObjectRef):
    kind: Literal["earnings_forecast", "disclosure_schedule", "business_segment"]


class EvidenceParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: EvidenceRef
    subject_ref: ListingRef
    as_of: datetime


class EventParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: EventRef
    subject_ref: ListingRef
    as_of: datetime


class ComparisonParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tickers: tuple[str, ...]
    field: str
    as_of: datetime
    report_period: date


async def evidence_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
    del session
    assert isinstance(business, EvidenceParameters)
    return (ObjectRef.model_validate(business.subject_ref.model_dump()),)


async def evidence(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
    from investment_agent.evidence.store import EvidenceService
    del operation
    assert isinstance(business, EvidenceParameters)
    return (await EvidenceService(session).get_evidence("local_owner", ObjectRef.model_validate(business.ref.model_dump()),
        business.as_of, subject_ref=ObjectRef.model_validate(business.subject_ref.model_dump()))).model_dump(mode="json")


async def event_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
    assert isinstance(business, EventParameters)
    result = await SnapshotQueryService(session).get_event(business.ref, business.as_of)
    if result.data is not None:
        return (result.data.subject_ref, ObjectRef.model_validate(business.subject_ref.model_dump()))
    return (ObjectRef.model_validate(business.subject_ref.model_dump()),)


async def event(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
    del operation
    assert isinstance(business, EventParameters)
    result = await SnapshotQueryService(session).get_event(business.ref, business.as_of)
    if result.data is not None and result.data.subject_ref != ObjectRef.model_validate(business.subject_ref.model_dump()):
        raise ExecutionDeniedError("event_subject_mismatch")
    return result.model_dump(mode="json")


async def comparison_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
    assert isinstance(business, ComparisonParameters)
    refs = []
    for ticker in business.tickers:
        listing = await ListingRepository(session).get_a_share_listing(ticker)
        if listing is None:
            raise ExecutionDeniedError("listing_unavailable")
        refs.append(ObjectRef(kind="listing", id=listing.id, revision=1))
    return tuple(refs)


async def comparison(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
    del operation
    assert isinstance(business, ComparisonParameters)
    return (await SnapshotQueryService(session).compare_stocks(business.tickers,
        business.field, business.as_of, report_period=business.report_period)).model_dump(mode="json")


async def search_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
    from investment_agent.evidence.search import SearchRequest
    del session
    assert isinstance(business, SearchRequest)
    return tuple(ObjectRef(kind="listing", id=i, revision=1) for i in business.listing_ids)


async def search_sources(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
    from investment_agent.evidence.search import SearchRequest, search_saved
    del operation
    assert isinstance(business, SearchRequest)
    return await search_saved(session, business)


async def saved_comparison_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
    from investment_agent.evidence.search import SavedComparisonRequest
    del session
    assert isinstance(business, SavedComparisonRequest)
    return tuple(ObjectRef(kind="listing", id=i.listing_id, revision=1) for i in business.items)


async def saved_comparison(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
    from investment_agent.evidence.search import SavedComparisonRequest, compare_saved
    del operation
    assert isinstance(business, SavedComparisonRequest)
    return await compare_saved(session, business)

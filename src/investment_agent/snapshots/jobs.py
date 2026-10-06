"""Snapshot job kinds, request payloads, idempotency keys and enqueueing."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.providers.baostock_reference import IndexCode
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.universe import UniverseRepository

UNIVERSE_SYNC = "universe_sync"
INDUSTRY_SYNC = "industry_sync"
MARKET_SNAPSHOT = "market_snapshot"
FINANCIAL_SNAPSHOT = "financial_snapshot"
SNAPSHOT_KINDS: tuple[str, ...] = (
    UNIVERSE_SYNC,
    INDUSTRY_SYNC,
    MARKET_SNAPSHOT,
    FINANCIAL_SNAPSHOT,
)

PeriodKind = Literal["annual", "semi", "q1", "q3"]
_PERIOD_KINDS: dict[tuple[int, int], PeriodKind] = {
    (3, 31): "q1",
    (6, 30): "semi",
    (9, 30): "q3",
    (12, 31): "annual",
}


def period_kind(report_period: date) -> PeriodKind:
    """``2026-06-30`` -> ``semi``; raises for a non quarter-end date."""

    kind = _PERIOD_KINDS.get((report_period.month, report_period.day))
    if kind is None:
        raise ValueError(
            f"report period must be a quarter end (03-31/06-30/09-30/12-31): "
            f"{report_period.isoformat()}"
        )
    return kind


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseSyncPayload(_Payload):
    universe_code: IndexCode
    as_of: date


class IndustrySyncPayload(_Payload):
    as_of: date
    universe_codes: tuple[IndexCode, ...] = ()
    """Only classify members of these universes (as of ``as_of``); empty = whole market."""


class MarketSnapshotPayload(_Payload):
    universe_code: IndexCode
    trade_date: date


class FinancialSnapshotPayload(_Payload):
    universe_code: IndexCode
    report_period: date
    as_of: date
    """Membership date; also the day the job was scheduled for."""

    @field_validator("report_period")
    @classmethod
    def _quarter_end(cls, value: date) -> date:
        period_kind(value)
        return value


SnapshotPayload = (
    UniverseSyncPayload | IndustrySyncPayload | MarketSnapshotPayload | FinancialSnapshotPayload
)

PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    UNIVERSE_SYNC: UniverseSyncPayload,
    INDUSTRY_SYNC: IndustrySyncPayload,
    MARKET_SNAPSHOT: MarketSnapshotPayload,
    FINANCIAL_SNAPSHOT: FinancialSnapshotPayload,
}


def payload_kind(payload: SnapshotPayload) -> str:
    for kind, model in PAYLOAD_MODELS.items():
        if type(payload) is model:
            return kind
    raise TypeError(f"not a snapshot payload: {type(payload).__name__}")


def idempotency_key(payload: SnapshotPayload) -> str:
    """kind + universe + date (+ period): one run per scheduled slot."""

    kind = payload_kind(payload)
    if isinstance(payload, UniverseSyncPayload):
        return f"{kind}:{payload.universe_code}:{payload.as_of.isoformat()}"
    if isinstance(payload, IndustrySyncPayload):
        scope = "+".join(sorted(payload.universe_codes)) or "ALL"
        return f"{kind}:{scope}:{payload.as_of.isoformat()}"
    if isinstance(payload, MarketSnapshotPayload):
        return f"{kind}:{payload.universe_code}:{payload.trade_date.isoformat()}"
    return (
        f"{kind}:{payload.universe_code}:{payload.report_period.isoformat()}:"
        f"{payload.as_of.isoformat()}"
    )


def payload_universe(payload: SnapshotPayload) -> str | None:
    return getattr(payload, "universe_code", None)


async def enqueue_snapshot_job(
    session: AsyncSession,
    payload: SnapshotPayload,
    *,
    triggered_by: str,
    key: str | None = None,
    max_attempts: int = 3,
) -> tuple[PipelineRun, bool]:
    """Queue one snapshot job; an existing run with the same key is returned."""

    universe_code = payload_universe(payload)
    universes = UniverseRepository(session)
    if universe_code is not None:
        await universes.ensure_universe(universe_code)
    return await PipelineRunRepository(session).enqueue(
        kind=payload_kind(payload),
        idempotency_key=key or idempotency_key(payload),
        triggered_by=triggered_by,
        universe_code=universe_code,
        request_json=payload.model_dump(mode="json"),
        max_attempts=max_attempts,
    )

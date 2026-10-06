"""Snapshot job kinds, request payloads, idempotency keys and enqueueing.

Market and financial payloads carry a ``provider`` and a ``scope`` (decision
D6): baostock serves the target pool (``scope="universe"`` + a
``universe_code``) and may also serve the watchlist; iFinD serves only the
watchlist (``scope="watchlist"``, enabled watchlist items), so its limited
gateway quota is never spent on breadth collection. Payloads persisted before
P1-A7 (no provider / scope) validate as baostock + universe.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.providers.baostock_reference import IndexCode
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.universe import UniverseRepository

UNIVERSE_SYNC = "universe_sync"
INDUSTRY_SYNC = "industry_sync"
MARKET_SNAPSHOT = "market_snapshot"
FINANCIAL_SNAPSHOT = "financial_snapshot"
SNAPSHOT_BACKFILL = "snapshot_backfill"
SNAPSHOT_KINDS: tuple[str, ...] = (
    UNIVERSE_SYNC,
    INDUSTRY_SYNC,
    MARKET_SNAPSHOT,
    FINANCIAL_SNAPSHOT,
    SNAPSHOT_BACKFILL,
)

SnapshotProvider = Literal["baostock", "ifind"]
SnapshotScope = Literal["universe", "watchlist"]
SNAPSHOT_PROVIDERS: tuple[SnapshotProvider, ...] = ("baostock", "ifind")
SNAPSHOT_SCOPES: tuple[SnapshotScope, ...] = ("universe", "watchlist")
WATCHLIST_SCOPE_LABEL = "watchlist"

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


IndustrySystem = Literal["csrc", "sw2021"]
INDUSTRY_SYSTEMS: tuple[IndustrySystem, ...] = ("csrc", "sw2021")


class IndustrySyncPayload(_Payload):
    as_of: date
    universe_codes: tuple[IndexCode, ...] = ()
    """Only classify members of these universes (as of ``as_of``); empty = whole market
    (CSRC creates the listings; Shenwan only updates existing listings)."""
    systems: tuple[IndustrySystem, ...] = INDUSTRY_SYSTEMS
    """Classification systems to sync: ``csrc`` (baostock) and / or ``sw2021`` (AkShare)."""

    @field_validator("systems")
    @classmethod
    def _systems(cls, value: tuple[IndustrySystem, ...]) -> tuple[IndustrySystem, ...]:
        if not value:
            raise ValueError("at least one industry system is required")
        return tuple(system for system in INDUSTRY_SYSTEMS if system in value)


class _CollectionPayload(_Payload):
    provider: SnapshotProvider = "baostock"
    scope: SnapshotScope = "universe"
    universe_code: IndexCode | None = None
    """Required for ``scope="universe"``; must be empty for the watchlist."""

    @model_validator(mode="after")
    def _routing(self) -> Self:
        if self.scope == "universe" and self.universe_code is None:
            raise ValueError("scope 'universe' needs a universe_code")
        if self.scope == "watchlist" and self.universe_code is not None:
            raise ValueError("scope 'watchlist' takes no universe_code")
        if self.provider == "ifind" and self.scope != "watchlist":
            raise ValueError(
                "iFinD collection only serves the watchlist scope (D6); "
                "target pools are collected with provider 'baostock'"
            )
        return self

    @property
    def scope_label(self) -> str:
        """``CSI300`` for a universe, ``watchlist`` otherwise."""

        return self.universe_code or WATCHLIST_SCOPE_LABEL


class MarketSnapshotPayload(_CollectionPayload):
    trade_date: date


class FinancialSnapshotPayload(_CollectionPayload):
    report_period: date
    as_of: date
    """Membership date; also the day the job was scheduled for."""

    @field_validator("report_period")
    @classmethod
    def _quarter_end(cls, value: date) -> date:
        period_kind(value)
        return value


BackfillPart = Literal["membership", "industry", "prices", "financials"]
BACKFILL_PARTS: tuple[BackfillPart, ...] = ("membership", "industry", "prices", "financials")
BACKFILL_CHUNKED_PARTS: frozenset[str] = frozenset({"prices", "financials"})
_CANONICAL_TICKER = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class SnapshotBackfillPayload(_Payload):
    """One unit of the historical backfill (P1-A4, decision D5; all via baostock).

    ``membership`` and ``industry`` are single jobs for the whole universe;
    ``prices`` and ``financials`` run per chunk of ``symbols``. Depths are part
    of the payload so a plan is reproducible.
    """

    universe_code: IndexCode
    as_of: date
    part: BackfillPart
    symbols: tuple[str, ...] = ()
    chunk: int | None = None
    price_years: int = Field(default=5, ge=1, le=20)
    annual_years: int = Field(default=6, ge=0, le=20)
    quarters: int = Field(default=4, ge=0, le=12)

    @model_validator(mode="after")
    def _chunking(self) -> Self:
        if self.part in BACKFILL_CHUNKED_PARTS:
            if not self.symbols or self.chunk is None:
                raise ValueError(f"backfill part {self.part!r} needs symbols and a chunk index")
        elif self.symbols or self.chunk is not None:
            raise ValueError(f"backfill part {self.part!r} takes no symbols / chunk")
        bad = [symbol for symbol in self.symbols if not _CANONICAL_TICKER.fullmatch(symbol)]
        if bad:
            raise ValueError(f"not canonical A-share tickers: {bad[:5]}")
        return self

    @property
    def depth_label(self) -> str:
        return f"p{self.price_years}a{self.annual_years}q{self.quarters}"


SnapshotPayload = (
    UniverseSyncPayload
    | IndustrySyncPayload
    | MarketSnapshotPayload
    | FinancialSnapshotPayload
    | SnapshotBackfillPayload
)

PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    UNIVERSE_SYNC: UniverseSyncPayload,
    INDUSTRY_SYNC: IndustrySyncPayload,
    MARKET_SNAPSHOT: MarketSnapshotPayload,
    FINANCIAL_SNAPSHOT: FinancialSnapshotPayload,
    SNAPSHOT_BACKFILL: SnapshotBackfillPayload,
}


def payload_kind(payload: SnapshotPayload) -> str:
    for kind, model in PAYLOAD_MODELS.items():
        if type(payload) is model:
            return kind
    raise TypeError(f"not a snapshot payload: {type(payload).__name__}")


def idempotency_key(payload: SnapshotPayload) -> str:
    """kind (+ provider) + scope + date (+ period): one run per scheduled slot."""

    kind = payload_kind(payload)
    if isinstance(payload, UniverseSyncPayload):
        return f"{kind}:{payload.universe_code}:{payload.as_of.isoformat()}"
    if isinstance(payload, IndustrySyncPayload):
        scope = "+".join(sorted(payload.universe_codes)) or "ALL"
        key = f"{kind}:{scope}:{payload.as_of.isoformat()}"
        if payload.systems != INDUSTRY_SYSTEMS:
            key += ":" + "+".join(payload.systems)
        return key
    if isinstance(payload, SnapshotBackfillPayload):
        key = (
            f"{kind}:{payload.universe_code}:{payload.as_of.isoformat()}:"
            f"{payload.part}:{payload.depth_label}"
        )
        return key if payload.chunk is None else f"{key}:c{payload.chunk:03d}"
    if isinstance(payload, MarketSnapshotPayload):
        return (
            f"{kind}:{payload.provider}:{payload.scope_label}:{payload.trade_date.isoformat()}"
        )
    return (
        f"{kind}:{payload.provider}:{payload.scope_label}:"
        f"{payload.report_period.isoformat()}:{payload.as_of.isoformat()}"
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

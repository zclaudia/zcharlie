"""Trusted execution proofs and explicitly scoped free-source collection.

Collectors never assert exchange-rule readiness. A trusted HTTP/admin adapter
must import session evidence separately; models must never receive that writer.
"""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import localcontext
from pathlib import Path
from typing import Literal, Protocol, Self
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.domain.time import as_utc
from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.providers.baostock_market import (
    DailyBarSeries,
    DividendYear,
    QuarterlyFinancials,
    StockBasic,
)
from investment_agent.scheduler.trading_calendar import TradingCalendar, load_trading_calendar
from investment_agent.simulation.contracts import Identifier, PositiveMoney
from investment_agent.snapshots.breadth import store_daily_bars, store_financial
from investment_agent.snapshots.dividends import store_dividend_year
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ExecutionEvidenceInput(Contract):
    """Explicit per-session facts. False/absent coverage can never become ready."""

    listing_id: int = Field(strict=True, gt=0)
    trade_date: date
    exchange: Literal["SSE", "SZSE"]
    board: Literal["mainboard"]
    security_type: Literal["ordinary_a"]
    ipo_date: date
    ordinary_limit_session: bool = Field(strict=True)
    reference_price: PositiveMoney
    upper_limit: PositiveMoney
    lower_limit: PositiveMoney
    tick_size: PositiveMoney
    lot_size: int = Field(strict=True, gt=0)
    is_st: bool = Field(strict=True)
    rule_version: Identifier
    rule_source: Identifier
    rule_verified: bool = Field(strict=True)
    corporate_actions_complete: bool = Field(strict=True)
    corporate_action_refs: tuple[Identifier, ...] = ()
    unsupported_corporate_actions: tuple[Identifier, ...] = ()
    published_at: datetime
    captured_at: datetime

    @model_validator(mode="after")
    def valid_prices(self) -> Self:
        if self.ipo_date > self.trade_date:
            raise ValueError("IPO date follows session")
        if not self.lower_limit <= self.reference_price <= self.upper_limit:
            raise ValueError("legal price interval does not contain reference")
        if self.lower_limit == self.upper_limit:
            raise ValueError("legal price interval is empty")
        # PositiveMoney permits 40 significant digits; the process Decimal
        # context (usually 28) must not round or reject valid tick arithmetic.
        with localcontext() as context:
            context.prec = 100
            if any(
                price % self.tick_size
                for price in (self.reference_price, self.upper_limit, self.lower_limit)
            ):
                raise ValueError("prices must align with tick")
        if len(set(self.corporate_action_refs)) != len(self.corporate_action_refs):
            raise ValueError("duplicate corporate action reference")
        if len(set(self.unsupported_corporate_actions)) != len(self.unsupported_corporate_actions):
            raise ValueError("duplicate unsupported corporate action reference")
        return self


class ExecutionEligibility(Contract):
    ready: bool
    missing: tuple[str, ...]
    evidence_id: str | None = None
    evidence_hash: str | None = None
    evidence: ExecutionEvidenceInput | None = None

    @model_validator(mode="after")
    def explicit_readiness(self) -> Self:
        if self.ready != (not self.missing):
            raise ValueError("readiness must match explicit blockers")
        if self.ready and (
            self.evidence is None or self.evidence_id is None or self.evidence_hash is None
        ):
            raise ValueError("ready execution requires pinned proof")
        return self


def proof_missing(proof: ExecutionEvidenceInput) -> tuple[str, ...]:
    missing = []
    if not proof.rule_verified:
        missing.append("rule_unverified")
    if not proof.ordinary_limit_session or proof.ipo_date == proof.trade_date:
        missing.append("special_session_unsupported")
    if not proof.corporate_actions_complete:
        missing.append("corporate_action_coverage_missing")
    if proof.unsupported_corporate_actions:
        missing.append("corporate_action_unsupported")
    return tuple(missing)


async def validate_listing_scope(
    session: AsyncSession, listing_ids: Sequence[int]
) -> dict[int, str]:
    if (
        not listing_ids
        or len(set(listing_ids)) != len(listing_ids)
        or any(type(value) is not int or value <= 0 for value in listing_ids)
    ):
        raise ValueError("nonempty unique controlled listing IDs required")
    rows = (await session.scalars(select(Listing).where(Listing.id.in_(listing_ids)))).all()
    result = {}
    for row in rows:
        ticker = format_listing_ticker(ticker=row.ticker, market_code=row.market_code)
        # Prefix screening is only scope admission; it does not prove trading rules.
        if not re.fullmatch(r"\d{6}\.(SH|SZ)", ticker) or not (
            ticker.endswith(".SH")
            and ticker[:3] in {"600", "601", "603", "605"}
            or ticker.endswith(".SZ")
            and ticker[:3] in {"000", "001", "002", "003"}
        ):
            raise ValueError(f"unsupported_mainboard_listing:{row.id}")
        result[row.id] = ticker
    if set(result) != set(listing_ids):
        raise ValueError("unknown_listing")
    return result


async def import_execution_evidence(
    session: AsyncSession,
    *,
    experiment_id: str,
    allowed_listing_ids: Sequence[int],
    evidence: ExecutionEvidenceInput,
    source_kind: Literal["user", "provider"],
    source_ref: str,
    imported_at: datetime,
) -> str:
    """Trusted adapter only. Owns no commit; caller serializes imports transactionally."""
    from investment_agent.storage.models.simulation import (
        SimulationExecutionEvidenceRecord,
        SimulationExperimentRecord,
    )

    imported_at = utc_timestamp(imported_at)
    if source_kind not in {"user", "provider"} or not source_ref.strip():
        raise ValueError("trusted evidence provenance required")
    if evidence.listing_id not in allowed_listing_ids:
        raise ValueError("evidence_outside_experiment_scope")
    experiment = await session.get(SimulationExperimentRecord, experiment_id)
    if experiment is None or not set(allowed_listing_ids) <= set(
        experiment.config.get("universe", [])
    ):
        raise ValueError("evidence_outside_experiment_scope")
    listings = await validate_listing_scope(session, allowed_listing_ids)
    expected = "SSE" if listings[evidence.listing_id].endswith(".SH") else "SZSE"
    if evidence.exchange != expected:
        raise ValueError("evidence_exchange_mismatch")
    # Revalidate at this trust boundary even if a caller used model_copy().
    evidence = ExecutionEvidenceInput.model_validate(evidence.model_dump())
    if max(evidence.published_at, evidence.captured_at) > imported_at:
        raise ValueError("evidence_imported_before_available")
    payload = evidence.model_dump(mode="json")
    digest = content_hash(
        {
            "experiment_id": experiment_id,
            "source_kind": source_kind,
            "source_ref": source_ref,
            "payload": payload,
        }
    )
    existing = await session.scalar(
        select(SimulationExecutionEvidenceRecord).where(
            SimulationExecutionEvidenceRecord.experiment_id == experiment_id,
            SimulationExecutionEvidenceRecord.content_hash == digest,
        )
    )
    if existing:
        return str(existing.id)
    revision = await session.scalar(
        select(func.max(SimulationExecutionEvidenceRecord.revision)).where(
            SimulationExecutionEvidenceRecord.experiment_id == experiment_id,
            SimulationExecutionEvidenceRecord.listing_id == evidence.listing_id,
            SimulationExecutionEvidenceRecord.trade_date == evidence.trade_date,
        )
    )
    row = SimulationExecutionEvidenceRecord(
        id=str(uuid4()),
        experiment_id=experiment_id,
        listing_id=evidence.listing_id,
        trade_date=evidence.trade_date,
        revision=(revision or 0) + 1,
        source_kind=source_kind,
        source_ref=source_ref,
        captured_at=imported_at,
        content_hash=digest,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    return str(row.id)


async def execution_eligibility(
    session: AsyncSession,
    *,
    experiment_id: str,
    listing_id: int,
    trade_date: date,
    cutoff: datetime,
    evidence_id: str | None = None,
) -> ExecutionEligibility:
    """Pin evidence_id after freezing; never replace an old round's proof with latest."""
    from investment_agent.storage.models.simulation import (
        SimulationExecutionEvidenceRecord,
        SimulationExperimentRecord,
    )

    cutoff = utc_timestamp(cutoff)
    query = select(SimulationExecutionEvidenceRecord).where(
        SimulationExecutionEvidenceRecord.experiment_id == experiment_id,
        SimulationExecutionEvidenceRecord.listing_id == listing_id,
        SimulationExecutionEvidenceRecord.trade_date == trade_date,
        SimulationExecutionEvidenceRecord.captured_at <= cutoff,
    )
    if evidence_id is not None:
        query = query.where(SimulationExecutionEvidenceRecord.id == evidence_id)
    row = await session.scalar(
        query.order_by(SimulationExecutionEvidenceRecord.revision.desc()).limit(1)
    )
    if row is None:
        return ExecutionEligibility(ready=False, missing=("execution_evidence_missing",))
    experiment = await session.get(SimulationExperimentRecord, experiment_id)
    if experiment is None or listing_id not in experiment.config.get("universe", []):
        raise ValueError("execution_evidence_outside_experiment_scope")
    listing = await validate_listing_scope(session, [listing_id])
    proof = ExecutionEvidenceInput.model_validate(row.payload)
    expected_exchange = "SSE" if listing[listing_id].endswith(".SH") else "SZSE"
    if proof.exchange != expected_exchange:
        raise ValueError("execution_evidence_exchange_mismatch")
    if proof.listing_id != row.listing_id or proof.trade_date != row.trade_date:
        raise ValueError("execution_evidence_scope_mismatch")
    if row.source_kind not in {"user", "provider"} or not row.source_ref.strip():
        raise ValueError("execution_evidence_provenance_invalid")
    if max(proof.captured_at, proof.published_at) > as_utc(row.captured_at):
        raise ValueError("execution_evidence_imported_before_available")
    if (
        content_hash(
            {
                "experiment_id": row.experiment_id,
                "source_kind": row.source_kind,
                "source_ref": row.source_ref,
                "payload": proof.model_dump(mode="json"),
            }
        )
        != row.content_hash
    ):
        raise ValueError("execution_evidence_hash_mismatch")
    missing = list(proof_missing(proof))
    if max(proof.captured_at, proof.published_at) > cutoff:
        missing.append("execution_evidence_after_cutoff")
    return ExecutionEligibility(
        ready=not missing,
        missing=tuple(missing),
        evidence_id=row.id,
        evidence_hash=row.content_hash,
        evidence=proof,
    )


def reviewed_calendar(path: Path) -> TradingCalendar:
    return load_trading_calendar(path)


def next_trading_day(calendar: TradingCalendar, day: date) -> date:
    for offset in range(1, 367):
        candidate = day + timedelta(days=offset)
        if calendar.is_trading_day(candidate):
            return candidate
    raise ValueError("next trading day not found")


class ExperimentCollector(Protocol):
    async def fetch_daily_bars(
        self, symbol: str, start: date, end: date, adjust: Literal["none"]
    ) -> DailyBarSeries: ...
    async def fetch_stock_basic(self, symbol: str) -> StockBasic | None: ...
    async def fetch_dividends(self, symbol: str, year: int) -> DividendYear: ...
    async def fetch_quarterly_financials(
        self, symbol: str, year: int, quarter: int
    ) -> QuarterlyFinancials | None: ...


async def collect_experiment_data(
    session: AsyncSession,
    *,
    listing_ids: Sequence[int],
    start: date,
    end: date,
    client: ExperimentCollector,
    archive: Callable[[str, object], str],
    financial_periods: Sequence[tuple[int, int]] = (),
) -> dict[str, object]:
    """Exact experiment IDs, free baostock API seam, no watchlist mutation.

    Caller supplies an opened client session and an artifact writer. Returning
    missing rule flags is intentional: bars/dividends/IPO facts prove no limits.
    """
    if end < start:
        raise ValueError("invalid_collection_window")
    if any(
        not 2000 <= year <= end.year or quarter not in {1, 2, 3, 4}
        for year, quarter in financial_periods
    ):
        raise ValueError("invalid_financial_collection_period")
    listings = await validate_listing_scope(session, listing_ids)

    def archive_capture(name: str, payload: object) -> str:
        # A previous snapshot's raw_ref must continue to resolve to its bytes.
        return archive(f"{name}-{content_hash(payload)}", payload)

    inserted = 0
    basic_refs = {}
    missing = {"trusted_session_rule_evidence", "complete_corporate_action_evidence"}
    for listing_id, ticker in sorted(listings.items()):
        basic = await client.fetch_stock_basic(ticker)
        if basic is not None and basic.ticker != ticker:
            raise ValueError("collector_security_scope_mismatch")
        if basic is None or basic.ipo_date is None:
            missing.add(f"ipo_date_missing:{listing_id}")
        basic_refs[str(listing_id)] = archive_capture(
            f"stock-basic-{listing_id}",
            {
                "ticker": ticker,
                "ipo_date": basic.ipo_date.isoformat() if basic and basic.ipo_date else None,
                "out_date": basic.out_date.isoformat() if basic and basic.out_date else None,
            },
        )
        series = await client.fetch_daily_bars(ticker, start, end, "none")
        if (
            series.ticker != ticker
            or series.adjust != "none"
            or series.start != start
            or series.end != end
            or len({bar.trade_date for bar in series.bars}) != len(series.bars)
            or any(not start <= b.trade_date <= end for b in series.bars)
        ):
            raise ValueError("collector_market_scope_mismatch")
        raw_ref = archive_capture(f"market-{listing_id}", list(series.raw_rows))
        inserted += await store_daily_bars(
            MarketSnapshotRepository(session), listing_id=listing_id, series=series, raw_ref=raw_ref
        )
        for year in range(start.year, end.year + 1):
            dividends = await client.fetch_dividends(ticker, year)
            if dividends.ticker != ticker or dividends.year != year:
                raise ValueError("collector_dividend_scope_mismatch")
            raw_ref = archive_capture(f"dividends-{listing_id}-{year}", list(dividends.raw_rows))
            await store_dividend_year(
                DividendRepository(session),
                listing_id=listing_id,
                answer=dividends,
                raw_ref=raw_ref,
            )
            if dividends.no_ex_date:
                missing.add(f"unresolved_dividend_plan:{listing_id}:{year}")
        for year, quarter in sorted(set(financial_periods)):
            financial = await client.fetch_quarterly_financials(ticker, year, quarter)
            if financial is None:
                missing.add(f"financial_missing:{listing_id}:{year}:q{quarter}")
                continue
            expected_period = date(year, quarter * 3, 31 if quarter in {1, 4} else 30)
            if (
                financial.ticker != ticker
                or financial.report_period != expected_period
                or financial.stat_date != expected_period
            ):
                raise ValueError("collector_financial_scope_mismatch")
            raw_ref = archive_capture(
                f"financial-{listing_id}-{year}-q{quarter}", financial.raw_rows
            )
            await store_financial(
                FinancialSnapshotRepository(session),
                listing_id=listing_id,
                record=financial,
                raw_ref=raw_ref,
            )
    return {
        "inserted_bars": inserted,
        "listing_ids": sorted(listings),
        "stock_basic_refs": basic_refs,
        "missing": sorted(missing),
        "ready": False,
    }

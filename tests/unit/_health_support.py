"""Seeding helpers for the data health (P1-E2) tests: rows exactly as collectors write them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.snapshots.health import HealthSettings, HealthWarning
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import (
    DividendCoverage,
    FinancialSnapshot,
    IndustryClassification,
    MarketSnapshot,
    ReportOverdue,
)
from investment_agent.storage.models.reference import UniverseMembership
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.universe import UniverseRepository

CALENDAR = load_trading_calendar(Path("config/calendars/a_share.yaml"))
UNIVERSE = "CSI300"


def health_settings(
    *,
    universes: tuple[str, ...] = (UNIVERSE,),
    threshold: float = 0.95,
    budget_cap: int = 150,
    calendar_warnings: tuple[HealthWarning, ...] = (),
) -> HealthSettings:
    return HealthSettings(
        universes=universes,
        threshold=threshold,
        budget_cap=budget_cap,
        budget_window=timedelta(hours=5),
        calendar=CALENDAR,
        calendar_warnings=calendar_warnings,
    )


def ticker(index: int) -> str:
    return f"{600000 + index:06d}.SH"


async def seed_members(
    session: AsyncSession,
    count: int,
    *,
    universe: str = UNIVERSE,
    valid_from: date = date(2026, 1, 5),
    first: int = 0,
) -> list[int]:
    await UniverseRepository(session).ensure_universe(universe)
    listings = ListingRepository(session)
    ids: list[int] = []
    for index in range(first, first + count):
        listing = await listings.ensure_a_share_listing(ticker(index))
        session.add(
            UniverseMembership(
                universe_code=universe,
                listing_id=listing.id,
                valid_from=valid_from,
                valid_to=None,
                source="baostock",
                captured_at=datetime(2026, 1, 5, tzinfo=UTC),
            )
        )
        ids.append(listing.id)
    await session.flush()
    return ids


def completeness(
    expected: int,
    collected: int,
    missing: dict[str, tuple[str, str]] | None = None,
    *,
    pending: int = 0,
) -> dict[str, Any]:
    missing = missing or {}
    kinds = [kind for kind, _ in missing.values()]
    denominator = expected - pending
    return {
        "expected": expected,
        "collected": collected,
        "missing_count": len(missing),
        "failed_count": kinds.count("failed"),
        "pending_count": kinds.count("pending"),
        "quota_count": kinds.count("quota"),
        "stopped_count": kinds.count("stopped"),
        "ratio": round(collected / denominator, 4) if denominator > 0 else 1.0,
        "missing": {
            name: {"kind": kind, "reason": reason} for name, (kind, reason) in sorted(missing.items())
        },
    }


async def add_run(
    session: AsyncSession,
    kind: str,
    request: dict[str, Any],
    *,
    status: str = "success",
    metadata: dict[str, Any] | None = None,
    universe: str | None = UNIVERSE,
    finished_at: datetime | None = None,
    error_summary: str | None = None,
    key: str | None = None,
) -> int:
    finished = finished_at or datetime(2026, 10, 14, 11, 0, tzinfo=UTC)
    run = PipelineRun(
        kind=kind,
        universe_code=universe,
        triggered_by="scheduler",
        started_at=finished - timedelta(minutes=5),
        finished_at=finished if status in {"success", "partial", "failed"} else None,
        status=status,
        stage="done",
        idempotency_key=key,
        request_json=request,
        metadata_json=metadata or {},
        error_summary=error_summary,
    )
    session.add(run)
    await session.flush()
    return run.id


async def add_market_run(
    session: AsyncSession,
    trade_date: date,
    *,
    ratio_collected: tuple[int, int] = (300, 300),
    status: str = "success",
    provider: str = "baostock",
    universe: str = UNIVERSE,
    missing: dict[str, tuple[str, str]] | None = None,
    stop_reason: str | None = None,
    finished_at: datetime | None = None,
) -> int:
    expected, collected = ratio_collected
    metadata: dict[str, Any] = {"completeness": completeness(expected, collected, missing)}
    if stop_reason:
        metadata["stop_reason"] = stop_reason
        metadata["stop_error"] = "QuotaExhausted: 429"
    request: dict[str, Any] = {
        "provider": provider,
        "scope": "universe",
        "universe_code": universe,
        "trade_date": trade_date.isoformat(),
    }
    return await add_run(
        session,
        "market_snapshot",
        request,
        status=status,
        metadata=metadata,
        universe=universe,
        finished_at=finished_at
        or datetime.combine(trade_date, datetime.min.time(), UTC) + timedelta(hours=11),
        key=f"market_snapshot:{provider}:{universe}:{trade_date.isoformat()}:{status}:{collected}",
    )


async def add_bars(
    session: AsyncSession,
    listing_ids: list[int],
    days: list[date],
    *,
    provider: str = "baostock",
    adjusts: tuple[str, ...] = ("none",),
    is_backfill: bool = False,
) -> None:
    captured = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)
    rows = [
        MarketSnapshot(
            listing_id=listing_id,
            trade_date=day,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=Decimal("1000"),
            adjust=adjust,
            provider=provider,
            captured_at=captured,
            is_backfill=is_backfill,
            version=1,
        )
        for listing_id in listing_ids
        for day in days
        for adjust in adjusts
    ]
    session.add_all(rows)
    await session.flush()


async def add_financial(
    session: AsyncSession, listing_ids: list[int], period: date, *, provider: str = "baostock"
) -> None:
    session.add_all(
        FinancialSnapshot(
            listing_id=listing_id,
            report_period=period,
            period_kind="semi",
            available_from=period + timedelta(days=50),
            availability="actual",
            fields={"roe": 0.1},
            provider=provider,
            captured_at=datetime(2026, 10, 6, tzinfo=UTC),
            version=1,
        )
        for listing_id in listing_ids
    )
    await session.flush()


async def add_dividend_coverage(session: AsyncSession, listing_ids: list[int], year: int) -> None:
    stamp = datetime(2026, 10, 6, tzinfo=UTC)
    session.add_all(
        DividendCoverage(
            listing_id=listing_id,
            provider="baostock",
            year=year,
            first_checked_at=stamp,
            last_checked_at=stamp,
            event_count=1,
        )
        for listing_id in listing_ids
    )
    await session.flush()


async def add_industry(
    session: AsyncSession, listing_id: int, system: str, *, valid_from: date = date(2021, 7, 30)
) -> None:
    session.add(
        IndustryClassification(
            listing_id=listing_id,
            system=system,
            code="C38" if system == "csrc" else "630000",
            name="电气机械" if system == "csrc" else "电力设备",
            valid_from=valid_from,
            valid_to=None,
            source=system,
            captured_at=datetime(2026, 10, 6, tzinfo=UTC),
        )
    )
    await session.flush()


async def add_overdue(
    session: AsyncSession, listing_id: int, period: date, *, resolved: date | None = None
) -> None:
    session.add(
        ReportOverdue(
            listing_id=listing_id,
            report_period=period,
            deadline=date(2026, 8, 31),
            detected_on=date(2026, 9, 1),
            resolved_on=resolved,
            provider="baostock",
        )
    )
    await session.flush()

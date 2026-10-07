"""``GET /health/data`` - data-layer health aggregate (P1-E2).

Read-only: snapshot run completeness, freshness, accumulation toward the P1
exit metric, backfill progress, iFinD budget / queue / conflicts, overdue
reports and derived warnings (see :mod:`investment_agent.snapshots.health`).
Auth: ``X-API-Key`` or the web session. No secrets or raw provider bodies
are returned. ``/healthz`` stays the unauthenticated liveness probe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.routes.dashboard import (
    _APP_CONFIG_PATH,
    _calendar_path,
    calendar_warnings,
)
from investment_agent.clock import today_in
from investment_agent.config import Settings, SnapshotScheduleConfig, load_app_yaml
from investment_agent.scheduler.trading_calendar import TradingCalendar, load_trading_calendar
from investment_agent.snapshots.health import (
    DataHealth,
    HealthSettings,
    HealthWarning,
    build_data_health,
)
from investment_agent.storage.database import session_scope

router = APIRouter(
    prefix="/health",
    tags=["health"],
    dependencies=[Depends(verify_api_key_or_session)],
)

log = structlog.get_logger(__name__)

_TIMEZONE = "Asia/Shanghai"


def _snapshot_universes() -> tuple[str, ...]:
    try:
        snapshot = load_app_yaml(_APP_CONFIG_PATH).snapshot
    except Exception:
        snapshot = None
    universes = (snapshot or SnapshotScheduleConfig()).universes
    return tuple(dict.fromkeys(universes))


def _calendar() -> TradingCalendar | None:
    try:
        return load_trading_calendar(_calendar_path())
    except Exception:
        log.warning("data_health.calendar_unavailable")
        return None


def health_settings(settings: Settings, *, include_calendar_warnings: bool = True) -> HealthSettings:
    warnings: tuple[HealthWarning, ...] = ()
    if include_calendar_warnings:
        warnings = tuple(
            HealthWarning(
                code=item.code, level=item.level, message=item.message, section="freshness"
            )
            for item in calendar_warnings()
        )
    return HealthSettings(
        universes=_snapshot_universes(),
        threshold=settings.snapshot_completeness_threshold,
        budget_cap=settings.ifind_budget_per_5h,
        budget_window=timedelta(hours=settings.ifind_budget_window_hours),
        calendar=_calendar(),
        calendar_warnings=warnings,
    )


async def compute_data_health(
    session: AsyncSession, settings: Settings, *, include_calendar_warnings: bool = True
) -> DataHealth:
    return await build_data_health(
        session,
        health_settings(settings, include_calendar_warnings=include_calendar_warnings),
        now=datetime.now(UTC),
        today=today_in(_TIMEZONE),
    )


@router.get("/data", response_model=DataHealth)
async def data_health(settings: Annotated[Settings, Depends(get_settings)]) -> DataHealth:
    async with session_scope() as session:
        return await compute_data_health(session, settings)

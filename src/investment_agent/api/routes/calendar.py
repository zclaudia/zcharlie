"""``/calendar`` - trading-calendar status and the two maintenance actions.

The calendar in effect is the **data-dir copy** when it exists
(``DATA_DIR/calendars/<name>``), else the committed baseline
(``config/calendars/a_share.yaml``) - see
:func:`investment_agent.settings.loader.resolved_calendar_path`. The baseline
stays the reviewed artifact shipped with the checkout; applying a candidate
writes the runtime copy, which needs no commit and no writable ``config``.
The CLI (``tools/refresh_trading_calendar.py``) stays for ops.

* ``GET /calendar/status`` - coverage, reviewed years, the dual-source
  candidate (if the weekly probe recorded one), the effective file and its
  baseline, whether applying is possible, and how the last probe ended;
* ``POST /calendar/check`` - enqueue one ``calendar_probe`` run now (the
  worker cross-checks the next unreviewed year against baostock and
  AkShare/Sina; ``force`` skips only the expiry-window gate);
* ``POST /calendar/apply`` - write the verified candidate to the runtime copy
  and remove it. Applying can only *add* years: a candidate that would change
  or drop a reviewed year is refused (409), as is an unwritable data dir.

Auth: ``X-API-Key`` or the web session, like every other route.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.routes import dashboard
from investment_agent.clock import today_in
from investment_agent.config import Settings
from investment_agent.scheduler.calendar_refresh import refresh_years
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    calendar_coverage,
    load_trading_calendar,
)
from investment_agent.settings.loader import (
    calendar_path,
    configured_calendar_path,
    runtime_calendar_path,
)
from investment_agent.snapshots.calendar_probe import (
    CalendarCandidate,
    CandidateConflictError,
    apply_candidate,
    candidates,
)
from investment_agent.snapshots.jobs import CalendarProbePayload, enqueue_snapshot_job
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun

router = APIRouter(
    prefix="/calendar",
    tags=["calendar"],
    dependencies=[Depends(verify_api_key_or_session)],
)

log = structlog.get_logger(__name__)
_PROBE_KIND = "calendar_probe"


class CalendarYearStatus(BaseModel):
    year: int
    closed_dates: int
    verified_by: list[str] = Field(default_factory=list)
    verified_on: date | None = None


class CalendarCoverage(BaseModel):
    end: date | None
    days_remaining: int
    expired: bool
    message: str


class CalendarCandidateStatus(BaseModel):
    year: int
    path: str
    closed_dates: list[date]
    open_dates: list[date]
    trading_days: int
    verified_by: list[str] = Field(default_factory=list)
    verified_on: date | None = None


class LastProbe(BaseModel):
    run_id: int
    status: str
    outcome: str | None = None
    summary: str | None = None
    finished_at: str | None = None


class CalendarStatus(BaseModel):
    path: str
    """The file in effect."""
    baseline: str
    """The committed file the runtime copy is derived from."""
    target: str
    """Where an applied calendar is written (``DATA_DIR/calendars/<name>``)."""
    source: Literal["data_dir", "baseline"]
    calendar_id: str
    timezone: str
    years: list[CalendarYearStatus]
    coverage: CalendarCoverage
    candidate: CalendarCandidateStatus | None = None
    writable: bool
    last_probe: LastProbe | None = None


class ProbeQueued(BaseModel):
    run_id: int
    year: int
    created: bool


class CalendarApplied(BaseModel):
    path: str
    added_years: list[int]
    years: list[CalendarYearStatus]
    removed_candidate: str


def _paths() -> tuple[Path, Path, Path]:
    """``(baseline, effective, runtime target)`` for the configured calendar."""

    baseline = Path(configured_calendar_path(dashboard._APP_CONFIG_PATH))
    return baseline, Path(calendar_path(dashboard._APP_CONFIG_PATH)), runtime_calendar_path(baseline)


def _load(path: Path) -> TradingCalendar:
    try:
        return load_trading_calendar(path)
    except Exception:
        log.exception("calendar.status_unreadable", calendar_path=str(path))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"无法读取交易日历 {path}。",
        ) from None


def _year_status(calendar: TradingCalendar) -> list[CalendarYearStatus]:
    return [
        CalendarYearStatus(
            year=item.year,
            closed_dates=len(item.closed_dates),
            verified_by=list(item.verified_by),
            verified_on=item.verified_on,
        )
        for item in sorted(calendar.years, key=lambda entry: entry.year)
    ]


def _candidate_status(candidate: CalendarCandidate) -> CalendarCandidateStatus:
    return CalendarCandidateStatus(
        year=candidate.year,
        path=str(candidate.path),
        closed_dates=list(candidate.closed_dates),
        open_dates=list(candidate.open_dates),
        trading_days=candidate.trading_days,
        verified_by=list(candidate.verified_by),
        verified_on=candidate.verified_on,
    )


def _writable(target: Path) -> bool:
    """Whether the apply target can be written (its nearest existing dir is writable)."""

    directory = target.parent
    while not directory.exists() and directory.parent != directory:
        directory = directory.parent
    return os.access(directory, os.W_OK)


async def _last_probe() -> LastProbe | None:
    async with session_scope() as session:
        run = await session.scalar(
            select(PipelineRun)
            .where(PipelineRun.kind == _PROBE_KIND)
            .order_by(desc(PipelineRun.id))
            .limit(1)
        )
    if run is None:
        return None
    metadata = run.metadata_json or {}
    outcome = metadata.get("status")
    return LastProbe(
        run_id=run.id,
        status=run.status,
        outcome=str(outcome) if outcome else None,
        summary=run.error_summary or None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
    )


@router.get("/status", response_model=CalendarStatus)
async def calendar_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> CalendarStatus:
    baseline, effective, target = _paths()
    calendar = _load(effective)
    coverage = calendar_coverage(calendar, today_in(calendar.timezone))
    found = candidates(settings.data_dir)
    return CalendarStatus(
        path=str(effective),
        baseline=str(baseline),
        target=str(target),
        source="data_dir" if effective != baseline else "baseline",
        calendar_id=calendar.calendar_id,
        timezone=calendar.timezone,
        years=_year_status(calendar),
        coverage=CalendarCoverage(
            end=coverage.coverage_end,
            days_remaining=coverage.days_remaining,
            expired=coverage.expired,
            message=coverage.message(),
        ),
        candidate=_candidate_status(found[-1]) if found else None,
        writable=_writable(target),
        last_probe=await _last_probe(),
    )


@router.post("/check", response_model=ProbeQueued)
async def check_calendar(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ProbeQueued:
    """Enqueue one dual-source check of the next unreviewed year, right now."""

    _, effective, _ = _paths()
    calendar = _load(effective)
    today = today_in(calendar.timezone)
    year = refresh_years(calendar, today=today)[-1]
    payload = CalendarProbePayload(as_of=today, year=year, force=True)
    async with session_scope() as session:
        run, created = await enqueue_snapshot_job(session, payload, triggered_by="api")
    log.info("calendar.check_queued", run_id=run.id, year=year, created=created)
    return ProbeQueued(run_id=run.id, year=year, created=created)


@router.post("/apply", response_model=CalendarApplied)
async def apply_calendar(
    settings: Annotated[Settings, Depends(get_settings)],
) -> CalendarApplied:
    """Write the verified candidate to the runtime copy and remove it."""

    baseline, effective, target = _paths()
    _load(effective)
    found = candidates(settings.data_dir)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="没有待应用的交易日历候选；先点「检查更新」手工核对一次。",
        )
    candidate = found[-1]
    try:
        calendar, added = apply_candidate(target, candidate, fallback=baseline)
    except CandidateConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except OSError as exc:
        log.warning("calendar.apply_failed", path=str(target), error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"无法写入 {target}（{exc}）；请确认数据目录可写（DATA_DIR）。",
        ) from exc
    return CalendarApplied(
        path=str(target),
        added_years=list(added),
        years=_year_status(calendar),
        removed_candidate=str(candidate.path),
    )

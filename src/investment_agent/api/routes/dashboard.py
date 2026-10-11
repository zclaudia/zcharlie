"""Browser dashboard read models.

``GET /dashboard/today`` buckets the latest bounded window of research runs
into the four states the overview page needs (running, failed, partial,
needs_review) plus meaningful changes. All fields are derived from the
existing ``pipeline_run`` / ``research_snapshot`` / ``research_change_set``
tables — the run detail endpoint stays the single source of truth.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.dashboard import (
    DashboardBucket,
    DashboardEntry,
    DashboardToday,
)
from investment_agent.candidates.lifecycle import effective_draft
from investment_agent.clock import today_in
from investment_agent.config import SchedulerConfig, Settings, load_app_yaml
from investment_agent.scheduler.trading_calendar import (
    calendar_coverage,
    load_trading_calendar,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import (
    PipelineRun,
    ResearchChangeSet,
    ResearchSnapshot,
)
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.verification import VerificationRequest

router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(verify_api_key_or_session)],
)

_WINDOW = 300
_MAX_ENTRIES = 20
_UNFINISHED_VERIFICATION = ("pending", "deferred", "running")
_APP_CONFIG_PATH = Path("config/app.yaml")

log = structlog.get_logger(__name__)


class DashboardWarning(BaseModel):
    code: str
    level: Literal["warn", "error"]
    message: str
    coverage_end: str | None = None
    days_remaining: int | None = None
    source: Literal["calendar", "data_health"] = "calendar"
    link: str | None = None
    """Page with the details (data-health warnings link to ``/app/health``)."""


class DataHealthCounts(BaseModel):
    """The overview's "数据健康" bucket (P1-E2): warning counts and accumulation."""

    errors: int
    warnings: int
    infos: int
    streak_weeks: int
    target_weeks: int
    link: str = "/app/health"


class CandidateCounts(BaseModel):
    """The overview's "本周候选" bucket (P1-E1): counts only, no scores."""

    open: int
    drafts_ready: int
    """Open candidates whose draft rule awaits review."""
    drafting: int
    """Open candidates without a draft yet (queued or running)."""
    draft_failed: int
    verification_pending: int
    """iFinD verification requests not finished yet (pending / deferred / running)."""


class DashboardTodayWithWarnings(DashboardToday):
    warnings: list[DashboardWarning] = Field(default_factory=list)
    candidates: CandidateCounts | None = None
    data_health: DataHealthCounts | None = None


async def data_health_summary(
    session: AsyncSession, settings: Settings
) -> tuple[list[DashboardWarning], DataHealthCounts | None]:
    """``warn`` / ``error`` data-health warnings for the overview (never raises)."""

    from investment_agent.api.routes.data_health import compute_data_health

    try:
        health = await compute_data_health(session, settings, include_calendar_warnings=False)
    except Exception:
        log.exception("dashboard.data_health_failed")
        return [
            DashboardWarning(
                code="data_health_unavailable",
                level="error",
                message="数据健康检查失败，请查看服务日志。",
                source="data_health",
                link="/app/health",
            )
        ], None
    levels = [item.level for item in health.warnings]
    warnings = [
        DashboardWarning(
            code=item.code,
            level=item.level,
            message=item.message,
            source="data_health",
            link=f"/app/health#health-{item.section}",
        )
        for item in health.warnings
        if item.level in ("warn", "error")
    ]
    counts = DataHealthCounts(
        errors=levels.count("error"),
        warnings=levels.count("warn"),
        infos=levels.count("info"),
        streak_weeks=health.accumulation.streak_weeks,
        target_weeks=health.accumulation.target_weeks,
    )
    return warnings, counts


async def candidate_counts(session: AsyncSession) -> CandidateCounts:
    stmt = select(CandidateRecord).where(CandidateRecord.status == "open")
    open_rows = (await session.execute(stmt)).scalars().all()
    states: dict[str, int] = {}
    for candidate in open_rows:
        state, _draft = await effective_draft(session, candidate)
        states[state] = states.get(state, 0) + 1
    pending = (
        await session.execute(
            select(func.count())
            .select_from(VerificationRequest)
            .where(VerificationRequest.status.in_(_UNFINISHED_VERIFICATION))
        )
    ).scalar_one()
    return CandidateCounts(
        open=len(open_rows),
        drafts_ready=states.get("ready", 0),
        drafting=states.get("pending", 0) + states.get("none", 0),
        draft_failed=states.get("failed", 0),
        verification_pending=int(pending),
    )


def _calendar_path() -> Path:
    try:
        return Path(load_app_yaml(_APP_CONFIG_PATH).scheduler.a_share_calendar_path)
    except Exception:
        default = SchedulerConfig.model_fields["a_share_calendar_path"].default
        return Path(str(default))


def calendar_candidates(data_dir: Path | None) -> list[Path]:
    """Candidate calendars written by the scheduled probe, newest year last."""

    if data_dir is None:
        return []
    directory = Path(data_dir) / "calendar_candidates"
    return sorted(directory.glob("a_share-*.yaml")) if directory.is_dir() else []


def calendar_warnings(
    calendar_path: Path | None = None, *, data_dir: Path | None = None
) -> list[DashboardWarning]:
    """Surface trading-calendar expiry before scheduled refreshes silently stop.

    A candidate written by the weekly ``calendar_probe`` job (dual-source
    cross-check passed) gets its own warning: the refresh itself only needs
    ``tools/refresh_trading_calendar.py --write`` plus a commit.
    """

    warnings: list[DashboardWarning] = []
    for candidate in calendar_candidates(data_dir):
        warnings.append(
            DashboardWarning(
                code="calendar_candidate",
                level="warn",
                message=(
                    f"交易日历候选已生成：{candidate}（双源核对通过）。"
                    "核对差异后运行 tools/refresh_trading_calendar.py --write 并提交。"
                ),
            )
        )
    path = calendar_path or _calendar_path()
    try:
        calendar = load_trading_calendar(path)
    except Exception:
        log.exception("dashboard.calendar_unavailable", calendar_path=str(path))
        warnings.append(
            DashboardWarning(
                code="calendar",
                level="error",
                message=f"无法读取交易日历 {path}，观察池定时刷新可能无法运行。",
            )
        )
        return warnings
    coverage = calendar_coverage(calendar, today_in(calendar.timezone))
    if not coverage.expiring_soon:
        return warnings
    warnings.append(
        DashboardWarning(
            code="calendar",
            level="error" if coverage.expired else "warn",
            message=coverage.message(),
            coverage_end=(
                coverage.coverage_end.isoformat() if coverage.coverage_end else None
            ),
            days_remaining=coverage.days_remaining,
        )
    )
    return warnings


def _entry(
    run: PipelineRun,
    snapshot: ResearchSnapshot | None,
    change_set: ResearchChangeSet | None,
) -> DashboardEntry:
    request = run.request_json
    ticker = request.get("ticker")
    summary = run.error_summary
    if change_set is not None and change_set.has_meaningful_changes:
        summary = summary or f"{change_set.meaningful_change_count} 项重要变化"
    return DashboardEntry(
        run_id=run.id,
        ticker=(
            f"{snapshot.symbol}.{snapshot.market}"
            if snapshot is not None
            else (str(ticker) if isinstance(ticker, str) else None)
        ),
        name=snapshot.company_name if snapshot is not None else None,
        status=run.status,
        stage=run.stage,
        attempt_count=run.attempt_count,
        worker_id=run.worker_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        changed_at=change_set.notified_at if change_set is not None else None,
        summary=summary,
    )


@router.get("/today", response_model=DashboardTodayWithWarnings)
async def dashboard_today(
    settings: Annotated[Settings, Depends(get_settings)],
    window: Annotated[int, Query(ge=10, le=500)] = _WINDOW,
) -> DashboardTodayWithWarnings:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(PipelineRun, ResearchSnapshot, ResearchChangeSet)
                    .outerjoin(
                        ResearchSnapshot,
                        ResearchSnapshot.pipeline_run_id == PipelineRun.id,
                    )
                    .outerjoin(
                        ResearchChangeSet,
                        ResearchChangeSet.current_snapshot_id == ResearchSnapshot.id,
                    )
                    .where(PipelineRun.kind == "ifind_research")
                    .order_by(PipelineRun.id.desc())
                    .limit(window)
                )
            )
            .all()
        )

        buckets: dict[str, list[tuple[PipelineRun, ResearchSnapshot | None, ResearchChangeSet | None]]] = {
            "running": [],
            "failed": [],
            "partial": [],
            "needs_review": [],
            "meaningful_changes": [],
        }
        seen_review_tickers: set[str] = set()
        for run, snapshot, change_set in rows:
            if run.status in {"running", "queued"}:
                buckets["running"].append((run, snapshot, change_set))
            elif run.status == "failed":
                buckets["failed"].append((run, snapshot, change_set))
            elif run.status == "partial":
                buckets["partial"].append((run, snapshot, change_set))
            if snapshot is not None and snapshot.score_json.get("needs_agent_review") is True:
                ticker = f"{snapshot.symbol}.{snapshot.market}"
                if ticker not in seen_review_tickers:
                    seen_review_tickers.add(ticker)
                    buckets["needs_review"].append((run, snapshot, change_set))
            if change_set is not None and change_set.has_meaningful_changes:
                buckets["meaningful_changes"].append((run, snapshot, change_set))

        def bucket(name: str) -> DashboardBucket:
            entries = buckets[name]
            return DashboardBucket(
                count=len(entries),
                entries=[_entry(*row) for row in entries[:_MAX_ENTRIES]],
            )

        health_warnings, health_counts = await data_health_summary(session, settings)
        return DashboardTodayWithWarnings(
            running=bucket("running"),
            failed=bucket("failed"),
            partial=bucket("partial"),
            needs_review=bucket("needs_review"),
            meaningful_changes=bucket("meaningful_changes"),
            generated_at=datetime.now(UTC),
            warnings=calendar_warnings(data_dir=settings.data_dir) + health_warnings,
            candidates=await candidate_counts(session),
            data_health=health_counts,
        )

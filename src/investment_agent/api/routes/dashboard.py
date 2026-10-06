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
from sqlalchemy import select

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.dashboard import (
    DashboardBucket,
    DashboardEntry,
    DashboardToday,
)
from investment_agent.clock import today_in
from investment_agent.config import SchedulerConfig, load_app_yaml
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

router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(verify_api_key_or_session)],
)

_WINDOW = 300
_MAX_ENTRIES = 20
_APP_CONFIG_PATH = Path("config/app.yaml")

log = structlog.get_logger(__name__)


class DashboardWarning(BaseModel):
    code: str
    level: Literal["warn", "error"]
    message: str
    coverage_end: str | None = None
    days_remaining: int | None = None


class DashboardTodayWithWarnings(DashboardToday):
    warnings: list[DashboardWarning] = Field(default_factory=list)


def _calendar_path() -> Path:
    try:
        return Path(load_app_yaml(_APP_CONFIG_PATH).scheduler.a_share_calendar_path)
    except Exception:
        default = SchedulerConfig.model_fields["a_share_calendar_path"].default
        return Path(str(default))


def calendar_warnings(calendar_path: Path | None = None) -> list[DashboardWarning]:
    """Surface trading-calendar expiry before scheduled refreshes silently stop."""

    path = calendar_path or _calendar_path()
    try:
        calendar = load_trading_calendar(path)
    except Exception:
        log.exception("dashboard.calendar_unavailable", calendar_path=str(path))
        return [
            DashboardWarning(
                code="calendar",
                level="error",
                message=f"无法读取交易日历 {path}，自选股定时刷新可能无法运行。",
            )
        ]
    coverage = calendar_coverage(calendar, today_in(calendar.timezone))
    if not coverage.expiring_soon:
        return []
    return [
        DashboardWarning(
            code="calendar",
            level="error" if coverage.expired else "warn",
            message=coverage.message(),
            coverage_end=(
                coverage.coverage_end.isoformat() if coverage.coverage_end else None
            ),
            days_remaining=coverage.days_remaining,
        )
    ]


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

        return DashboardTodayWithWarnings(
            running=bucket("running"),
            failed=bucket("failed"),
            partial=bucket("partial"),
            needs_review=bucket("needs_review"),
            meaningful_changes=bucket("meaningful_changes"),
            generated_at=datetime.now(UTC),
            warnings=calendar_warnings(),
        )

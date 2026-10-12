"""The deterministic daily ``zone_detection`` job (watch-holding-plan 6.2 WP8).

The scheduler and the ingestion hooks only enqueue; a worker executes. One run
covers one scope and one date, keyed
``zone_detection:{scope}:{as_of}:{reason}``: the ``reason`` token is what makes
a re-trigger after new inputs (a close-quote refresh, a new valuation) a new
run instead of a no-op, while a repeated trigger from the same source stays
idempotent. No LLM, no model budget - the run only reads factors, valuations
and rules and writes zone events.

Gates: candidate-scope detection runs on reviewed trading days only (no bars
are published on a non-trading day, so a weekend run would re-state Friday
with a new date and only add noise). A calendar that cannot decide - missing,
broken, or without the year under review - keeps the gate closed instead of
waving the run through. Holding-scope detection runs every day - holding days
and prices are part of the valuation, which is refreshed daily.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.rules.zone_detection import (
    CANDIDATE_SCOPE,
    HOLDING_SCOPE,
    ZoneScanOutcome,
    ZoneScope,
    run_zone_detection,
)
from investment_agent.scheduler.trading_calendar import load_trading_calendar
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

log = structlog.get_logger(__name__)

ZONE_DETECTION_JOB = "zone_detection"
ZONE_SCOPES: tuple[ZoneScope, ...] = (CANDIDATE_SCOPE, HOLDING_SCOPE)
PROJECT_ROOT_CALENDAR = "config/calendars/a_share.yaml"


class ZoneDetectionPayload(BaseModel):
    """Frozen inputs of one detection run (stable across retries)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: date
    scope: Literal["candidate", "holding"]
    reason: str = Field(default="cron", min_length=1, max_length=64)
    """Trigger token: ``cron``, the ingestion job name, ``valuation:<account>``..."""


def zone_detection_key(*, scope: ZoneScope, as_of: date, reason: str) -> str:
    return f"{ZONE_DETECTION_JOB}:{scope}:{as_of.isoformat()}:{reason}"


async def enqueue_zone_detection(
    session: AsyncSession,
    *,
    as_of: date,
    scope: ZoneScope,
    triggered_by: str,
    reason: str = "cron",
) -> tuple[object, bool]:
    """Queue one detection run (idempotent per scope, date and trigger)."""

    payload = ZoneDetectionPayload(as_of=as_of, scope=scope, reason=reason)
    return await PipelineRunRepository(session).enqueue(
        kind=ZONE_DETECTION_JOB,
        idempotency_key=zone_detection_key(scope=scope, as_of=as_of, reason=reason),
        triggered_by=triggered_by,
        request_json=payload.model_dump(mode="json"),
    )


async def enqueue_due_zone_detections(
    session: AsyncSession,
    *,
    triggered_by: str,
    as_of: date | None = None,
    reason: str = "cron",
    scopes: tuple[ZoneScope, ...] = ZONE_SCOPES,
) -> list[int]:
    """Queue today's runs for the requested scopes; returns the new run ids."""

    day = as_of or shanghai_today()
    created: list[int] = []
    for scope in scopes:
        run, was_created = await enqueue_zone_detection(
            session, as_of=day, scope=scope, triggered_by=triggered_by, reason=reason
        )
        if was_created:
            created.append(run.id)  # type: ignore[attr-defined]
    return created


def is_trading_day(calendar_path: str | None, day: date) -> bool | None:
    """``True`` / ``False`` / ``None`` when the calendar cannot decide."""

    if calendar_path is None:
        return None
    try:
        return load_trading_calendar(Path(calendar_path)).is_trading_day(day)
    except Exception:
        # A missing or unreviewed calendar must not silently pass for a
        # reviewed trading day, but it also must not stop a deterministic run:
        # the run is idempotent and the factors simply do not change.
        log.warning("zone_detection.calendar_unavailable", path=calendar_path, exc_info=True)
        return None


class ZoneDetectionHandler(BaseJobHandler):
    """The deterministic zone-detection run (never calls a model)."""

    kind = ZONE_DETECTION_JOB

    def __init__(self, *, settings: Settings, calendar_path: str | None = None) -> None:
        self.settings = settings
        self.calendar_path = calendar_path

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        payload = ZoneDetectionPayload.model_validate(job.request_json)
        skipped_reason: str | None = None
        if payload.scope == CANDIDATE_SCOPE:
            decision = is_trading_day(self.calendar_path, payload.as_of)
            if decision is False:
                skipped_reason = "non_trading_day"
            elif decision is None:
                # An unreviewed calendar cannot vouch for the day: without the
                # gate a weekend run would only re-state the last trading day
                # under a new date (noise events), so candidate scope stays
                # conservative. Holding scope still runs every day - the
                # valuation it reads refreshes daily regardless of the gate.
                skipped_reason = "trading_day_unknown"
        outcome: ZoneScanOutcome | None = None

        async def work() -> ZoneScanOutcome | None:
            async with session_scope() as session:
                return await run_zone_detection(
                    session,
                    as_of=payload.as_of,
                    scope=payload.scope,
                    settings=self.settings,
                )

        if skipped_reason is None:
            outcome = await ctx.run_with_heartbeat(job.lease, work)
        async with session_scope() as session:
            runs = PipelineRunRepository(session)
            metadata: dict[str, object] = {
                "as_of": payload.as_of.isoformat(),
                "scope": payload.scope,
                "reason": payload.reason,
            }
            if outcome is None:
                metadata["skipped_reason"] = skipped_reason or "not_executed"
            else:
                metadata.update(outcome.metadata())
            await runs.mark_success(job.run_id, metadata, lease=job.lease)
        log.info(
            "zone_detection.finished",
            run_id=job.run_id,
            scope=payload.scope,
            as_of=payload.as_of.isoformat(),
            created=len(outcome.created) if outcome is not None else 0,
            skipped=skipped_reason,
        )
        return "completed"


__all__ = [
    "ZONE_DETECTION_JOB",
    "ZONE_SCOPES",
    "ZoneDetectionHandler",
    "ZoneDetectionPayload",
    "enqueue_due_zone_detections",
    "enqueue_zone_detection",
    "is_trading_day",
    "zone_detection_key",
]

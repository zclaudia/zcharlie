"""``screen_run`` queue job (P1-C3): weekly template screen of a target pool.

One run = one universe on one ``as_of``:

1. optionally sync ``config/rule_templates`` into the rule store (idempotent;
   a changed file becomes a new template version before it is used);
2. load the latest version of every stored template;
3. for every universe member on ``as_of``: build the point-in-time view
   (:func:`investment_agent.factors.view.load_snapshot_view`, baostock
   target-pool policy) and compute the factors the templates and the
   pre-filter need (:data:`investment_agent.factors.catalog.DEFAULT_REGISTRY`);
4. :func:`investment_agent.rules.screen.run_screen` (pure);
5. replace this run's ``screen_result`` rows (a retried attempt rewrites
   them) and store a summary in ``pipeline_run.metadata_json``;
6. in a second transaction: expire overdue candidates and open / merge
   candidates from the ``hit`` rows (P1-D1,
   :func:`investment_agent.candidates.lifecycle.create_candidates_from_screen`;
   idempotent, so a retried run neither duplicates candidates nor drafts).

Idempotency key ``screen_run:<universe>:<as_of>``: a re-enqueue for the same
slot returns the existing run (``ia screen run --rerun`` adds a suffix).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.candidates.lifecycle import (
    CandidatePolicy,
    create_candidates_from_screen,
    expire_candidates,
)
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.view import load_snapshot_view
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.providers.baostock_reference import IndexCode
from investment_agent.rules.schema import RuleTemplate
from investment_agent.rules.screen import (
    DEFAULT_HIT_CAP,
    ScreenConfig,
    ScreenMember,
    ScreenRun,
    jsonable,
    required_factors,
    run_screen,
)
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.universe import UniverseRepository

log = structlog.get_logger(__name__)

SCREEN_RUN = "screen_run"
SUMMARY_EXAMPLES = 5


class ScreenRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    universe_code: IndexCode
    as_of: date
    sync_templates: bool = True
    hit_cap: int = Field(default=DEFAULT_HIT_CAP, ge=1, le=500)


def screen_idempotency_key(payload: ScreenRunPayload) -> str:
    return f"{SCREEN_RUN}:{payload.universe_code}:{payload.as_of.isoformat()}"


async def enqueue_screen_run(
    session: AsyncSession,
    payload: ScreenRunPayload,
    *,
    triggered_by: str,
    key: str | None = None,
    max_attempts: int = 3,
) -> tuple[PipelineRun, bool]:
    await UniverseRepository(session).ensure_universe(payload.universe_code)
    return await PipelineRunRepository(session).enqueue(
        kind=SCREEN_RUN,
        idempotency_key=key or screen_idempotency_key(payload),
        triggered_by=triggered_by,
        universe_code=payload.universe_code,
        request_json=payload.model_dump(mode="json"),
        max_attempts=max_attempts,
    )


@dataclass(frozen=True)
class MemberLoad:
    members: list[ScreenMember]
    errors: dict[str, str]
    seconds: float


async def load_members(
    session: AsyncSession, universe_code: str, as_of: date, factor_names: Sequence[str]
) -> MemberLoad:
    """Point-in-time factor values of every member (one view per member).

    Measured on the CSI300 database copy: ~11 s for 300 members computing
    every registered factor, so no batching is needed yet.
    """

    started = time.perf_counter()
    universe = await UniverseRepository(session).members_as_of(universe_code, as_of)
    names = sorted(set(factor_names))
    members: list[ScreenMember] = []
    errors: dict[str, str] = {}
    for item in universe:
        try:
            view = await load_snapshot_view(session, item.ticker, as_of)
        except LookupError as exc:
            errors[item.ticker] = str(exc)
            continue
        context: dict[str, Any] = {
            "provider": view.provider,
            "as_of": view.as_of.isoformat(),
            "industry_group": view.industry_group,
        }
        if view.industry_match is not None:
            context["industry"] = view.industry_match.describe()
        members.append(
            ScreenMember(
                listing_id=item.listing_id,
                ticker=item.ticker,
                industry_group=view.industry_group,
                factors=DEFAULT_REGISTRY.compute_all(view, names),
                context=context,
            )
        )
    return MemberLoad(members, errors, time.perf_counter() - started)


async def persist_screen(session: AsyncSession, run_id: int, screen: ScreenRun) -> int:
    """Replace ``screen_result`` rows of ``run_id``; returns the row count."""

    await session.execute(delete(ScreenResultRecord).where(ScreenResultRecord.screen_run_id == run_id))
    count = 0
    for template in screen.templates:
        for member in template.members:
            session.add(
                ScreenResultRecord(
                    screen_run_id=run_id,
                    as_of=screen.as_of,
                    template_id=template.template_id,
                    template_version=template.template_version,
                    listing_id=member.listing_id,
                    ticker=member.ticker,
                    outcome=member.outcome,
                    hit=member.hit,
                    rank=member.rank,
                    strength=member.strength,
                    reasons=[dict(reason) for reason in member.reasons]
                    or ([{"detail": member.detail}] if member.detail else []),
                    context=jsonable(screen.member_context.get(member.ticker, {})),
                )
            )
            count += 1
    await session.flush()
    return count


def screen_summary(screen: ScreenRun) -> dict[str, Any]:
    """Run metadata: counts per template, truncation, top miss causes, examples."""

    templates: dict[str, Any] = {}
    for template in screen.templates:
        misses = sorted(
            template.misses_by_condition.items(),
            key=lambda item: (-(item[1]["not_met"] + item[1]["missing"]), item[0]),
        )
        templates[template.template_ref] = {
            "calibration_status": template.calibration_status,
            "counts": template.counts(),
            "top_hits": [
                {"ticker": member.ticker, "rank": member.rank, "strength": member.strength}
                for member in template.by_outcome("hit")[:SUMMARY_EXAMPLES]
            ],
            "top_miss_conditions": [
                {"rule_path": path, **{k: entry[k] for k in ("factor", "not_met", "missing")}}
                for path, entry in misses[:SUMMARY_EXAMPLES]
            ],
            "missing_by_factor": dict(sorted(template.missing_by_factor.items())),
        }
    return {
        "as_of": screen.as_of.isoformat(),
        "member_count": screen.member_count,
        "excluded": {ticker: list(reasons) for ticker, reasons in sorted(screen.excluded.items())},
        "config": screen.config.to_dict(),
        "templates": templates,
    }


async def execute_screen_run(
    payload: ScreenRunPayload,
    *,
    run_id: int,
    templates_dir: Path | None = None,
    candidate_policy: CandidatePolicy | None = None,
) -> dict[str, Any]:
    """Sync templates (optional), screen the universe, persist, then create
    candidates from the hits; returns metadata."""

    if payload.sync_templates:
        files = load_templates(templates_dir)
        async with session_scope() as session:
            synced = await RuleStore(session).sync_templates(files)
        sync_meta = [
            {"template": item.template_id, "version": item.version, "action": item.action}
            for item in synced
        ]
    else:
        sync_meta = []
    async with session_scope() as session:
        templates: list[RuleTemplate] = await RuleStore(session).latest_templates()
        if not templates:
            raise RuntimeError("no rule templates stored; run `ia rules sync` first")
        config = ScreenConfig(hit_cap=payload.hit_cap)
        loaded = await load_members(
            session, payload.universe_code, payload.as_of, sorted(required_factors(templates, config))
        )
        started = time.perf_counter()
        screen = run_screen(
            as_of=payload.as_of, members=loaded.members, templates=templates, config=config
        )
        rows = await persist_screen(session, run_id, screen)
        screen_seconds = time.perf_counter() - started
    async with session_scope() as session:
        expired = await expire_candidates(session)
        candidates = await create_candidates_from_screen(
            session, run_id=run_id, policy=candidate_policy or CandidatePolicy()
        )
        candidates.expired = expired
    metadata = screen_summary(screen)
    metadata["candidates"] = candidates.to_metadata()
    metadata.update(
        {
            "universe_code": payload.universe_code,
            "template_sync": sync_meta,
            "load_errors": loaded.errors,
            "result_rows": rows,
            "timing_seconds": {
                "factors": round(loaded.seconds, 3),
                "screen_and_persist": round(screen_seconds, 3),
            },
        }
    )
    return metadata


class ScreenRunHandler(BaseJobHandler):
    kind = SCREEN_RUN

    def __init__(
        self,
        *,
        templates_dir: Path | None = None,
        candidate_policy: CandidatePolicy | None = None,
    ) -> None:
        self.templates_dir = templates_dir
        self.candidate_policy = candidate_policy

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        payload = ScreenRunPayload.model_validate(job.request_json)

        async def work() -> dict[str, Any]:
            return await execute_screen_run(
                payload,
                run_id=job.run_id,
                templates_dir=self.templates_dir,
                candidate_policy=self.candidate_policy,
            )

        metadata = await ctx.run_with_heartbeat(job.lease, work)
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(
                job.run_id, metadata, lease=job.lease
            )
        log.info(
            "screen.run.completed",
            run_id=job.run_id,
            universe=payload.universe_code,
            as_of=payload.as_of.isoformat(),
            hits={ref: item["counts"]["hit"] for ref, item in metadata["templates"].items()},
        )
        return "completed"


async def screen_results(
    session: AsyncSession, run_id: int, *, template_id: str | None = None
) -> list[ScreenResultRecord]:
    stmt = (
        select(ScreenResultRecord)
        .where(ScreenResultRecord.screen_run_id == run_id)
        .order_by(
            ScreenResultRecord.template_id,
            ScreenResultRecord.hit.desc(),
            ScreenResultRecord.rank,
            ScreenResultRecord.ticker,
        )
    )
    if template_id is not None:
        stmt = stmt.where(ScreenResultRecord.template_id == template_id)
    return list((await session.execute(stmt)).scalars())

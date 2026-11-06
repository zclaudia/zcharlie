"""``/health/data`` - data-layer health aggregate (P1-E2), plus the Shenwan
directory card's status and refresh action (config-docs-plan §3.2 / WP5).

Read-only: snapshot run completeness, freshness, accumulation toward the P1
exit metric, backfill progress, iFinD budget / queue / conflicts, overdue
reports, per-provider upstream call counters (``providers``: 24-hour calls /
failures from the ``provider_call`` ledger and the circuit-breaker state - the
API process has no breaker in memory, so "熔断中" comes from the ledger's
``circuit`` rows) and derived warnings (see
:mod:`investment_agent.snapshots.health`).
Auth: ``X-API-Key`` or the web session. No secrets or raw provider bodies
are returned. ``/healthz`` stays the unauthenticated liveness probe.

The aggregate construction (``compute_data_health`` and its settings /
universe helpers) now lives in :mod:`investment_agent.api.views`, shared with
the MCP ``get_data_health`` tool; this module keeps the route and re-exports
the helpers so existing importers keep working.

* ``GET /health/shenwan-directory`` - which directory file is in effect (the
  ``DATA_DIR/shenwan`` copy once a refresh ran, else the repository
  baseline), its version / capture date / level counts, whether the target is
  writable, and how the last refresh run ended;
* ``POST /health/shenwan-directory/refresh`` - enqueue one
  ``shenwan_directory_refresh`` run now (deduped to one run a day by the
  idempotency key, exactly like the calendar check endpoint). The worker
  fetches the official code table, writes only the DATA_DIR copy and reports
  the industry-groups coverage of the new directory.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, select

from investment_agent.api.auth import get_settings, verify_api_key_or_session

# Re-exported for existing importers: the aggregate lives in
# :mod:`investment_agent.api.views` and the universe helper in
# :mod:`investment_agent.directory_search`. The dashboard route imports
# ``compute_data_health`` from here and ``tests/unit/test_settings_consumers.py``
# reads ``_snapshot_universes`` from here, so both names are kept on this module.
from investment_agent.api.views import compute_data_health as compute_data_health
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.directory_search import (
    snapshot_universes as _snapshot_universes,  # noqa: F401  (re-export)
)
from investment_agent.providers.shenwan_directory import (
    DEFAULT_SHENWAN_DIRECTORY_PATH,
    load_shenwan_directory,
    resolved_shenwan_directory_path,
    runtime_shenwan_directory_path,
)
from investment_agent.snapshots.health import DataHealth
from investment_agent.snapshots.jobs import (
    SHENWAN_DIRECTORY_REFRESH,
    ShenwanRefreshPayload,
    enqueue_snapshot_job,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun

router = APIRouter(
    prefix="/health",
    tags=["health"],
    dependencies=[Depends(verify_api_key_or_session)],
)

log = structlog.get_logger(__name__)


@router.get("/data", response_model=DataHealth)
async def data_health(settings: Annotated[Settings, Depends(get_settings)]) -> DataHealth:
    async with session_scope() as session:
        return await compute_data_health(session, settings)


# --------------------------------------------------------------------------- #
# Shenwan directory card (config-docs-plan §3.2 / WP5)
# --------------------------------------------------------------------------- #


class ShenwanLastRefresh(BaseModel):
    run_id: int
    status: str
    outcome: str | None = None
    """The run metadata's ``status`` (``refreshed`` once a run wrote a copy)."""
    summary: str | None = None
    coverage_ok: bool | None = None
    finished_at: str | None = None


class ShenwanDirectoryStatus(BaseModel):
    path: str
    """The directory file in effect."""
    baseline: str
    target: str
    """Where a refresh writes (``DATA_DIR/shenwan/<name>``)."""
    source: Literal["data_dir", "baseline"]
    version: str
    captured_on: date | None = None
    level1: int
    level2: int
    level3: int
    writable: bool
    last_refresh: ShenwanLastRefresh | None = None


class ShenwanRefreshQueued(BaseModel):
    run_id: int
    created: bool


def _writable(target: Path) -> bool:
    """Whether the refresh target can be written (its nearest existing dir is)."""

    directory = target.parent
    while not directory.exists() and directory.parent != directory:
        directory = directory.parent
    return os.access(directory, os.W_OK)


async def _last_refresh() -> ShenwanLastRefresh | None:
    async with session_scope() as session:
        run = await session.scalar(
            select(PipelineRun)
            .where(PipelineRun.kind == SHENWAN_DIRECTORY_REFRESH)
            .order_by(desc(PipelineRun.id))
            .limit(1)
        )
    if run is None:
        return None
    metadata = run.metadata_json or {}
    coverage_ok = metadata.get("coverage_ok")
    return ShenwanLastRefresh(
        run_id=run.id,
        status=run.status,
        outcome=str(metadata["status"]) if metadata.get("status") else None,
        summary=run.error_summary or str(metadata.get("summary") or "") or None,
        coverage_ok=coverage_ok if isinstance(coverage_ok, bool) else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
    )


@router.get("/shenwan-directory", response_model=ShenwanDirectoryStatus)
async def shenwan_directory_status() -> ShenwanDirectoryStatus:
    """The Shenwan directory in effect (DATA_DIR copy first, else the baseline)."""

    baseline = DEFAULT_SHENWAN_DIRECTORY_PATH
    effective = resolved_shenwan_directory_path()
    target = runtime_shenwan_directory_path()
    try:
        directory = load_shenwan_directory(effective)
    except Exception:
        log.exception("health.shenwan_directory_unreadable", path=str(effective))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"无法读取申万行业目录 {effective}。",
        ) from None
    return ShenwanDirectoryStatus(
        path=str(effective),
        baseline=str(baseline),
        target=str(target),
        source="data_dir" if effective != baseline else "baseline",
        version=directory.version,
        captured_on=directory.captured_on,
        level1=len(directory.codes(1)),
        level2=len(directory.codes(2)),
        level3=len(directory.codes(3)),
        writable=_writable(target),
        last_refresh=await _last_refresh(),
    )


@router.post("/shenwan-directory/refresh", response_model=ShenwanRefreshQueued)
async def refresh_shenwan_directory() -> ShenwanRefreshQueued:
    """Enqueue one directory refresh run now (one run per day; see the key)."""

    payload = ShenwanRefreshPayload(as_of=shanghai_today())
    async with session_scope() as session:
        run, created = await enqueue_snapshot_job(session, payload, triggered_by="api")
    log.info("health.shenwan_refresh_queued", run_id=run.id, created=created)
    return ShenwanRefreshQueued(run_id=run.id, created=created)

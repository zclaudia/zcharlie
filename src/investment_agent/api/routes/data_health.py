"""``GET /health/data`` - data-layer health aggregate (P1-E2).

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
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from investment_agent.api.auth import get_settings, verify_api_key_or_session

# Re-exported for existing importers: the aggregate and its universe helper now
# live in :mod:`investment_agent.api.views`. The dashboard route imports
# ``compute_data_health`` from here and ``tests/unit/test_settings_consumers.py``
# reads ``_snapshot_universes`` from here, so both names are kept on this module.
from investment_agent.api.views import _snapshot_universes as _snapshot_universes
from investment_agent.api.views import compute_data_health as compute_data_health
from investment_agent.config import Settings
from investment_agent.snapshots.health import DataHealth
from investment_agent.storage.database import session_scope

router = APIRouter(
    prefix="/health",
    tags=["health"],
    dependencies=[Depends(verify_api_key_or_session)],
)


@router.get("/data", response_model=DataHealth)
async def data_health(settings: Annotated[Settings, Depends(get_settings)]) -> DataHealth:
    async with session_scope() as session:
        return await compute_data_health(session, settings)

"""A and B must consume one durable P4 queue capacity budget."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from investment_agent.config import Settings
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.supplemental_rules.update_jobs import request_update_maintenance


@pytest.mark.asyncio
async def test_a_maintenance_cannot_overfill_queue_occupied_by_b(db_session):
    now = datetime.now(UTC)
    rows = [PipelineRun(
        kind="supplemental_disclosure_poll", triggered_by="controlled-test",
        started_at=now, status="queued", stage="queued", request_json={},
    ) for _ in range(128)]
    db_session.add_all(rows)
    await db_session.flush()
    settings = Settings(_env_file=None, api_key="queue-test", supplemental_rules_enabled=True,
                        supplemental_updates_enabled=True)
    assert not await request_update_maintenance(db_session, settings=settings)
    assert not list(await db_session.scalars(select(PipelineRun).where(
        PipelineRun.kind == "supplemental_update_process")))
    rows[0].status = "completed"
    await db_session.flush()
    assert await request_update_maintenance(db_session, settings=settings)
    assert not await request_update_maintenance(db_session, settings=settings)
    active = list(await db_session.scalars(select(PipelineRun).where(
        PipelineRun.status.in_(("queued", "running")))))
    assert len(active) == 128

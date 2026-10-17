"""P4 capacity recovery uses the original station consent and frozen bytes."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from investment_agent.investor.monitoring import MonitoringService
from investment_agent.investor.notifications import deliver_one
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.monitoring import NotificationDelivery
from tests.support.investor import saved
from tests.support.notifications import FixtureTransport, enable
from tests.support.publication import publication_db as publication_db


async def _blocked(settings, case_id):
    async with session_scope() as session:
        await MonitoringService(session, settings)._upsert(
            "p4:capacity", str(case_id), {"category": "major_change", "stage": "reviewed"})
        row = await session.scalar(select(NotificationDelivery))
        row.state = "capacity_blocked"
        return row.id, dict(row.payload)


async def test_capacity_recovery_preserves_id_body_and_does_not_create_research(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable()
    ident, payload = await _blocked(settings, candidate.thesis.case_ref.id)
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) == "delivered"
    assert transport.calls[0]["delivery_id"] == ident
    import json
    assert json.loads(transport.calls[0]["body"]) == payload["body"]
    async with session_scope() as session:
        row = await session.get(NotificationDelivery, ident)
        assert row.state == "delivered" and row.payload["attempts"] == 1
        assert row.payload["case_revision"] == payload["case_revision"]


@pytest.mark.parametrize("terminal", ["failed", "unknown", "suppressed", "superseded"])
async def test_capacity_recovery_never_resurrects_terminal_delivery(publication_db, terminal):
    candidate, _, _, _ = await saved()
    settings = await enable()
    ident, payload = await _blocked(settings, candidate.thesis.case_ref.id)
    async with session_scope() as session:
        row = await session.get(NotificationDelivery, ident)
        row.state = terminal
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport, now=datetime.now(UTC)) is None
    assert not transport.calls
    async with session_scope() as session:
        row = await session.get(NotificationDelivery, ident)
        assert row.state == terminal and row.payload == payload


async def test_capacity_recovery_rechecks_configuration_instead_of_reconsenting(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable()
    ident, payload = await _blocked(settings, candidate.thesis.case_ref.id)
    async with session_scope() as session:
        await store_for(session, settings).apply({"notification_external_enabled": False}, actor="api")
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) is None
    assert not transport.calls
    async with session_scope() as session:
        row = await session.get(NotificationDelivery, ident)
        assert row.state == "suppressed" and row.payload == payload

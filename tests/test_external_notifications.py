"""Frozen AG15 changes, explicit consent and fixture-only delivery failures."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from investment_agent.investor.controls import CaseAction
from investment_agent.investor.monitoring import MonitoringService
from investment_agent.investor.notification_http import HTTPSWebhook, validate_endpoint
from investment_agent.investor.notifications import configuration, deliver_one, freeze
from investment_agent.settings.loader import env_import_values, store_for
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.app_settings import AppSetting
from investment_agent.storage.models.monitoring import InvestorNotification, NotificationDelivery
from tests.test_agent_conversations import base
from tests.test_investor_controls import case_ref, execute
from tests.test_investor_monitoring import saved
from tests.test_investor_publication import publication_db as publication_db


class FixtureTransport:
    def __init__(self, outcomes=("delivered",)):
        self.calls = []
        self.outcomes = iter(outcomes)

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


async def enable(*, idempotent=False, actor="api"):
    settings = base(settings_encryption_key=Fernet.generate_key().decode())
    async with session_scope() as session:
        await store_for(session, settings).apply(
            {
                "notification_external_enabled": True,
                "notification_webhook_url": "https://receiver.example/notify",
                "notification_webhook_token": "fixture-token",
                "notification_webhook_idempotent": idempotent,
            },
            actor=actor,
        )
    return settings


async def fresh_change(settings, case_id, *, stage="review_required"):
    async with session_scope() as session:
        await MonitoringService(session, settings)._upsert(
            "fixture:" + case_id,
            case_id,
            {
                "category": "major_change",
                "stage": stage,
                "private_prompt": "DO NOT EXPORT",
                "source_html": "<script>secret</script>",
                "reasons": ["private details"],
            },
        )


async def rows():
    async with session_scope() as session:
        return list(
            await session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))
        )


async def test_default_off_env_and_import_never_grant_consent_no_backlog(publication_db):
    candidate, _, _, _ = await saved()
    case_id = str(candidate.thesis.case_ref.id)
    settings = base(
        notification_external_enabled=True,
        notification_webhook_url="https://receiver.example/notify",
    )
    assert not any(key.startswith("notification_") for key in env_import_values(settings))
    async with session_scope() as session:
        assert await configuration(session, settings) is None
    await fresh_change(settings, case_id)
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) is None
    assert not await rows()
    settings = await enable(actor="import")
    await fresh_change(settings, case_id, stage="reviewed")
    assert not await rows()
    async with session_scope() as session:
        await store_for(session, settings).apply(
            {"notification_external_enabled": False}, actor="api"
        )
        await store_for(session, settings).apply(
            {"notification_external_enabled": True}, actor="api"
        )
    assert await deliver_one(settings, transport=transport) is None
    assert not transport.calls


async def test_delivery_retry_only_same_frozen_payload_stable_id_no_model(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable(idempotent=True)
    await fresh_change(settings, str(candidate.thesis.case_ref.id))
    frozen = (await rows())[0]
    transport = FixtureTransport([TimeoutError("secret URL"), "delivered"])
    now = datetime.now(UTC) + timedelta(seconds=1)
    assert await deliver_one(settings, transport=transport, now=now) == "unknown"
    assert (await rows())[0].state == "retry"
    assert (
        await deliver_one(settings, transport=transport, now=now + timedelta(seconds=30))
        == "delivered"
    )
    assert transport.calls[0] == transport.calls[1]
    assert json.loads(transport.calls[0]["body"])["id"] == frozen.id
    assert (
        b"DO NOT EXPORT" not in transport.calls[0]["body"]
        and b"script" not in transport.calls[0]["body"]
    )
    assert (await rows())[0].payload["attempts"] == 2
    async with session_scope() as session:
        from investment_agent.storage.models.agent import AgentModelRun

        assert await session.scalar(select(func.count()).select_from(AgentModelRun)) == 0
        url = await session.get(AppSetting, "notification_webhook_url")
        assert url.value_json is None and "receiver.example" not in url.ciphertext


async def test_unknown_without_receiver_idempotency_stops_after_one_attempt(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable()
    await fresh_change(settings, str(candidate.thesis.case_ref.id))
    transport = FixtureTransport(["unknown"])
    assert await deliver_one(settings, transport=transport) == "unknown"
    assert (
        await deliver_one(settings, transport=transport, now=datetime.now(UTC) + timedelta(days=1))
        is None
    )
    assert len(transport.calls) == 1 and (await rows())[0].state == "unknown"


async def test_restart_lost_ack_retries_only_idempotent_bytes(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable(idempotent=True)
    await fresh_change(settings, str(candidate.thesis.case_ref.id))
    frozen = (await rows())[0]
    now = datetime.now(UTC)
    async with session_scope() as session:
        row = await session.get(NotificationDelivery, frozen.id)
        row.state = "sending"
        row.payload = {**row.payload, "attempts": 1, "sent_at": now.isoformat()}
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport, now=now + timedelta(seconds=29)) is None
    assert (
        await deliver_one(settings, transport=transport, now=now + timedelta(seconds=31))
        == "delivered"
    )
    assert json.loads(transport.calls[0]["body"]) == frozen.payload["body"]


@pytest.mark.parametrize("action", ["mute", "pause", "archive", "disable", "destination"])
async def test_live_delivery_controls_suppress_without_replay(publication_db, action):
    candidate, _, _, _ = await saved()
    settings = await enable()
    case_id = str(candidate.thesis.case_ref.id)
    await fresh_change(settings, case_id)
    if action in {"disable", "destination"}:
        async with session_scope() as session:
            await store_for(session, settings).apply(
                {"notification_external_enabled": False}
                if action == "disable"
                else {"notification_webhook_url": "https://changed.example/notify"},
                actor="api",
            )
    else:
        ref = await case_ref(candidate)
        async with session_scope() as session:
            await execute(
                session, CaseAction(case_ref=ref, action=action, reason="explicit fixture control")
            )
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) is None
    assert (await rows())[0].state == "suppressed" and not transport.calls
    if action in {"mute", "pause"}:
        ref = await case_ref(candidate)
        async with session_scope() as session:
            await execute(
                session,
                CaseAction(
                    case_ref=ref,
                    action="unmute" if action == "mute" else "resume",
                    reason="explicit fixture resume",
                ),
            )
        assert await deliver_one(settings, transport=transport) is None
        await fresh_change(settings, case_id, stage="reviewed")
        assert await deliver_one(settings, transport=transport) == "delivered"
        assert len(transport.calls) == 1


async def test_no_change_quiet_and_current_revisions_coalesce(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable()
    case_id = str(candidate.thesis.case_ref.id)
    await fresh_change(settings, case_id)
    await fresh_change(settings, case_id)
    assert len(await rows()) == 1
    async with session_scope() as session:
        station = await session.scalar(
            select(InvestorNotification).where(InvestorNotification.key == "fixture:" + case_id)
        )
        await freeze(session, settings, station)
    assert (await rows())[0].state == "pending"
    await fresh_change(settings, case_id, stage="reviewed")
    assert sorted(r.state for r in await rows()) == ["pending", "superseded"]
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) == "delivered"
    assert json.loads(transport.calls[0]["body"])["stage"] == "reviewed"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://user:pass@example.com/",
        "https://example.com:444/",
        "https://example.com/?token=secret",
        "https://example.com/#fragment",
        "https://example.com/\r\nheader",
        "https://[::1]/",
    ],
)
def test_endpoint_authority_rejected(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


async def test_production_adapter_dns_public_validation_before_socket(monkeypatch):
    import investment_agent.investor.notification_http as module
    from investment_agent.evidence.disclosure_http import public_targets

    def dns(host):
        return public_targets(
            host, resolver=lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))]
        )

    monkeypatch.setattr(module, "public_targets", dns)
    with pytest.raises(ValueError, match="non_public"):
        await HTTPSWebhook().send(
            endpoint="https://fixture.example/", token="", delivery_id="id", body=b"{}"
        )


async def test_independent_deliverers_claim_once_before_fixture_transport(publication_db):
    import asyncio

    candidate, _, _, _ = await saved()
    settings = await enable(idempotent=True)
    await fresh_change(settings, str(candidate.thesis.case_ref.id))
    transport = FixtureTransport()
    results = await asyncio.gather(
        deliver_one(settings, transport=transport), deliver_one(settings, transport=transport)
    )
    assert sorted(str(r) for r in results) == ["None", "delivered"]
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "status,body,outcome",
    [
        (204, b"", "delivered"),
        (307, b"", "failed"),
        (500, b"", "unknown"),
        (200, b"x" * 4097, "unknown"),
    ],
)
async def test_actual_adapter_fixture_socket_bounded_no_redirect_or_ambient_auth(
    monkeypatch, status, body, outcome
):
    import investment_agent.investor.notification_http as module

    calls = []

    class Response:
        def __init__(self):
            self.status = status
            self.parts = iter([body, b""])

        def getheader(self, name, default=None):
            return (
                "identity"
                if name == "Content-Encoding"
                else str(len(body))
                if name == "Content-Length"
                else default
            )

        def read1(self, limit):
            return next(self.parts)

    class Socket:
        def __init__(self, host, target, timeout):
            calls.append((host, target, timeout))

        def abort(self):
            pass

        def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(module, "public_targets", lambda host: ("93.184.216.34",))
    monkeypatch.setattr(module, "PinnedHTTPS", Socket)
    result = await HTTPSWebhook().send(
        endpoint="https://fixture.example/hook", token="", delivery_id="stable", body=b"{}"
    )
    assert result == outcome
    assert calls[0] == ("fixture.example", "93.184.216.34", 10)
    assert calls[1][0:2] == ("POST", "/hook")
    assert calls[1][2]["headers"]["Idempotency-Key"] == "stable"
    assert "Authorization" not in calls[1][2]["headers"] and "Cookie" not in calls[1][2]["headers"]
    assert len(calls) == 2


async def test_old_sending_crash_new_revision_recovery_never_retries_old_payload(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable(idempotent=True)
    case_id = str(candidate.thesis.case_ref.id)
    await fresh_change(settings, case_id)
    old = (await rows())[0]
    now = datetime.now(UTC)
    async with session_scope() as session:
        row = await session.get(NotificationDelivery, old.id)
        row.state = "sending"
        row.payload = {**row.payload, "attempts": 1, "sent_at": now.isoformat()}
    await fresh_change(settings, case_id, stage="reviewed")
    transport = FixtureTransport()
    assert (
        await deliver_one(settings, transport=transport, now=now + timedelta(seconds=1))
        == "delivered"
    )
    assert await deliver_one(settings, transport=transport, now=now + timedelta(seconds=31)) is None
    assert len(transport.calls) == 1 and json.loads(transport.calls[0]["body"])["revision"] == 2
    async with session_scope() as session:
        row = await session.get(NotificationDelivery, old.id)
        assert row.state == "superseded" and row.payload["prior_acknowledgement"] == "unknown"


async def test_backoff_rows_do_not_hide_later_ready_notification(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable(idempotent=True)
    case_id = str(candidate.thesis.case_ref.id)
    await fresh_change(settings, case_id)
    original = (await rows())[0]
    async with session_scope() as session:
        row = await session.get(NotificationDelivery, original.id)
        row.payload = {
            **row.payload,
            "available_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        }
        for n in range(101):
            session.add(
                NotificationDelivery(
                    id=str(n),
                    notification_key=original.notification_key,
                    case_id=case_id,
                    state="retry",
                    payload={**row.payload, "created_at": "2000-01-01T00:00:00+00:00"},
                )
            )
        session.add(
            InvestorNotification(
                key="ready:" + case_id,
                owner_id="local_owner",
                case_id=case_id,
                revision=1,
                payload={"category": "major_change", "stage": "reviewed"},
            )
        )
        await session.flush()
        station = await session.scalar(
            select(InvestorNotification).where(InvestorNotification.key == "ready:" + case_id)
        )
        await freeze(session, settings, station)
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) == "delivered"
    assert len(transport.calls) == 1


async def test_disable_reenable_between_polls_never_resurrects_old_payload(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable()
    case_id = str(candidate.thesis.case_ref.id)
    await fresh_change(settings, case_id)
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.apply({"notification_external_enabled": False}, actor="api")
        await store.apply({"notification_external_enabled": True}, actor="api")
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) is None
    assert (await rows())[0].state == "suppressed"
    await fresh_change(settings, case_id, stage="reviewed")
    assert await deliver_one(settings, transport=transport) == "delivered"
    assert len(transport.calls) == 1


async def test_mute_unmute_between_polls_invalidates_old_control_revision(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable()
    case_id = str(candidate.thesis.case_ref.id)
    await fresh_change(settings, case_id)
    for action in ("mute", "unmute"):
        ref = await case_ref(candidate)
        async with session_scope() as session:
            await execute(
                session, CaseAction(case_ref=ref, action=action, reason="explicit fixture control")
            )
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) is None
    assert (await rows())[0].state == "suppressed"
    await fresh_change(settings, case_id, stage="reviewed")
    assert await deliver_one(settings, transport=transport) == "delivered"


async def test_delivery_outbox_capacity_keeps_station_and_never_replays_blocked(publication_db):
    candidate, _, _, _ = await saved()
    settings = await enable()
    case_id = str(candidate.thesis.case_ref.id)
    async with session_scope() as session:
        for number in range(256):
            session.add(
                NotificationDelivery(
                    id=str(number),
                    notification_key="fixture-reserved",
                    case_id=case_id,
                    state="pending",
                    payload={},
                )
            )
    await fresh_change(settings, case_id)
    async with session_scope() as session:
        result = await session.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.notification_key == "fixture:" + case_id
            )
        )
        assert result.state == "capacity_blocked"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestorNotification)
                .where(InvestorNotification.key == "fixture:" + case_id)
            )
            == 1
        )
        station = await session.scalar(
            select(InvestorNotification).where(InvestorNotification.key == "fixture:" + case_id)
        )
        await freeze(session, settings, station)
        assert result.state == "capacity_blocked"

"""Authenticated source controls and zero-DML operating reads, no network."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import event, func, select

from investment_agent.config import Settings
from investment_agent.settings.loader import store_for
from investment_agent.storage import database
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow

KEY = {"X-API-Key": "secret"}
AUTH_PATH = "/supplemental-updates/sources/cninfo_szse_v1/scopes"


async def _grant_command():
    async with session_scope() as session:
        session.add(Market(code="SZSE", name="Shenzhen fixture", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="B API fixture issuer")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SZSE", ticker="300901")
        session.add(listing)
        await session.flush()
        listing_id = listing.id
    now = datetime.now(UTC)
    return {"action": "create", "operation_id": str(uuid4()), "authorization_id": str(uuid4()),
            "expected_revision": 0, "expected_owner_policy_revision": 0,
            "owner_daily_http": 24, "owner_daily_response_bytes": 128 * 1024 * 1024,
            "owner_min_request_interval_seconds": 5,
            "body": {"contract_hash": "a" * 64,
                     "listing_refs": [{"kind": "listing", "id": listing_id, "revision": 1}],
                     "doc_type_allowlist": ["annual_report_full"],
                     "history_start": (now - timedelta(days=7)).isoformat(),
                     "history_end": now.isoformat(), "expires_at": (now + timedelta(days=7)).isoformat(),
                     "min_poll_interval_seconds": 43_200, "min_http_interval_seconds": 5,
                     "max_staleness_seconds": 129_600,
                     "per_poll_query_http": 4, "per_poll_fetch": 3, "per_poll_http": 8,
                     "per_poll_response_bytes": 64 * 1024 * 1024,
                     "grant_daily_http": 24, "grant_daily_response_bytes": 128 * 1024 * 1024,
                     "storage_bytes_cap": 512 * 1024 * 1024}}


async def test_grant_controls_are_explicit_authenticated_idempotent_and_readable_closed(api_client):
    command = await _grant_command()
    assert api_client.post(AUTH_PATH, json=command).status_code == 401
    forbidden = api_client.post(AUTH_PATH, json={**command, "actor": "reviewer"}, headers=KEY)
    assert forbidden.status_code == 422
    created = api_client.post(AUTH_PATH, json=command, headers=KEY)
    assert created.status_code == 200, created.text
    assert created.json()["authorization"]["actor"] == "api"
    replay = api_client.post(AUTH_PATH, json=command, headers=KEY)
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == created.json()["receipt_id"]
    assert replay.json()["authorization"] == created.json()["authorization"]
    conflict = api_client.post(AUTH_PATH, json={**command, "owner_daily_http": 25}, headers=KEY)
    assert conflict.status_code == 409
    grant_id = command["authorization_id"]
    status = api_client.get(f"/supplemental-updates/authorizations/{grant_id}", headers=KEY)
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "active"
    # Merely granting a source does not enable a runtime switch or invent a
    # completed source window, and internal auth/quota rows stay out of counts.
    scopes = api_client.get("/supplemental-updates/sources", headers=KEY)
    assert scopes.status_code == 200 and scopes.json()["enabled"] is False
    assert scopes.json()["declared_source_count"] == 0
    assert scopes.json()["sources"] == []
    revoke = {k: v for k, v in command.items() if k != "body"}
    revoke.update(action="revoke", operation_id=str(uuid4()), expected_revision=1,
                  expected_owner_policy_revision=1, reason="fixture user revokes")
    result = api_client.post(AUTH_PATH, json=revoke, headers=KEY)
    assert result.status_code == 200, result.text
    after = api_client.get(f"/supplemental-updates/authorizations/{grant_id}", headers=KEY)
    assert after.status_code == 200 and after.json()["status"] == "revoked"
    assert after.json()["authorization"]["revision"] == 2


async def test_disclosure_reads_never_create_quota_or_refresh_authorization(api_client):
    command = await _grant_command()
    assert api_client.post(AUTH_PATH, json=command, headers=KEY).status_code == 200
    writes = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement)

    async with session_scope() as session:
        before = await session.scalar(select(func.count()).select_from(SupplementalChangeReceiptRow))
    engine = database.get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        view = api_client.get(f"/supplemental-updates/authorizations/{command['authorization_id']}", headers=KEY)
        assert view.status_code == 200, view.text
        assert view.json()["remaining_http"] is None
        assert api_client.get("/supplemental-updates/sources", headers=KEY).status_code == 200
        listing_id = command["body"]["listing_refs"][0]["id"]
        assert api_client.get(f"/supplemental-updates/scopes/{listing_id}", headers=KEY).status_code == 200
        assert api_client.get(f"/supplemental-updates/authorizations/{uuid4()}", headers=KEY).status_code == 404
        assert api_client.get(f"/supplemental-updates/authorizations/{command['authorization_id']}").status_code == 401
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert not writes
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(SupplementalChangeReceiptRow)) == before


async def test_poll_command_replays_exactly_when_closed_without_starting_collection(api_client):
    command = await _grant_command()
    granted = api_client.post(AUTH_PATH, json=command, headers=KEY)
    assert granted.status_code == 200, granted.text
    grant = granted.json()["authorization"]
    request = {"operation_id": str(uuid4()), "authorization_id": grant["authorization_id"],
               "authorization_revision": grant["revision"], "authorization_hash": grant["authorization_hash"],
               "listing_ref": grant["listing_refs"][0], "window_start": grant["history_start"],
               "window_end": grant["history_end"], "expected_source_revision": 0}
    assert api_client.post("/supplemental-updates/polls", json=request, headers=KEY).status_code == 403
    settings = Settings(_env_file=None, api_key="secret")
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply({"supplemental_rules_enabled": True, "supplemental_updates_enabled": True,
                           "supplemental_disclosure_polling_enabled": True, "disclosure_originals_enabled": True},
                          actor="api")
    # The HTTP route only persists a command; it never creates a transport.
    accepted = api_client.post("/supplemental-updates/polls", json=request, headers=KEY)
    assert accepted.status_code == 202, accepted.text
    poll_id = accepted.json()["poll_id"]
    assert api_client.get(f"/supplemental-updates/polls/{poll_id}", headers=KEY).status_code == 200
    assert api_client.get(f"/supplemental-updates/polls/{uuid4()}", headers=KEY).status_code == 404
    async with session_scope() as session:
        await store_for(session, settings).apply({"supplemental_disclosure_polling_enabled": False}, actor="api")
    replay = api_client.post("/supplemental-updates/polls", json=request, headers=KEY)
    assert replay.status_code == 202 and replay.json()["replayed"] is True
    assert replay.json()["poll_id"] == poll_id
    changed = api_client.post("/supplemental-updates/polls", json={**request, "expected_source_revision": 1}, headers=KEY)
    assert changed.status_code == 409
    new = api_client.post("/supplemental-updates/polls", json={**request, "operation_id": str(uuid4())}, headers=KEY)
    assert new.status_code == 403
    scopes = api_client.get("/supplemental-updates/sources", headers=KEY).json()
    assert scopes["declared_source_count"] == 1
    assert scopes["sources"][0]["current_status"] == "unknown"

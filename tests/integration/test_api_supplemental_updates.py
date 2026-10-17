"""Real HTTP/SQLite P4 local intake, source coverage, isolation and recovery."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import event, select

from investment_agent.config import Settings
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from tests.support.research_closure import seed_company_view, seed_listing

KEY = {"X-API-Key": "secret"}


async def _inputs():
    async with session_scope() as session:
        listing = await seed_listing(session)
        _, evidence = await seed_company_view(session, listing)
        other = await seed_listing(session, ticker="600102")
        store = store_for(session, Settings(_env_file=None, api_key="secret"))
        await store.claim_import()
        await store.apply({
            "supplemental_rules_enabled": True, "supplemental_updates_enabled": True,
        }, actor="api")
    return listing, evidence, other


async def test_local_preview_intake_replay_closed_reads_and_no_promotion(api_client):
    listing, evidence, _ = await _inputs()
    request = {"listing_ref": listing.model_dump(mode="json"), "context": "company_research",
               "source_refs": [evidence.model_dump(mode="json")]}
    preview = api_client.post("/supplemental-updates/imports/preview", json=request, headers=KEY)
    assert preview.status_code == 200, preview.text
    body = {**request, "operation_id": str(uuid4()), "manifest_hash": preview.json()["manifest_hash"],
            "expected_source_revision": preview.json()["expected_source_revision"]}
    response = api_client.post("/supplemental-updates/imports", json=body, headers=KEY)
    assert response.status_code == 202, response.text
    receipt = response.json()["receipt"]
    assert receipt["kind"] == "evidence_revision"
    assert receipt["payload"]["coverage"] == "explicit_saved_refs"
    async with session_scope() as session:
        count = len(list(await session.scalars(select(SupplementalChangeReceiptRow))))
        await store_for(session, Settings(_env_file=None, api_key="secret")).apply({
            "supplemental_updates_enabled": False}, actor="api")
    replay = api_client.post("/supplemental-updates/imports", json=body, headers=KEY)
    assert replay.status_code == 202 and replay.json() == response.json()
    changed = api_client.post("/supplemental-updates/imports", json={**body, "manifest_hash": "0" * 64}, headers=KEY)
    assert changed.status_code == 409
    forbidden = api_client.post("/supplemental-updates/imports", json={**body, "operation_id": str(uuid4())}, headers=KEY)
    assert forbidden.status_code == 403
    view = api_client.get(f"/supplemental-updates/scopes/{listing.id}", headers=KEY)
    assert view.status_code == 200, view.text
    assert view.json()["enabled"] is False
    assert view.json()["sources"][0]["coverage"] == "explicit_saved_refs"
    # The read path exposes a real boundary before any timer creates refresh state.
    source = view.json()["sources"][0]
    assert datetime.fromisoformat(view.json()["next_due_at"]) == (
        datetime.fromisoformat(source["last_success_at"]) + timedelta(seconds=86400))
    assert view.json()["overdue"] is False
    assert "没有采集到" in view.json()["notice"]
    exact = api_client.get(f"/supplemental-updates/receipts/{receipt['id']}", headers=KEY)
    assert exact.status_code == 200 and exact.json() == receipt
    async with session_scope() as session:
        assert len(list(await session.scalars(select(SupplementalChangeReceiptRow)))) == count


async def test_import_rejects_cross_subject_untrusted_fields_bad_hash_and_auth(api_client):
    listing, evidence, other = await _inputs()
    request = {"listing_ref": listing.model_dump(mode="json"), "source_refs": [evidence.model_dump(mode="json")]}
    assert api_client.post("/supplemental-updates/imports/preview", json=request).status_code == 401
    wrong = api_client.post("/supplemental-updates/imports/preview", json={**request, "listing_ref": other.model_dump(mode="json")}, headers=KEY)
    assert wrong.status_code == 422
    assert api_client.post("/supplemental-updates/imports", json={**request, "operation_id": str(uuid4()), "manifest_hash": "0" * 64}, headers=KEY).status_code == 422
    forged = api_client.post("/supplemental-updates/imports/preview", json={**request, "verified": True}, headers=KEY)
    assert forged.status_code == 422
    assert api_client.get(f"/supplemental-updates/receipts/{uuid4()}", headers=KEY).status_code == 404
    assert api_client.get("/supplemental-updates/batches/999999", headers=KEY).status_code == 404
    assert api_client.get("/supplemental-updates/sources?cursor=garbage", headers=KEY).status_code == 422


async def test_all_p4_reads_and_preview_issue_no_dml(api_client):
    from investment_agent.storage import database
    listing, evidence, _ = await _inputs()
    statements = []
    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            statements.append(statement)
    # The API and fixture share the real file-backed engine.
    engine = database.get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert api_client.get(f"/supplemental-updates/scopes/{listing.id}", headers=KEY).status_code == 200
        assert api_client.get("/supplemental-updates/sources", headers=KEY).status_code == 200
        assert api_client.post("/supplemental-updates/imports/preview", json={
            "listing_ref": listing.model_dump(mode="json"), "source_refs": [evidence.model_dump(mode="json")]}, headers=KEY).status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert statements == []


async def test_same_saved_manifest_new_operation_does_not_refanout_or_renew_coverage(api_client):
    listing, evidence, _ = await _inputs()
    request = {"listing_ref": listing.model_dump(mode="json"), "context": "company_research",
               "source_refs": [evidence.model_dump(mode="json")]}
    preview = api_client.post("/supplemental-updates/imports/preview", json=request, headers=KEY).json()
    body = {**request, "operation_id": str(uuid4()), "manifest_hash": preview["manifest_hash"]}
    assert api_client.post("/supplemental-updates/imports", json=body, headers=KEY).status_code == 202
    before = api_client.get(f"/supplemental-updates/scopes/{listing.id}", headers=KEY).json()["sources"][0]
    duplicate = api_client.post("/supplemental-updates/imports", json={**body, "operation_id": str(uuid4()), "expected_source_revision": 1}, headers=KEY)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["state"] == "unchanged"
    assert duplicate.json()["receipt"]["kind"] == "source_attempt"
    after = api_client.get(f"/supplemental-updates/scopes/{listing.id}", headers=KEY).json()["sources"][0]
    assert before == after


async def test_source_cursor_keeps_its_scope_snapshot_and_rejects_changed_collection(api_client):
    listing, evidence, _ = await _inputs()
    # A per-listing reconciliation cursor is not a declared evidence source.
    from investment_agent.storage.repositories.supplemental_updates import (
        SupplementalUpdatesRepository,
    )
    async with session_scope() as session:
        await SupplementalUpdatesRepository(session).ensure_source(
            "local_owner", "bootstrap_industry_classification", f"listing:{listing.id}",
            "a" * 64, listing_id=int(listing.id), payload={})

    def import_context(context):
        request = {"listing_ref": listing.model_dump(mode="json"), "context": context,
                   "source_refs": [evidence.model_dump(mode="json")]}
        preview = api_client.post("/supplemental-updates/imports/preview", json=request, headers=KEY)
        assert preview.status_code == 200
        response = api_client.post("/supplemental-updates/imports", headers=KEY, json={
            **request, "operation_id": str(uuid4()), "manifest_hash": preview.json()["manifest_hash"]})
        assert response.status_code == 202, response.text

    import_context("company_research")
    import_context("holding_review")
    first = api_client.get("/supplemental-updates/sources?limit=1", headers=KEY)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor
    second = api_client.get("/supplemental-updates/sources", params={"limit": 1, "cursor": cursor}, headers=KEY)
    assert second.status_code == 200
    assert second.json()["next_cursor"] is None
    assert len(api_client.get("/supplemental-updates/sources", headers=KEY).json()["sources"]) == 2
    assert first.json()["sources"][0]["scope_key"] != second.json()["sources"][0]["scope_key"]
    import_context("candidate")
    stale = api_client.get("/supplemental-updates/sources", params={"cursor": cursor}, headers=KEY)
    assert stale.status_code == 409

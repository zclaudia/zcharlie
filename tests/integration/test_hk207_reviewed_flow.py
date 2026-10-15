"""Reviewed imports feed the real API and exact publication input selection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.evidence.store import EvidenceService
from investment_agent.materials.bootstrap import initialize_pilot
from investment_agent.materials.publication_prepare import prepare_material_publication
from investment_agent.materials.reviewed_import import import_reviewed_annual
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.research_material import ResearchDataset
from tests.unit.test_annual_validation import _make_package


@pytest.mark.asyncio
async def test_all_eight_imports_are_durable_idempotent_and_read_only_in_the_api(
    api_client, tmp_path, monkeypatch
):
    manifest, source_root, _ = _make_package(tmp_path)
    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    imported_at = datetime(2026, 10, 16, 12, tzinfo=UTC)
    async with session_scope() as session:
        setup = await initialize_pilot(session, identities)
        first = await import_reviewed_annual(
            session, manifest_path=manifest, source_root=source_root,
            data_dir=tmp_path, identities=identities, company_ids=setup.company_ids,
            imported_at=imported_at,
        )
        assert all(company.imported for company in first.companies)
        assert len(first.saved) == 8
        repeated = await import_reviewed_annual(
            session, manifest_path=manifest, source_root=source_root,
            data_dir=tmp_path, identities=identities, company_ids=setup.company_ids,
            imported_at=imported_at + timedelta(days=1),
        )
        assert [(item.id, item.revision) for item in repeated.saved] == [
            (item.id, item.revision) for item in first.saved
        ]
        assert not any(item.created for item in repeated.saved)
        listings = {identity.company_key: setup.listings[(identity.market_code, identity.ticker)].id
                    for identity in identities}
        rows = (await session.scalars(select(ResearchDataset))).all()
        for row in rows:
            for document in row.payload["review"]["documents"]:
                assert hashlib.sha256((tmp_path / document["raw_ref"]).read_bytes()).hexdigest() == document["sha256"]

    # Current reads must remain usable when the research capture directory moves.
    source_root.rename(source_root.with_name("moved-originals"))

    def no_network(*_args, **_kwargs):
        raise AssertionError("financial GET attempted a real network connection")

    import socket

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    for identity in identities:
        response = api_client.get(
            f"/research/listings/{listings[identity.company_key]}/financial-context",
            headers={"X-API-Key": "secret"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["freshness"]["last_verified_on"] == "2026-10-15"
        assert body["freshness"]["last_imported_at"].startswith("2026-10-16T12:00:00")
        assert body["freshness"]["last_success_at"] is None
        assert body["freshness"]["last_attempt_at"] is None
        assert body["freshness"]["latestness"] == "checked_through"
        assert body["quote_currency"] == identity.quote_currency
        assert body["report_currency"] == identity.report_currency
        assert body["capabilities"]["online_refresh"] is False
        assert body["capabilities"]["financials"] is True
        assert "raw_archive_missing" not in {item["code"] for item in body["statuses"]}
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(ResearchDataset)) == 8
        assert await session.scalar(select(func.count()).select_from(PipelineRun)) == 0


@pytest.mark.asyncio
async def test_reviewed_selection_prepares_precise_calculations_and_survives_new_session(
    api_client, tmp_path
):
    manifest, source_root, _ = _make_package(tmp_path)
    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    as_of = datetime(2026, 10, 17, 12, tzinfo=UTC)
    async with session_scope() as session:
        setup = await initialize_pilot(session, identities)
        report = await import_reviewed_annual(
            session, manifest_path=manifest, source_root=source_root,
            data_dir=tmp_path, identities=identities, company_ids=setup.company_ids,
            imported_at=as_of - timedelta(days=1),
        )
        assert len(report.saved) == 8
        listing = setup.listings[("NASDAQ", "AAPL")]
        prepared = await prepare_material_publication(session, listing_id=listing.id, as_of=as_of)
        usable = [item for item in prepared.metrics if item.calculation_ref]
        assert usable
        assert not any("ambiguous_source" in gap for item in prepared.metrics for gap in item.gaps)
        saved_refs = [(item.key, item.calculation_ref, item.fact_refs) for item in usable]
    async with session_scope() as session:
        service = EvidenceService(session)
        for key, ref, fact_refs in saved_refs:
            read = await service.get_calculation("local_owner", ref, as_of)
            assert read.data is not None
            assert read.data.results[key].value is not None
            assert read.data.fact_refs == fact_refs

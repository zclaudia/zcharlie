"""Financial context marks legacy rows without acquisition evidence explicitly."""

from __future__ import annotations

from datetime import UTC, datetime

from investment_agent.materials.archive import (
    ArchiveCapture,
    DatasetLink,
    ResponseEvidence,
    archive_capture,
    identity_snapshot,
    period_snapshot,
    record_acquisition,
)
from investment_agent.materials.context import financial_context
from investment_agent.materials.contracts import FactPeriod, PeriodBasis
from investment_agent.materials.store import MaterialStore
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.listing import ListingRepository
from tests.integration.test_material_collection_job import _draft
from tests.unit._hk_us_support import pilot_identities, seed_pilot_listings


def _identity():
    return next(item for item in pilot_identities() if item.company_key == "hsbc-holdings")


async def _seed_dataset(tmp_path):
    async with session_scope() as session:
        await seed_pilot_listings(session)
        listing = await ListingRepository(session).get_by_ticker("HKEX", "00005")
        assert listing is not None
        period = FactPeriod(
            start=None,
            end=datetime(2024, 12, 31, tzinfo=UTC).date(),
            basis=PeriodBasis.ANNUAL,
        )
        saved = await MaterialStore(session, data_dir=tmp_path).save(
            _draft(source="eastmoney.hk.native.v1", subject_id=listing.company_id)
        )
        await session.commit()
        return listing.id, listing.company_id, saved, period


async def test_legacy_material_has_raw_archive_missing_without_capability_changes(
    api_client, tmp_path
) -> None:
    del api_client
    listing_id, _, _, _ = await _seed_dataset(tmp_path)
    async with session_scope() as session:
        view = await financial_context(session, listing_id)

    status = next(item for item in view.statuses if item.code == "raw_archive_missing")
    assert status.affected == ("交易所年报原件",)
    assert "归档" in status.next_step
    assert view.capabilities == {
        "financials": True,
        "price": False,
        "annual_pe": False,
        "dividends": False,
        "pb": False,
        "online_refresh": False,
    }
    assert view.facts


async def test_acquisition_link_removes_legacy_gap_without_network_or_mutation(
    api_client, tmp_path
) -> None:
    del api_client
    listing_id, company_id, saved, period = await _seed_dataset(tmp_path)
    at = datetime(2026, 10, 12, tzinfo=UTC)
    archived = archive_capture(
        tmp_path,
        ArchiveCapture(
            identity=identity_snapshot(_identity()),
            subject_id=company_id,
            source="eastmoney.hk.native.v1",
            adapter_version="fake-1",
            periods=(period_snapshot(period),),
            expected_requests=("income.json",),
            responses=(
                ResponseEvidence(
                    name="income.json",
                    content=b'{"exact":true}',
                    source_url="https://example.invalid/income",
                    media_type="application/json",
                    http_status=200,
                    fetched_at=at,
                ),
            ),
            captured_at=at,
        ),
    )
    async with session_scope() as session:
        await record_acquisition(
            session,
            archived,
            datasets=(DatasetLink(saved.id, saved.revision, saved.content_hash),),
        )
        await session.commit()
        view = await financial_context(session, listing_id)

    assert "raw_archive_missing" not in {item.code for item in view.statuses}
    assert view.capabilities["financials"] is True
    assert view.capabilities["price"] is False
    assert view.facts

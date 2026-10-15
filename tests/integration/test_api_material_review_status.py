"""API lifecycle timestamps remain independent through queued/failed refreshes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from investment_agent.materials.jobs import MATERIAL_COLLECTION
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchDataset
from tests.integration.test_api_research_materials import (
    H,
    seed_listings,
    store_apple_material,
    store_hsbc_material,
    url_context,
    url_refresh,
)
from tests.unit.test_material_review_status import _reviewed_dataset


async def test_get_separates_request_attempt_import_and_original_review(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    listing_id = seeded["HKEX/00005"]
    await store_hsbc_material(tmp_path, listing_id)

    queued = api_client.post(url_refresh(listing_id), headers=H, json={"reason": "user_refresh"})
    assert queued.status_code == 202
    body = api_client.get(url_context(listing_id), headers=H).json()

    assert body["refresh"]["status"] == "queued"
    assert body["refresh"]["started_at"] is None
    assert body["freshness"]["last_requested_at"] is not None
    assert body["freshness"]["last_requested_at"].endswith(("Z", "+00:00"))
    assert body["freshness"]["last_attempt_at"] is None
    assert body["freshness"]["last_success_at"] is None
    assert body["freshness"]["last_imported_at"] is None
    assert body["freshness"]["last_verified_on"] is None
    assert body["freshness"]["latestness"] == "unknown"
    assert "latestness_unknown" in {item["code"] for item in body["statuses"]}
    assert body["freshness"]["sources"] == {
        "hk_annual_reports": {"enabled": False, "licensing": "unknown", "state": "disabled"},
        "us_issuer_facts": {"enabled": False, "licensing": "unknown", "state": "disabled"},
    }


async def test_failed_and_queued_runs_keep_last_successful_provider_collection(
    api_client, tmp_path
) -> None:
    seeded = await seed_listings()
    listing_id = seeded["HKEX/00005"]
    await store_hsbc_material(tmp_path, listing_id)
    async with session_scope() as session:
        listing = await session.get(Listing, listing_id)
        assert listing is not None
        dataset = await session.scalar(
            select(ResearchDataset)
            .where(ResearchDataset.subject_id == listing.company_id)
            .order_by(ResearchDataset.id.desc())
        )
        assert dataset is not None
        started = datetime.now(UTC)
        succeeded = PipelineRun(
            kind=MATERIAL_COLLECTION,
            listing_id=listing_id,
            triggered_by="fixture:provider-success",
            started_at=started,
            claimed_at=started,
            finished_at=started + timedelta(seconds=3),
            status="success",
            stage="finished",
            attempt_count=1,
            max_attempts=3,
            request_json={"reason": "scheduled_refresh"},
            metadata_json={
                "source_status": {dataset.source: "saved"},
                "saved": [{"dataset_id": dataset.id}],
            },
        )
        session.add(succeeded)
        await session.flush()
        success_time = succeeded.finished_at

        failed_started = started + timedelta(minutes=1)
        failed = PipelineRun(
            kind=MATERIAL_COLLECTION,
            listing_id=listing_id,
            triggered_by="fixture:provider-failure",
            started_at=failed_started,
            claimed_at=failed_started,
            finished_at=failed_started + timedelta(seconds=2),
            status="failed",
            stage="finished",
            attempt_count=1,
            max_attempts=1,
            request_json={"reason": "retry_after_failure"},
            metadata_json={},
        )
        session.add(failed)
        await session.commit()

    after_failure = api_client.get(url_context(listing_id), headers=H).json()
    assert after_failure["refresh"]["status"] == "failed"
    assert after_failure["refresh"]["reason"] == "刷新运行失败"
    expected_success = success_time.isoformat().replace("+00:00", "Z")
    assert after_failure["freshness"]["last_success_at"] == expected_success
    assert after_failure["freshness"]["last_attempt_at"] is not None
    assert after_failure["freshness"]["last_attempt_at"].endswith(("Z", "+00:00"))
    assert after_failure["refresh"]["started_at"].endswith(("Z", "+00:00"))
    assert after_failure["refresh"]["completed_at"].endswith(("Z", "+00:00"))

    queued = api_client.post(url_refresh(listing_id), headers=H, json={})
    assert queued.status_code == 202
    after_queue = api_client.get(url_context(listing_id), headers=H).json()
    assert after_queue["refresh"]["status"] == "queued"
    assert after_queue["freshness"]["last_success_at"] == expected_success


async def test_old_unreviewed_facts_never_gain_a_today_latest_claim(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    listing_id = seeded["NASDAQ/AAPL"]
    await store_apple_material(tmp_path, listing_id)
    body = api_client.get(url_context(listing_id), headers=H).json()

    assert body["freshness"]["last_verified_on"] is None
    assert body["freshness"]["latestness"] == "unknown"
    assert body["freshness"]["latestness_checked_on"] is None
    assert "material_stale" not in {item["code"] for item in body["statuses"]}


async def test_review_date_and_import_capture_time_are_reported_separately(api_client) -> None:
    seeded = await seed_listings()
    listing_id = seeded["NASDAQ/AAPL"]
    async with session_scope() as session:
        listing = await session.get(Listing, listing_id)
        assert listing is not None
        reviewed = _reviewed_dataset()
        reviewed.subject_id = listing.company_id
        session.add(reviewed)
        await session.commit()

    body = api_client.get(url_context(listing_id), headers=H).json()
    assert body["freshness"]["last_verified_on"] == "2026-10-15"
    assert body["freshness"]["last_imported_at"].startswith("2026-10-15T")
    assert body["freshness"]["last_imported_at"].endswith(("Z", "+00:00"))
    assert body["freshness"]["last_success_at"] is None
    assert body["freshness"]["latestness"] == "checked_through"
    assert body["freshness"]["latestness_checked_on"] == "2026-10-15"

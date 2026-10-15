"""Bank roster selection is isolated from the legacy baijiu cohort."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from investment_agent.cli.commands.peer_cohort import import_sample_roster
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.peer import PeerCohortSnapshot
from investment_agent.storage.repositories.listing import ListingRepository

FIXTURE = Path(__file__).parents[1] / "fixtures/bank_run02"
CODES = [
    "601398.SH", "601939.SH", "601288.SH", "600036.SH", "601166.SH",
    "000001.SZ", "600919.SH", "601009.SH", "002142.SZ", "601128.SH",
]


@pytest.mark.asyncio
async def test_bank_sample_is_explicit_partial_cohort_with_bank_metric_pack(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path}/cohort.db")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            listings = ListingRepository(session)
            for code in CODES:
                await listings.ensure_a_share_listing(code, company_name=code)
            result = await import_sample_roster(
                session, sample_path=FIXTURE / "sample.json", industry="bank",
                cohort_key="bank_current", acquired_at=datetime(2026, 10, 15, tzinfo=UTC),
            )
            await session.commit()
            row = result.snapshot
            assert row.cohort_key == "bank_current" and row.verification == "candidate"
            payload = row.payload
            assert payload["metric_pack"] == "bank_annual_v1"
            assert payload["index_code"] is None
            assert payload["source_differences"]["not_a_complete_sector_list"] is True
            assert len(payload["entries"]) == len(payload["included"]) == 10
            assert [entry["ticker"] for entry in payload["entries"]] == CODES
            assert all(entry["classification"]["industry_code"] for entry in payload["entries"])
            assert "not a bank index constituent list" in payload["notes"][0]
            # The legacy route keeps the prior cohort/hash semantics; explicit
            # bank selection creates a separate key and payload family.
            assert await session.scalar(select(PeerCohortSnapshot.id).where(
                PeerCohortSnapshot.cohort_key == "baijiu_current"
            )) is None
    finally:
        await engine.dispose()

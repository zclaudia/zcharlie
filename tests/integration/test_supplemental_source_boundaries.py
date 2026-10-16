from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.earnings_events import EarningsForecastRecord
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.supplemental_rules import ResearchEventRevision
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import ResearchEvent
from investment_agent.supplemental_rules.event_sources import (
    load_candidate_event_facts,
    load_research_event,
)


@pytest.mark.asyncio
async def test_existing_event_adapter_returns_candidate_fact_at_acquisition_time(db_session) -> None:
    db_session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Event issuer")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600002")
    db_session.add(listing)
    await db_session.flush()
    capture = datetime(2026, 10, 15, 2, tzinfo=UTC)
    db_session.add(EarningsForecastRecord(
        listing_id=listing.id, report_period=date(2025, 12, 31), notice_date=date(2026, 10, 15),
        forecast_type="预增", indicator="归母净利润", profit_low=Decimal("100"),
        profit_high=Decimal("120"), yoy_low=Decimal("0.1"), yoy_high=Decimal("0.2"),
        content="saved forecast", provider="akshare", captured_at=capture, raw_ref="run:1",
        created_at=capture, updated_at=capture,
    ))
    # The captured/notice timestamps claim this was old information, but the
    # immutable row was first persisted after the cutoff. It is backfill, not
    # evidence that was actually available at the historical instant.
    db_session.add(EarningsForecastRecord(
        listing_id=listing.id, report_period=date(2024, 12, 31), notice_date=date(2026, 10, 15),
        forecast_type="预增", indicator="归母净利润", profit_low=Decimal("50"),
        profit_high=Decimal("60"), yoy_low=Decimal("0.1"), yoy_high=Decimal("0.2"),
        content="late-saved old notice", provider="akshare", captured_at=capture,
        raw_ref="run:late", created_at=datetime(2026, 10, 16, 1, tzinfo=UTC),
    ))
    await db_session.flush()
    facts, diagnostics = await load_candidate_event_facts(
        db_session, listing.id, datetime(2026, 10, 15, 18, tzinfo=UTC)
    )
    assert any(item.startswith("earnings_forecast_row_persisted_after_cutoff:")
               for item in diagnostics)
    assert len(facts) == 1
    assert facts[0]["semantic_role"] == "candidate_fact"
    assert facts[0]["verification"] == "candidate"
    assert facts[0]["date_precision"] == "date"
    assert facts[0]["available_from"] == "2026-10-15T16:00:00+00:00"
    assert any(item.startswith("earnings_forecast_row_persisted_after_cutoff:")
               for item in diagnostics)

    hidden, _ = await load_candidate_event_facts(
        db_session, listing.id, datetime(2026, 10, 15, 15, tzinfo=UTC)
    )
    assert hidden == ()


@pytest.mark.asyncio
async def test_research_event_requires_exact_owner_cutoff_and_visibility(db_session) -> None:
    now = datetime(2026, 11, 1, 4, tzinfo=UTC)
    event_id = uuid4()
    event = ResearchEvent(
        id=event_id, revision=1, owner_id="local_owner", event_key="report:1", title="User report",
        event_type="report", jurisdiction="CN", affected_products=(), affected_subjects=(),
        policy_stage="unknown", published_at=None, effective_from=None, effective_to=None,
        acquired_at=now - timedelta(minutes=3), available_from=now - timedelta(minutes=3),
        source_refs=(), verification="user_report", conflict=False,
    )
    event = event.model_copy(update={"content_hash": canonical_hash(event.model_dump(mode="json", exclude={"content_hash"}))})
    db_session.add(ResearchEventRevision(
        id=str(event_id), revision=1, owner_id="local_owner", previous_ref=None,
        event_key=event.event_key, event_aliases=[], published_at=None, effective_from=None,
        effective_to=None, acquired_at=event.acquired_at, available_from=event.available_from,
        created_at=now - timedelta(minutes=2), content_hash=event.content_hash,
        payload=event.model_dump(mode="json"),
    ))
    await db_session.flush()
    ref = ObjectRef(kind="research_event", id=event_id, revision=1)
    loaded = await load_research_event(db_session, "local_owner", ref, now)
    assert loaded is not None
    assert loaded.verification == "user_report"
    absent_ref = ObjectRef(kind="research_event", id=uuid4(), revision=1)
    assert await load_research_event(db_session, "local_owner", absent_ref, now) is None
    assert await load_research_event(db_session, "local_owner", ref, now,
                                     visible_at=now - timedelta(minutes=3)) is None
    assert await load_research_event(db_session, "local_owner", ref,
                                     now - timedelta(minutes=4)) is None


async def test_event_source_rejects_forged_content_hash_instead_of_calling_it_unknown(db_session):
    now = datetime(2026, 11, 1, 4, tzinfo=UTC)
    identity = uuid4()
    event = ResearchEvent(id=identity, revision=1, owner_id="local_owner", event_key="forged",
        title="Synthetic forged record", event_type="report", jurisdiction="CN",
        affected_products=(), affected_subjects=(), policy_stage="unknown",
        acquired_at=now, available_from=now, source_refs=(), verification="user_report",
        conflict=False, content_hash="a"*64)
    db_session.add(ResearchEventRevision(id=str(identity), revision=1, owner_id="local_owner",
        previous_ref=None, event_key="forged", event_aliases=[], published_at=None,
        effective_from=None, effective_to=None, acquired_at=now, available_from=now,
        created_at=now, content_hash="a"*64, payload=event.model_dump(mode="json")))
    await db_session.flush()
    with pytest.raises(ValueError, match="recomputed content"):
        await load_research_event(db_session, "local_owner", event.reference, now)

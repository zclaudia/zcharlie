"""S13/A2: real fixture acquisition, parser conflict/correction, saved search,
then native maintenance must remain quiet without a published opinion.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from investment_agent.evidence.disclosures import DisclosureService, FetchRequest
from investment_agent.evidence.documents import parse_staged, save_candidates
from investment_agent.evidence.search import SearchRequest, search_saved
from investment_agent.investor.monitoring import MonitoringService
from investment_agent.investor.notifications import deliver_one
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentModelRun, AgentTask
from investment_agent.storage.models.monitoring import InvestorNotification, NotificationDelivery
from tests.support.disclosure import PUBLISHED, FixtureHTTP, discovery
from tests.support.execution import execution_db as execution_db
from tests.support.notifications import FixtureTransport, enable
from tests.support.originals import html, original, snapshot


async def test_s13_actual_original_conflict_correction_search_no_false_delivery(execution_db):
    settings = await enable(idempotent=True)
    async with session_scope() as session:
        payload, body, request = await original(session)
        await snapshot(session, request.listing_id)
        first = await save_candidates(
            session, payload, body, await parse_staged(body, "text/html"), request
        )
        assert first["facts"][0]["verification"] == "verified"
        old_ref = first["fact_refs"][0]

    async def newer(number, correction):
        async with session_scope() as session:
            http = FixtureHTTP()
            http.items[0].update(
                announcementId=number,
                announcementTime=int((PUBLISHED + timedelta(days=1)).timestamp() * 1000),
                adjunctUrl=f"finalpage/2026-10-09/{number}.html",
            )
            http.body, http.mime = html(value="20,000.00", correction=correction), "text/html"
            service = DisclosureService.for_tests(session, http)
            found = await service.execute(uuid4(), discovery(request.listing_id))
            acquired = await service.execute(
                uuid4(),
                FetchRequest(
                    listing_id=request.listing_id,
                    item_id=UUID(found["items"][0]["id"]),
                    as_of=datetime.now(UTC),
                ),
            )
            if correction:
                await snapshot(session, request.listing_id, value=200000000)
            current = request.model_copy(
                update={"original_id": UUID(acquired["original"]["id"]), "as_of": datetime.now(UTC)}
            )
            return await save_candidates(
                session,
                acquired["original"],
                http.body,
                await parse_staged(http.body, "text/html"),
                current,
            )

    conflicting = await newer("67890", "")
    assert conflicting["facts"][0]["verification"] != "verified"
    corrected = await newer("99999", "<p>本公告更正原公告编号：12345</p>")
    assert old_ref in corrected["facts"][0]["correction_refs"]
    async with session_scope() as session:
        hits = await search_saved(
            session,
            SearchRequest(
                listing_ids=(request.listing_id,), query="净利润", as_of=datetime.now(UTC)
            ),
        )
        assert hits["matches"] and all(h["discovery_only"] for h in hits["matches"])
        assert any("source_corrected" in str(h) for h in hits["matches"])
        for _ in range(3):
            await MonitoringService(session, settings).maintain()
        for model in (InvestorNotification, NotificationDelivery, AgentTask, AgentModelRun):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    transport = FixtureTransport()
    assert await deliver_one(settings, transport=transport) is None and not transport.calls

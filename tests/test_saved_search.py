"""Real saved HTML/PDF manifests, literal search and time/owner fences."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.evidence.documents import parse_staged, save_candidates
from investment_agent.evidence.search import SearchRequest, search_saved
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.evidence import SavedEvidence
from tests.support.execution import execution_db as execution_db
from tests.support.originals import html, original, pdf

pytestmark = pytest.mark.usefixtures("execution_db")


@pytest.mark.parametrize("mime,body,query", [
    ("text/html", html(), "净利润"),
    ("application/pdf", pdf(["Security code: 000001", 'Risk: 100% _ \\"quoted\\"']), "Risk:"),
])
async def test_actual_saved_manifest_search_adjacent_and_fixed_cutoff(mime, body, query):
    async with session_scope() as session:
        payload, exact, request = await original(session, body=body, mime=mime)
        parsed = await parse_staged(exact, mime)
        result = await save_candidates(session, payload, exact, parsed, request)
        before = await search_saved(session, SearchRequest(listing_ids=(request.listing_id,), query=query, as_of=request.as_of))
        assert not before["matches"]  # parse availability is later than fixed cutoff
    async with session_scope() as session:
        later = SearchRequest(listing_ids=(request.listing_id,), query=query, as_of=datetime.now(UTC))
        result = await search_saved(session, later)
        assert result["matches"] and result["matches"][0]["discovery_only"]
        hit = result["matches"][0]
        assert hit["original_hash"] == payload["content_hash"]
        assert hit["fragment_hash"] and hit["adjacent_context"]
        assert hit["verification"] == "candidate"
        assert not (await search_saved(session, later.model_copy(update={"listing_ids": (request.listing_id+1,)})))["matches"]
        assert not (await search_saved(session, later, owner="other_owner"))["matches"]
        assert not (await search_saved(session, later.model_copy(update={"as_of": request.as_of, "historical_reconstruction": True})))["matches"]
        assert not (await search_saved(session, later.model_copy(update={"period_end": "2024-12-31"})))["matches"]


async def test_same_original_copy_dedup_and_literal_punctuation():
    body = html().replace(b"</body>", b'<p>100% _ &quot;quoted&quot; OR * - : \\</p></body>')
    async with session_scope() as session:
        payload, exact, request = await original(session, body=body)
        parsed = await parse_staged(exact, "text/html")
        saved = await save_candidates(session, payload, exact, parsed, request)
        manifest = await session.get(SavedEvidence, (saved["parse_artifact_ref"]["id"], 1))
        copied = dict(manifest.payload, id=str(uuid4()))
        session.add(SavedEvidence(id=copied["id"], revision=1, owner_id="local_owner", payload=copied))
    async with session_scope() as session:
        for query in ['100%', '_ "quoted"', 'OR * - : \\', "' OR 1=1 --"]:
            data = await search_saved(session, SearchRequest(listing_ids=(request.listing_id,), query=query, as_of=datetime.now(UTC)))
            if query.startswith("'"):
                assert not data["matches"]
            else:
                assert len(data["matches"]) == 1
                assert data["matches"][0]["distinct_original_count"] == 1
                assert len(data["matches"][0]["source_audit"]) == 2


def test_request_bounds():
    base = dict(listing_ids=(1,), query="净利润", as_of=datetime.now(UTC))
    for update in ({"query": " "}, {"query": "x"*121}, {"limit": 21}, {"offset": 201}, {"listing_ids": (1,1)}, {"listing_ids": (0,)}):
        with pytest.raises(ValidationError):
            SearchRequest(**(base | update))


async def test_filters_before_fair_company_windows_and_no_future_metadata():
    from datetime import timedelta
    from hashlib import sha256

    from investment_agent.evidence.contracts import EvidenceRecord
    from investment_agent.investor.common import ObjectRef

    now = datetime.now(UTC)
    async with session_scope() as session:
        payload, exact, request = await original(session)
        result = await save_candidates(session, payload, exact, await parse_staged(exact, "text/html"), request)
        fact = await session.get(SavedEvidence, (result["fact_refs"][0]["id"], 1))
        base = EvidenceRecord.model_validate(fact.payload)
        for index in range(120):
            # No original parsing involved for existing user research sources.
            text = "中文 Ä 100% company evidence " + str(index)
            record = base.model_copy(update={"id": uuid4(), "subject_ref": ObjectRef(kind="listing", id=1 if index < 110 else 2, revision=1),
                "source_ref": ObjectRef(kind="listing", id=1 if index < 110 else 2, revision=1),
                "source_kind": "user_report", "verification": "user_report", "document_provenance": None,
                "created_at": now-timedelta(seconds=1), "available_from": now-timedelta(seconds=1),
                "acquired_at": now-timedelta(seconds=1), "published_at": now-timedelta(seconds=1),
                "text": text, "content_hash": sha256(text.encode()).hexdigest()})
            session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
        future = record.model_copy(update={"id": uuid4(), "created_at": now + timedelta(microseconds=1)})
        session.add(SavedEvidence(id=str(future.id), revision=1, owner_id="local_owner", payload=future.model_dump(mode="json")))
        foreign = record.model_copy(update={"id": uuid4()})
        session.add(SavedEvidence(id=str(foreign.id), revision=1, owner_id="foreign", payload=foreign.model_dump(mode="json")))
    async with session_scope() as session:
        query = SearchRequest(listing_ids=(1,2), query="中文 Ä 100%", as_of=now, limit=20)
        data = await search_saved(session, query)
        assert data["scanned_records"] == 60 and data["record_cap_per_listing"] == 50
        assert data["window_exhausted"] and data["exhausted_listing_ids"] == [1]
        assert {h["subject_ref"]["id"] for h in data["matches"]} == {1,2}
        assert data["coverage"] == "bounded_saved_window"
        assert not (await search_saved(session, query.model_copy(update={"query": "ä"})))["matches"]
        assert not (await search_saved(session, query.model_copy(update={"field": "absent"})))["matches"]


async def test_exact_saved_comparison_period_scope_and_effective_correction():
    from datetime import date

    from investment_agent.evidence.contracts import EvidenceRecord, Period
    from investment_agent.evidence.search import SavedComparisonRequest, compare_saved
    from investment_agent.investor.common import ObjectRef
    from tests.support.originals import snapshot

    async with session_scope() as session:
        payload, exact, request = await original(session)
        await snapshot(session, request.listing_id)
        result = await save_candidates(session, payload, exact, await parse_staged(exact, "text/html"), request)
        fact = await session.get(SavedEvidence, (result["fact_refs"][0]["id"], 1))
        base = EvidenceRecord.model_validate(fact.payload)
        other = base.model_copy(update={"id": uuid4(), "subject_ref": ObjectRef(kind="listing", id=2, revision=1)})
        session.add(SavedEvidence(id=str(other.id), revision=1, owner_id="local_owner", payload=other.model_dump(mode="json")))
    async with session_scope() as session:
        refs = [dict(listing_id=1, ref=result["fact_refs"][0]), dict(listing_id=2, ref=ObjectRef(kind="evidence", id=other.id, revision=1))]
        comparison = SavedComparisonRequest(items=refs, as_of=datetime.now(UTC))
        assert (await compare_saved(session, comparison))["comparable"]
        mismatched = other.model_copy(update={"id": uuid4(), "period": Period(start=date(2024,1,1), end=date(2024,12,31), basis='period_to_date/annual')})
        session.add(SavedEvidence(id=str(mismatched.id), revision=1, owner_id="local_owner", payload=mismatched.model_dump(mode="json")))
        await session.flush()
        bad = SavedComparisonRequest(items=[refs[0], dict(listing_id=2, ref=ObjectRef(kind="evidence", id=mismatched.id, revision=1))], as_of=datetime.now(UTC))
        not_comparable = await compare_saved(session, bad)
        assert not not_comparable["comparable"] and not_comparable["ranking"] is None
        assert "period_unit_currency_scope_or_metric_mismatch" in not_comparable["not_comparable_reasons"]
        correction = base.model_copy(update={"id": uuid4(), "correction_refs": (ObjectRef(kind="evidence", id=base.id, revision=1),)})
        session.add(SavedEvidence(id=str(correction.id), revision=1, owner_id="local_owner", payload=correction.model_dump(mode="json")))
        await session.flush()
        current = await search_saved(session, SearchRequest(listing_ids=(1,), query="净利润", as_of=datetime.now(UTC)))
        assert any("source_corrected" in h["warnings"] for h in current["matches"])
        restricted = await compare_saved(session, comparison.model_copy(update={"as_of": datetime.now(UTC)}))
        assert not restricted["comparable"]


async def test_fragment_budget_is_fair_across_companies():
    from hashlib import sha256

    from investment_agent.evidence.contracts import EvidenceRecord
    from investment_agent.investor.common import ObjectRef

    body = html().replace(b"</body>", ("".join(f"<p>共同关键词 段落 {n}</p>" for n in range(240)) + "</body>").encode())
    async with session_scope() as session:
        payload, exact, request = await original(session, body=body)
        result = await save_candidates(session, payload, exact, await parse_staged(exact, "text/html"), request)
        fact = await session.get(SavedEvidence, (result["fact_refs"][0]["id"], 1))
        record = EvidenceRecord.model_validate(fact.payload).model_copy(update={"id": uuid4(),
            "subject_ref": ObjectRef(kind="listing", id=2, revision=1),
            "source_ref": ObjectRef(kind="listing", id=2, revision=1),
            "source_kind": "user_report", "verification": "user_report", "document_provenance": None,
            "text": "共同关键词 另一个公司", "content_hash": sha256("共同关键词 另一个公司".encode()).hexdigest()})
        session.add(SavedEvidence(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
    async with session_scope() as session:
        result = await search_saved(session, SearchRequest(listing_ids=(1,2), query="共同关键词", as_of=datetime.now(UTC), limit=20))
        assert result["fragment_hit_cap_reached"] and result["fragment_hit_cap_per_listing"] == 110
        assert result["matched_count_in_window"] == 111
        assert {h["subject_ref"]["id"] for h in result["matches"]} == {1,2}

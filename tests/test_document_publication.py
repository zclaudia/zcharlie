"""Actual parsed origins invalidate dependent opinions before monitor polling."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select

from investment_agent.agent.document_tools import queue_document_review
from investment_agent.agent.tools import ExecutionContext
from investment_agent.evidence.disclosures import DisclosureService, FetchRequest
from investment_agent.evidence.documents import parse_staged, save_candidates
from investment_agent.investor.common import Mutation, ObjectRef
from investment_agent.investor.contracts import Dependency, NumericNode, TextNode
from investment_agent.investor.foundations import CreateCase, InvestorService
from investment_agent.investor.publication import PublicationCandidate, PublicationService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.monitoring import ReviewCycle
from tests.test_disclosure_originals import FixtureHTTP
from tests.test_investor_publication import base, fixture, prepared, publish
from tests.test_investor_publication import publication_db as publication_db
from tests.test_original_parsing import html, original, snapshot


async def test_real_document_discrepancy_limits_old_opinion_and_idempotent_ag15_review(
    publication_db,
):
    template, _, _ = await fixture()
    async with session_scope() as session:
        payload, body, request = await original(session)
        await snapshot(session, request.listing_id)
        parsed = await save_candidates(
            session, payload, body, await parse_staged(body, "text/html"), request
        )
        fact_ref = ObjectRef.model_validate(parsed["fact_refs"][0])
        subject = ObjectRef(kind="listing", id=request.listing_id, revision=1)
        service = InvestorService(session)
        case = await service.create_case(
            "local_owner",
            CreateCase(
                listing_ref=subject,
                question="Review saved consolidated profit",
                idempotency_key=uuid4(),
            ),
        )
        mandate = await service.get_mandate("local_owner")
        mandate = await service.revise_mandate(
            "local_owner",
            mandate.model_copy(update={"revision": 2, "scope_refs": (subject,)}),
            Mutation(expected_revision=1, idempotency_key=uuid4()),
        )
    now = datetime.now(UTC)
    claim = template.thesis.claims[0].model_copy(
        update={
            "text": (
                TextNode(type="text", text="Saved consolidated annual profit "),
                NumericNode(
                    type="numeric_claim",
                    ref=fact_ref.model_copy(update={"field_path": "value"}),
                    role="historical_fact",
                    display_format="decimal",
                ),
            ),
            "support_refs": (fact_ref,),
        }
    )
    thesis = template.thesis.model_copy(
        update={
            "case_ref": ObjectRef(kind="case", id=case.id, revision=case.revision),
            "claims": (claim,),
            "as_of": now,
        }
    )
    recommendation = template.recommendation.model_copy(
        update={
            "case_ids": (case.id,),
            "claims": (claim,),
            "as_of": now,
            "mandate_revision_id": ObjectRef(
                kind="mandate", id=mandate.id, revision=mandate.revision
            ),
            "dependency_refs": (
                Dependency(ref=fact_ref, materiality="critical", fields=("value",)),
            ),
            "provenance": (fact_ref,),
        }
    )
    candidate = PublicationCandidate(thesis=thesis, recommendation=recommendation)
    prepared_action = await prepared(candidate)
    published = await publish(candidate, prepared_action)
    assert published.published, published.check
    historical = datetime.now(UTC)
    async with session_scope() as session:
        assert not (
            await PublicationService(session).get_recommendation(
                published.recommendation_ref, at=historical
            )
        ).limitation_reasons
        http = FixtureHTTP()
        http.body, http.mime = html(value="20,000.00"), "text/html"
        newer = await DisclosureService.for_tests(session, http).execute(
            uuid4(),
            FetchRequest(
                listing_id=request.listing_id,
                item_id=UUID(payload["item_id"]),
                as_of=datetime.now(UTC),
            ),
        )
        # No independent verified crosscheck agrees: this remains candidate and
        # an explicit discrepancy. It cannot silently overwrite the old fact.
        req = request.model_copy(
            update={"original_id": UUID(newer["original"]["id"]), "as_of": datetime.now(UTC)}
        )
        updated = await save_candidates(
            session, newer["original"], http.body, await parse_staged(http.body, "text/html"), req
        )
        assert updated["facts"][0]["verification"] == "candidate"
        view = await PublicationService(session).get_recommendation(published.recommendation_ref)
        assert view.status == "review_required" and any(
            "document" in reason.lower() for reason in view.limitation_reasons
        )
        old_view = await PublicationService(session).get_recommendation(
            published.recommendation_ref, at=historical
        )
        assert not old_view.limitation_reasons
        context = ExecutionContext(prepared_action[0], session_scope, base())
        refs = tuple(ObjectRef.model_validate(ref) for ref in updated["fact_refs"])
        await queue_document_review(session, context, request.listing_id, refs)
        await queue_document_review(session, context, request.listing_id, refs)
        assert await session.scalar(select(func.count()).select_from(ReviewCycle)) == 1
        cycle = await session.scalar(select(ReviewCycle))
        assert len(cycle.payload["events"]) == 1 and cycle.payload["reasons"]
    attempted = await prepared(candidate)
    rejected = await publish(candidate, attempted)
    assert not rejected.published

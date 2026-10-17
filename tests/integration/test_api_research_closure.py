"""AF-P3-10: authenticated read-only HTTP for P3 binding/coverage closures.

Every scenario builds its data through the real publication chain (admission
binding, real worker evaluations, prepare, semantic review, publish) inside
the API client's own SQLite database, then exercises the P3 GET surface over
real HTTP. Reads stay read-only with owner 404 semantics, the static
``current`` route must survive the dynamic UUID route, cursors bind to the
immutable manifest identity, and the generic run lists must never expose P3
manifests.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from sqlalchemy import select

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.publication import RuleReviewItem
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from tests.integration.test_coverage_prepare import _drain_workers
from tests.integration.test_publication_closure import (
    _coverages,
    _open_application_refs,
    _proposal,
    _publish,
    _publish_action,
    _reviewed,
)
from tests.support.research_closure import (
    activate_rule,
    admit_research,
    business_input,
    claim_task,
    commit_progress,
    metric_rule,
    requirement_rule,
    run_prepare,
    seed_company_view,
    seed_listing,
    set_runtime,
)

API_KEY = {"X-API-Key": "secret"}


async def _published_chain(ticker: str) -> dict:
    """Publish one real recommendation that carries a rule-coverage dependency."""

    await set_runtime(True)
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker=ticker)
        await activate_rule(session, requirement_rule(listing_ref))
        await activate_rule(session, metric_rule(listing_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
        chain = {"listing_id": int(listing_ref.id), "task_id": UUID(str(task.id))}
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    requirement_ref, metric_ref = await _open_application_refs()
    proposals, frozen = _proposal(requirement_ref)
    result = await run_prepare(claim, business_input(
        case, evidence_ref, question_scope="After evaluation",
        closure_proposals=proposals))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref,
                       disposition="resolved_with_closure",
                       proposal_hash=frozen.proposal_hash),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ))
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is True, published.check.field_errors
    payload = (await _coverages("published"))[0]
    chain.update({
        "published_id": UUID(payload["id"]),
        "prepared_id": UUID(payload["prepared_ref"]["id"]),
        "binding_id": UUID(payload["binding_ref"]["id"]),
        "manifest_hash": payload["manifest_hash"],
        "closure_ref": payload["closure_application_refs"][0],
    })
    return chain


async def _seed_listing_only(ticker: str) -> int:
    async with session_scope() as session:
        listing_ref = await seed_listing(session, ticker=ticker)
        await session.flush()
        return int(listing_ref.id)


def test_p3_reads_require_auth(api_client) -> None:
    for path in (
        "/supplemental-rule-coverages/current?listing=1",
        f"/supplemental-rule-coverages/{uuid4()}",
        f"/research-rule-bindings/{uuid4()}",
    ):
        response = api_client.get(path)
        assert response.status_code == 401, (path, response.status_code)


def test_current_route_survives_dynamic_uuid_route(api_client) -> None:
    """Real HTTP proof: the static route answers before the {id} route."""

    listing_id = asyncio.run(_seed_listing_only("600721"))
    current = api_client.get(
        "/supplemental-rule-coverages/current",
        params={"listing": listing_id},
        headers=API_KEY,
    )
    # A 200 with the honest unknown marker (not the dynamic route's 422 for a
    # non-UUID path segment) proves registration order.
    assert current.status_code == 200, current.text
    body = current.json()
    assert body["state"] == "unknown"
    assert body["reasons"] == ["rule_binding_missing"]
    assert body["manifest"] is None and body["binding"] is None
    assert body["current_limits"] is None
    assert body["listing_ref"] == {
        "kind": "listing", "id": listing_id, "revision": 1, "field_path": None}

    unknown_listing = api_client.get(
        "/supplemental-rule-coverages/current",
        params={"listing": 987654321},
        headers=API_KEY,
    )
    assert unknown_listing.status_code == 404
    assert unknown_listing.json()["detail"]["code"] == "not_found"


def test_exact_coverage_fields_closure_records_and_limits(api_client) -> None:
    chain = asyncio.run(_published_chain("600731"))
    current = api_client.get(
        "/supplemental-rule-coverages/current",
        params={"listing": chain["listing_id"]},
        headers=API_KEY,
    )
    assert current.status_code == 200, current.text
    view = current.json()
    assert view["enabled"] is True
    assert view["state"] == "bound"
    assert view["reasons"] == []
    assert view["manifest"]["id"] == str(chain["published_id"])
    assert view["manifest"]["stage"] == "published"
    assert view["binding"]["id"] == str(chain["binding_id"])
    assert view["current_limits"] == {"reasons": []}

    exact = api_client.get(
        f"/supplemental-rule-coverages/{chain['published_id']}", headers=API_KEY)
    assert exact.status_code == 200, exact.text
    coverage = exact.json()
    assert coverage["stage"] == "published"
    assert coverage["coverage_ref"]["kind"] == "rule_coverage"
    assert coverage["task_ref"]["id"] == str(chain["task_id"])
    assert coverage["binding_ref"]["id"] == str(chain["binding_id"])
    assert coverage["binding"]["task_ref"]["id"] == str(chain["task_id"])
    assert coverage["manifest_hash"] == chain["manifest_hash"]
    assert coverage["entry_total"] == 2
    assert {entry["status"] for entry in coverage["entries"]} == {"application"}
    assert coverage["prepared_ref"]["id"] == str(chain["prepared_id"])
    assert coverage["semantic_review_ref"]["kind"] == "review"
    assert coverage["thesis_ref"]["kind"] == "thesis"
    assert coverage["recommendation_ref"]["kind"] == "recommendation"
    assert coverage["committed_at"] is not None
    assert coverage["closure_application_refs"] == [chain["closure_ref"]]
    assert len(coverage["closure_applications"]) == 1
    closure = coverage["closure_applications"][0]
    assert closure["id"] == chain["closure_ref"]["id"]
    assert closure["authorization"] == "formal_review"
    assert closure["semantic_status"] == "reviewed"
    assert closure["open_issue_refs"] == []
    assert coverage["current_limits"] == {"reasons": []}

    prepared = api_client.get(
        f"/supplemental-rule-coverages/{chain['prepared_id']}", headers=API_KEY)
    assert prepared.status_code == 200, prepared.text
    prepared_view = prepared.json()
    assert prepared_view["stage"] == "prepared"
    # Prepared manifests never compute limits and never carry publish fields.
    assert prepared_view["current_limits"] is None
    assert prepared_view["committed_at"] is None
    assert prepared_view["prepared_ref"] is None
    assert prepared_view["manifest_hash"] != chain["manifest_hash"]


def test_exact_coverage_owner_and_revision_404_semantics(api_client) -> None:
    asyncio.run(_published_chain("600741"))
    missing = api_client.get(f"/supplemental-rule-coverages/{uuid4()}", headers=API_KEY)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"
    revision = api_client.get(
        f"/supplemental-rule-coverages/{uuid4()}",
        params={"revision": 2},
        headers=API_KEY,
    )
    assert revision.status_code == 404


def test_coverage_entry_pagination_binds_identity_and_ordinal(api_client) -> None:
    chain = asyncio.run(_published_chain("600751"))
    base = f"/supplemental-rule-coverages/{chain['published_id']}"

    first = api_client.get(base, params={"limit": 1}, headers=API_KEY)
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["entries"]) == 1
    assert page["entry_total"] == 2
    assert page["next_cursor"] is not None

    second = api_client.get(base, params={"limit": 1, "cursor": page["next_cursor"]},
                            headers=API_KEY)
    assert second.status_code == 200, second.text
    final = second.json()
    assert len(final["entries"]) == 1
    assert final["entries"][0]["rule_ref"] != page["entries"][0]["rule_ref"]
    assert final["next_cursor"] is None
    assert final["manifest_hash"] == chain["manifest_hash"]

    # A cursor minted for the published manifest cannot page the prepared one.
    cross = api_client.get(
        f"/supplemental-rule-coverages/{chain['prepared_id']}",
        params={"limit": 1, "cursor": page["next_cursor"]},
        headers=API_KEY,
    )
    assert cross.status_code == 422
    assert cross.json()["detail"]["code"] == "invalid_cursor"

    garbage = api_client.get(base, params={"limit": 1, "cursor": "not-a-cursor"},
                             headers=API_KEY)
    assert garbage.status_code == 422
    assert garbage.json()["detail"]["code"] == "invalid_cursor"


def test_task_binding_read_and_legacy_marker(api_client) -> None:
    chain = asyncio.run(_published_chain("600761"))
    bound = api_client.get(f"/research-rule-bindings/{chain['task_id']}", headers=API_KEY)
    assert bound.status_code == 200, bound.text
    view = bound.json()
    assert view["state"] == "bound"
    assert view["reasons"] == []
    assert view["task_ref"]["id"] == str(chain["task_id"])
    assert view["binding"]["id"] == str(chain["binding_id"])
    assert view["binding"]["listing_ref"]["id"] == chain["listing_id"]
    assert len(view["binding"]["rule_set"]) == 2
    assert view["binding"]["rule_set_hash"]
    assert view["binding"]["rules_generation"] >= 0
    assert view["binding"]["events_generation"] >= 0

    legacy = api_client.get(f"/research-rule-bindings/{uuid4()}", headers=API_KEY)
    assert legacy.status_code == 200, legacy.text
    legacy_view = legacy.json()
    # A missing binding is the explicit legacy state, never an empty rule set.
    assert legacy_view["state"] == "legacy_unbound"
    assert legacy_view["reasons"] == ["rule_binding_missing"]
    assert legacy_view["binding"] is None


def test_switch_off_keeps_reads_and_hides_nothing(api_client) -> None:
    chain = asyncio.run(_published_chain("600771"))
    asyncio.run(set_runtime(False))
    current = api_client.get(
        "/supplemental-rule-coverages/current",
        params={"listing": chain["listing_id"]},
        headers=API_KEY,
    )
    assert current.status_code == 200, current.text
    view = current.json()
    assert view["enabled"] is False
    assert view["state"] == "bound"
    assert view["manifest"]["id"] == str(chain["published_id"])
    assert view["current_limits"] is not None

    exact = api_client.get(
        f"/supplemental-rule-coverages/{chain['published_id']}", headers=API_KEY)
    assert exact.status_code == 200
    assert exact.json()["manifest_hash"] == chain["manifest_hash"]

    binding = api_client.get(f"/research-rule-bindings/{chain['task_id']}", headers=API_KEY)
    assert binding.status_code == 200
    assert binding.json()["state"] == "bound"


def test_generic_runs_do_not_leak_p3_manifests(api_client) -> None:
    chain = asyncio.run(_published_chain("600781"))

    async def run_ids() -> list[int]:
        from investment_agent.storage.models.analysis import PipelineRun

        async with session_scope() as session:
            return list((await session.scalars(select(PipelineRun.id))).all())

    ids = asyncio.run(run_ids())
    assert ids
    listing_row = asyncio.run(_listing_row(chain["listing_id"]))
    assert listing_row is not None

    listing = api_client.get("/research/runs", params={"kind": "all"}, headers=API_KEY)
    assert listing.status_code == 200
    for secret in (chain["manifest_hash"], str(chain["published_id"]),
                   str(chain["binding_id"])):
        assert secret not in listing.text
    for run_id in ids:
        detail = api_client.get(f"/research/runs/{run_id}", headers=API_KEY)
        if detail.status_code == 200:
            assert chain["manifest_hash"] not in detail.text
            assert str(chain["binding_id"]) not in detail.text

    # The assessment run view (kind- and receipt-checked) rejects evaluation
    # runs, so coverage manifests have no path through it either.
    for run_id in ids:
        assessment = api_client.get(f"/investment-assessment-runs/{run_id}", headers=API_KEY)
        assert assessment.status_code == 404, (run_id, assessment.status_code)


async def _listing_row(listing_id: int) -> Listing | None:
    async with session_scope() as session:
        return await session.get(Listing, listing_id)

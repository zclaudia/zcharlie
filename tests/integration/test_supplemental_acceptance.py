"""Real HTTP, SQLite, queue and worker acceptance for independent P2 rules."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from investment_agent.config import Settings
from investment_agent.investor.methods import fingerprint
from investment_agent.jobs.base import ClaimedJob
from investment_agent.research.worker import ResearchWorker
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.investor import InvestorMutation
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from investment_agent.supplemental_rules.admission import SUPPLEMENTAL_RULE_EVALUATE
from investment_agent.supplemental_rules.canonical import mutation_request_hash
from investment_agent.supplemental_rules.contracts import EvaluationRequest, FrozenManifest
from investment_agent.supplemental_rules.jobs import SupplementalRuleHandler
from investment_agent.supplemental_rules.service import manifest_digest

API_KEY = {"X-API-Key": "secret"}


async def _seed_listing() -> int:
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="P2 acceptance issuer")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600001")
        session.add(listing)
        await session.flush()
        return listing.id


def _rule_content(listing_id: int, *, contexts: list[str] | None = None) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "name": "Acceptance ROE rule",
        "rationale": "A low saved ROE requires review.",
        "source_refs": [],
        "selector": {
            "kind": "listing",
            "listing_ref": {"kind": "listing", "id": listing_id, "revision": 1},
        },
        "contexts": contexts or ["company_research"],
        "rule_type": "metric_condition",
        "definition": {
            "kind": "metric_condition",
            "condition": {
                "kind": "all",
                "conditions": [
                    {
                        "kind": "factor",
                        "factor_id": "roe",
                        "operator": "<",
                        "value": "0.05",
                        "unit": "fraction",
                    }
                ],
            },
        },
        "dimensions": ["financial_cash"],
        "effects": ["require_review"],
        "severity": "warning",
        "materiality": "Acceptance fixture only.",
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "review_due_at": (now + timedelta(days=30)).isoformat(),
        "release_requirements": ["Review exact saved factor evidence."],
    }


class _InlineContext:
    worker_id = "p2-acceptance"

    async def run_with_heartbeat(self, lease: RunLease, work):
        return await work()


def _drain_run(run_id: int) -> str:
    async def run() -> str:
        async with session_scope() as session:
            row = await PipelineRunRepository(session).claim_next(
                worker_id="p2-acceptance",
                kind=SUPPLEMENTAL_RULE_EVALUATE,
                run_id=run_id,
            )
            assert row is not None
            job = ClaimedJob(
                run_id=row.id,
                kind=row.kind,
                lease=RunLease(row.id, "p2-acceptance", row.attempt_count),
                request_json=dict(row.request_json or {}),
            )
        return await SupplementalRuleHandler(settings=Settings(_env_file=None)).execute(
            job, _InlineContext()
        )

    return asyncio.run(run())


def _run_admitted_job_with_worker(run_id: int) -> int | None:
    worker = ResearchWorker(
        settings=Settings(_env_file=None),
        worker_id="p2-acceptance-worker",
        kinds=[SUPPLEMENTAL_RULE_EVALUATE],
    )
    return asyncio.run(worker.run_once(run_id=run_id))


def _set_writes_enabled(enabled: bool) -> None:
    async def set_value() -> None:
        async with session_scope() as session:
            await store_for(session, Settings(_env_file=None)).apply(
                {"supplemental_rules_enabled": enabled}, actor="acceptance-test"
            )

    asyncio.run(set_value())


def _approve_rule(
    api_client,
    listing_id: int,
    suffix: str,
    *,
    rule_id: UUID | None = None,
    contexts: list[str] | None = None,
) -> dict[str, object]:
    content = _rule_content(listing_id, contexts=contexts)
    content["name"] = f"Acceptance rule {suffix}"
    created = api_client.post(
        "/supplemental-rules",
        headers=API_KEY,
        json={
            "idempotency_key": str(rule_id or uuid4()),
            "expected_head_revision": 0,
            "definition": content,
        },
    )
    assert created.status_code == 201, created.text
    rule_ref = created.json()["rule_ref"]
    detail = api_client.get(f"/supplemental-rules/{rule_ref['id']}", headers=API_KEY)
    assert detail.status_code == 200, detail.text
    draft = detail.json()["draft"]
    validation = api_client.post(
        f"/supplemental-rules/{rule_ref['id']}/validations",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": detail.json()["head_revision"],
            "rule_ref": rule_ref,
            "content_hash": draft["content_hash"],
        },
    )
    assert validation.status_code == 200, validation.text
    decision = api_client.post(
        f"/supplemental-rules/{rule_ref['id']}/decisions",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": validation.json()["head_revision"],
            "rule_ref": rule_ref,
            "content_hash": draft["content_hash"],
            "action": "approve",
            "validation_hash": validation.json()["validation_hash"],
        },
    )
    assert decision.status_code == 200, decision.text
    return rule_ref


def _admit(
    api_client,
    listing_id: int,
    *,
    exact_rule_refs: list[dict[str, object]] | None = None,
    contexts: list[str] | None = None,
) -> tuple[dict[str, object], int]:
    request: dict[str, object] = {
        "idempotency_key": str(uuid4()),
        "listing_refs": [{"kind": "listing", "id": listing_id, "revision": 1}],
        "contexts": contexts or ["company_research"],
        "mode": "live",
    }
    if exact_rule_refs is not None:
        request["exact_rule_refs"] = exact_rule_refs
    response = api_client.post(
        "/supplemental-rule-evaluations",
        headers=API_KEY,
        json=request,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    return body, int(body["admission"]["run_ref"]["id"])


def _run_worker_paused_after_first_checkpoint(
    run_id: int, monkeypatch, *, worker_id: str, after_checkpoint
) -> int | None:
    async def run() -> int | None:
        from investment_agent.supplemental_rules import jobs as job_module

        first_saved = asyncio.Event()
        continue_worker = asyncio.Event()
        original_scope = job_module.session_scope
        pause_used = False

        @asynccontextmanager
        async def controlled_scope():
            nonlocal pause_used
            async with original_scope() as session:
                yield session
            if not pause_used:
                async with original_scope() as probe:
                    saved = await probe.get(PipelineRun, run_id)
                    checkpoint = (
                        saved.metadata_json.get("supplemental_checkpoint")
                        if saved is not None and isinstance(saved.metadata_json, dict)
                        else None
                    )
                states = (
                    [member.get("state") for member in checkpoint.get("members", [])]
                    if isinstance(checkpoint, dict)
                    else []
                )
                if len(states) >= 2 and states[0] == "saved" and states[1] == "pending":
                    pause_used = True
                    first_saved.set()
                    await continue_worker.wait()

        monkeypatch.setattr(job_module, "session_scope", controlled_scope)
        worker = ResearchWorker(
            settings=Settings(_env_file=None),
            worker_id=worker_id,
            kinds=[SUPPLEMENTAL_RULE_EVALUATE],
            heartbeat_interval_seconds=120,
        )
        task = asyncio.create_task(worker.run_once(run_id=run_id))
        try:
            await asyncio.wait_for(first_saved.wait(), timeout=15)
            await after_checkpoint()
        finally:
            continue_worker.set()
        result = await task
        monkeypatch.setattr(job_module, "session_scope", original_scope)
        return result

    return asyncio.run(run())


def test_http_lifecycle_queue_worker_and_exact_current_views(api_client, monkeypatch) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())

    created = api_client.post(
        "/supplemental-rules",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": 0,
            "definition": _rule_content(listing_id),
        },
    )
    assert created.status_code == 201, created.text
    rule_ref = created.json()["rule_ref"]
    rule_id = rule_ref["id"]
    history = api_client.get(f"/supplemental-rules/{rule_id}/history", headers=API_KEY)
    assert history.status_code == 200, history.text
    assert history.json()["events"][0]["created_at"].endswith("Z")
    detail = api_client.get(f"/supplemental-rules/{rule_id}", headers=API_KEY)
    assert detail.status_code == 200, detail.text
    draft = detail.json()["draft"]
    assert draft["revision"] == 1
    assert draft["content_hash"]

    validated = api_client.post(
        f"/supplemental-rules/{rule_id}/validations",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": detail.json()["head_revision"],
            "rule_ref": rule_ref,
            "content_hash": draft["content_hash"],
        },
    )
    assert validated.status_code == 200, validated.text
    assert validated.json()["outcome"] == "pass"
    approved = api_client.post(
        f"/supplemental-rules/{rule_id}/decisions",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": validated.json()["head_revision"],
            "rule_ref": rule_ref,
            "content_hash": draft["content_hash"],
            "action": "approve",
            "validation_hash": validated.json()["validation_hash"],
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "active"

    request = {
        "idempotency_key": str(uuid4()),
        "listing_refs": [{"kind": "listing", "id": listing_id, "revision": 1}],
        "contexts": ["company_research"],
        "mode": "live",
    }
    admitted = api_client.post("/supplemental-rule-evaluations", headers=API_KEY, json=request)
    assert admitted.status_code == 202, admitted.text
    body = admitted.json()
    run_id = int(body["admission"]["run_ref"]["id"])
    assert body["admission"]["manifest_hash"]

    async def saved_run_payload():
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            mutation = await session.get(
                InvestorMutation, ("local_owner", request["idempotency_key"])
            )
            return run, mutation

    run, mutation = asyncio.run(saved_run_payload())
    assert run is not None and mutation is not None
    queued = run.request_json
    assert queued["source_manifest_hash"] == body["admission"]["manifest_hash"]
    parsed = EvaluationRequest.model_validate(queued["command_body"])
    assert mutation.request_hash == mutation_request_hash(
        "supplemental_rule.evaluate.v1", None, parsed
    )
    assert queued["request_hash"] == mutation.request_hash
    frozen_pair = queued["source_manifest"]["pairs"][0]
    assert frozen_pair["subject"]["company_id"] > 0
    assert frozen_pair["subject"]["ticker"] == "600001.SH"

    assert _run_admitted_job_with_worker(run_id) == run_id
    worker_settled = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert worker_settled.status_code == 200, worker_settled.text
    assert worker_settled.json()["status"] == "success"
    run_view = api_client.get(f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY)
    assert run_view.status_code == 200, run_view.text
    assert run_view.json()["status"] == "success"
    app_ref = run_view.json()["members"][0]["application_ref"]
    exact = api_client.get(
        f"/supplemental-rule-applications/{app_ref['id']}?revision={app_ref['revision']}",
        headers=API_KEY,
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["payload"]["source_manifest_hash"] == body["admission"]["manifest_hash"]
    current = api_client.get(
        "/supplemental-rule-applications/current",
        params={"listing_id": listing_id, "context": "company_research"},
        headers=API_KEY,
    )
    assert current.status_code == 200, current.text
    assert current.json()["applications"][0]["payload"]["id"] == app_ref["id"]

    replay_request = {
        "idempotency_key": str(uuid4()),
        "listing_refs": request["listing_refs"],
        "contexts": request["contexts"],
        "mode": "historical_replay",
        "evaluation_purpose": "coverage",
        "replay_run_ref": {"kind": "pipeline_run", "id": run_id, "revision": 1},
        "replay_manifest_hash": body["admission"]["manifest_hash"],
        "input_as_of": queued["source_manifest"]["as_of"],
    }
    historical = api_client.post(
        "/supplemental-rule-evaluations",
        headers=API_KEY,
        json=replay_request,
    )
    assert historical.status_code == 202, historical.text
    historical_run_id = int(historical.json()["admission"]["run_ref"]["id"])
    assert _drain_run(historical_run_id) == "completed"
    historical_view = api_client.get(
        f"/supplemental-rule-evaluations/runs/{historical_run_id}",
        headers=API_KEY,
    )
    assert historical_view.status_code == 200, historical_view.text
    historical_ref = historical_view.json()["members"][0]["application_ref"]
    historical_exact = api_client.get(
        f"/supplemental-rule-applications/{historical_ref['id']}?revision=1",
        headers=API_KEY,
    )
    assert historical_exact.status_code == 200, historical_exact.text
    assert historical_exact.json()["payload"]["application_mode"] == "historical_replay"
    still_current = api_client.get(
        "/supplemental-rule-applications/current",
        params={"listing_id": listing_id, "context": "company_research"},
        headers=API_KEY,
    )
    assert still_current.status_code == 200, still_current.text
    assert still_current.json()["applications"][0]["payload"]["id"] == app_ref["id"]

    _set_writes_enabled(False)
    replay = api_client.post("/supplemental-rule-evaluations", headers=API_KEY, json=request)
    assert replay.status_code == 202, replay.text
    assert replay.json()["receipt"] == body["receipt"]
    changed_target = {
        **request,
        "listing_refs": [{"kind": "listing", "id": listing_id + 1, "revision": 1}],
    }
    conflict = api_client.post(
        "/supplemental-rule-evaluations", headers=API_KEY, json=changed_target
    )
    assert conflict.status_code == 409, conflict.text

    async def tamper_payload(change):
        async with session_scope() as session:
            saved = await session.get(PipelineRun, run_id)
            assert saved is not None
            payload = deepcopy(saved.request_json)
            change(payload)
            saved.request_json = payload

    def change_mode(payload):
        payload["execution_envelope"]["mode"] = "historical_replay"

    asyncio.run(tamper_payload(change_mode))
    tampered_mode = api_client.get(f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY)
    assert tampered_mode.status_code == 404, tampered_mode.text

    async def restore_and_tamper_manifest():
        async with session_scope() as session:
            saved = await session.get(PipelineRun, run_id)
            assert saved is not None
            payload = deepcopy(queued)
            manifest = FrozenManifest.model_validate(payload["source_manifest"])
            changed = manifest.model_copy(
                update={"scan_diagnostics": ("tampered-after-admission",)}
            )
            new_hash = manifest_digest(changed)
            changed = changed.model_copy(update={"input_hash": new_hash, "manifest_hash": new_hash})
            payload["source_manifest"] = changed.model_dump(mode="json")
            payload["source_manifest_hash"] = new_hash
            saved.request_json = payload

    asyncio.run(restore_and_tamper_manifest())
    tampered_manifest = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert tampered_manifest.status_code == 404, tampered_manifest.text


def test_user_report_revision_is_unverified_and_zero_target_run_is_real(
    api_client, monkeypatch
) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    event = {
        "event_key": f"report:{uuid4()}",
        "title": "User supplied report",
        "event_type": "policy",
        "jurisdiction": "CN",
        "affected_products": [],
        "affected_subjects": [],
        "policy_stage": "unknown",
        "source_refs": [],
    }
    created = api_client.post(
        "/research-events",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_event_revision": 0,
            "event": event,
        },
    )
    assert created.status_code == 201, created.text
    first_ref = created.json()["event_ref"]
    first = api_client.get(
        f"/research-events/{first_ref['id']}?revision=1",
        headers=API_KEY,
    )
    assert first.status_code == 200, first.text
    assert first.json()["verification"] == "user_report"
    assert first.json()["acquired_at"] == first.json()["available_from"]
    assert first.json()["conflict"] is False

    changed = {**event, "title": "User supplied correction"}
    revised = api_client.post(
        f"/research-events/{first_ref['id']}/revisions",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_event_revision": 1,
            "previous_ref": first_ref,
            "event": changed,
        },
    )
    assert revised.status_code == 201, revised.text
    second_ref = revised.json()["event_ref"]
    assert second_ref["revision"] == 2
    first_again = api_client.get(
        f"/research-events/{first_ref['id']}?revision=1",
        headers=API_KEY,
    )
    second = api_client.get(
        f"/research-events/{first_ref['id']}?revision=2",
        headers=API_KEY,
    )
    assert first_again.json()["title"] == "User supplied report"
    assert second.json()["title"] == "User supplied correction"
    assert second.json()["verification"] == "user_report"

    run_id = int(second_ref.get("run_ref", {}).get("id", revised.json()["run_ref"]["id"]))
    before = api_client.get(f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY)
    assert before.status_code == 200, before.text
    assert before.json()["declared_count"] == 0
    assert _drain_run(run_id) == "completed"
    after = api_client.get(f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY)
    assert after.status_code == 200, after.text
    assert after.json()["completed_count"] == 0


def test_http_event_cursor_tracks_registry_and_exact_event_revision_refs(
    api_client, monkeypatch
) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")

    def post_event(title: str) -> dict[str, object]:
        response = api_client.post(
            "/research-events",
            headers=API_KEY,
            json={
                "idempotency_key": str(uuid4()),
                "expected_event_revision": 0,
                "event": {
                    "event_key": f"report:{uuid4()}",
                    "title": title,
                    "event_type": "policy",
                    "jurisdiction": "CN",
                    "affected_products": [],
                    "affected_subjects": [],
                    "policy_stage": "unknown",
                    "source_refs": [],
                },
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    first = post_event("Cursor event one")
    post_event("Cursor event two")
    page = api_client.get("/research-events", headers=API_KEY, params={"limit": 1})
    assert page.status_code == 200, page.text
    cursor = page.json()["next_cursor"]
    assert cursor

    listing_id = asyncio.run(_seed_listing())
    created = api_client.post(
        "/supplemental-rules",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": 0,
            "definition": _rule_content(listing_id),
        },
    )
    assert created.status_code == 201, created.text
    stale_cursor = api_client.get(
        "/research-events", headers=API_KEY, params={"limit": 1, "cursor": cursor}
    )
    assert stale_cursor.status_code == 409, stale_cursor.text

    rule_ref = created.json()["rule_ref"]
    detail = api_client.get(f"/supplemental-rules/{rule_ref['id']}", headers=API_KEY)
    draft = detail.json()["draft"]
    validation = api_client.post(
        f"/supplemental-rules/{rule_ref['id']}/validations",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": detail.json()["head_revision"],
            "rule_ref": rule_ref,
            "content_hash": draft["content_hash"],
        },
    )
    assert validation.status_code == 200, validation.text
    drafts = api_client.get("/supplemental-rules", headers=API_KEY, params={"state": "draft"})
    assert drafts.status_code == 200, drafts.text
    assert any(item["rule_id"] == rule_ref["id"] for item in drafts.json()["items"])
    assert (
        next(item for item in drafts.json()["items"] if item["rule_id"] == rule_ref["id"])[
            "draft_state"
        ]
        == "validated"
    )

    malformed = api_client.post(
        f"/research-events/{first['event_ref']['id']}/revisions",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_event_revision": 1,
            "previous_ref": {
                **first["event_ref"],
                "field_path": "title",
            },
            "event": {
                "event_key": f"report:{uuid4()}",
                "title": "Malformed correction",
                "event_type": "policy",
                "jurisdiction": "CN",
                "affected_products": [],
                "affected_subjects": [],
                "policy_stage": "unknown",
                "source_refs": [],
            },
        },
    )
    assert malformed.status_code == 422, malformed.text
    wrong_revision = api_client.post(
        f"/research-events/{first['event_ref']['id']}/revisions",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_event_revision": 1,
            "previous_ref": {
                **first["event_ref"],
                "revision": 2,
            },
            "event": {
                "event_key": f"report:{uuid4()}",
                "title": "Wrong revision correction",
                "event_type": "policy",
                "jurisdiction": "CN",
                "affected_products": [],
                "affected_subjects": [],
                "policy_stage": "unknown",
                "source_refs": [],
            },
        },
    )
    assert wrong_revision.status_code == 422, wrong_revision.text


def test_p1_shared_mutation_key_collision_is_rejected(api_client, monkeypatch) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    operation_id = uuid4()

    async def seed_p1_receipt() -> None:
        async with session_scope() as session:
            session.add(
                InvestorMutation(
                    owner_id="local_owner",
                    key=str(operation_id),
                    request_hash=fingerprint({"method": {"operation": "p1-test-operation"}}),
                    result={"kind": "P1_method_receipt", "operation_id": str(operation_id)},
                )
            )

    asyncio.run(seed_p1_receipt())
    listing_id = asyncio.run(_seed_listing())
    response = api_client.post(
        "/supplemental-rule-evaluations",
        headers=API_KEY,
        json={
            "idempotency_key": str(operation_id),
            "listing_refs": [{"kind": "listing", "id": listing_id, "revision": 1}],
            "contexts": ["company_research"],
            "mode": "live",
        },
    )
    assert response.status_code == 409, response.text


def test_real_partial_pair_failure_and_checkpointed_crash_resume(api_client, monkeypatch) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    _approve_rule(api_client, listing_id, "one")
    _approve_rule(api_client, listing_id, "two")

    # Two admissions before either worker runs freeze the same predecessor.
    first, first_run = _admit(api_client, listing_id)
    stale, stale_run = _admit(api_client, listing_id)
    assert _drain_run(first_run) == "completed"
    assert _drain_run(stale_run) == "partial"
    stale_view = api_client.get(
        f"/supplemental-rule-evaluations/runs/{stale_run}",
        headers=API_KEY,
    )
    assert stale_view.status_code == 200, stale_view.text
    assert stale_view.json()["completed_count"] == 0
    assert [m["state"] for m in stale_view.json()["members"]] == ["item_failed", "item_failed"]
    assert len(stale_view.json()["failed_items"]) == 2

    # A later batch has valid predecessors. Simulate a process crash at the
    # second member's pre-write authorization, after the first member and its
    # checkpoint have committed in real SQLite transactions.
    _, crash_run = _admit(api_client, listing_id)
    from investment_agent.supplemental_rules import jobs as job_module

    original_authorize = job_module.authorize_supplemental_run
    calls = 0

    async def crash_before_second_member(session, run_id, *, owner_id="local_owner"):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("simulated worker process loss")
        return await original_authorize(session, run_id, owner_id=owner_id)

    monkeypatch.setattr(job_module, "authorize_supplemental_run", crash_before_second_member)
    try:
        try:
            _drain_run(crash_run)
        except RuntimeError as exc:
            assert str(exc) == "simulated worker process loss"
        else:
            raise AssertionError("the injected worker interruption did not occur")
    finally:
        monkeypatch.setattr(job_module, "authorize_supplemental_run", original_authorize)

    async def checkpoint_and_reclaim():
        async with session_scope() as session:
            run = await session.get(PipelineRun, crash_run)
            assert run is not None
            checkpoint = run.metadata_json["supplemental_checkpoint"]
            assert [member["state"] for member in checkpoint["members"]] == ["saved", "pending"]
            saved_ref = checkpoint["members"][0]["application_ref"]
            repo = PipelineRunRepository(session)
            requeued, failed = await repo.recover_stale(
                stale_before=datetime.now(UTC) + timedelta(seconds=5),
                kind=SUPPLEMENTAL_RULE_EVALUATE,
            )
            assert crash_run in requeued and crash_run not in failed
            row = await repo.claim_next(
                worker_id="p2-recovery", kind=SUPPLEMENTAL_RULE_EVALUATE, run_id=crash_run
            )
            assert row is not None
            job = ClaimedJob(
                run_id=row.id,
                kind=row.kind,
                lease=RunLease(row.id, "p2-recovery", row.attempt_count),
                request_json=dict(row.request_json or {}),
            )
        outcome = await SupplementalRuleHandler(settings=Settings(_env_file=None)).execute(
            job, _InlineContext()
        )
        return outcome, saved_ref

    recovered, saved_ref = asyncio.run(checkpoint_and_reclaim())
    assert recovered == "completed"
    final = api_client.get(f"/supplemental-rule-evaluations/runs/{crash_run}", headers=API_KEY)
    assert final.status_code == 200, final.text
    assert [member["state"] for member in final.json()["members"]] == ["saved", "saved"]
    assert final.json()["completed_count"] == 2
    assert final.json()["members"][0]["application_ref"] == saved_ref


def test_real_worker_lost_lease_rolls_back_second_pair_and_takeover_resumes(
    api_client, monkeypatch
) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    _approve_rule(api_client, listing_id, "lease-one")
    _approve_rule(api_client, listing_id, "lease-two")
    _, run_id = _admit(api_client, listing_id)

    async def invalidate_old_lease() -> None:
        async with session_scope() as session:
            requeued, failed = await PipelineRunRepository(session).recover_stale(
                stale_before=datetime.now(UTC) + timedelta(seconds=5),
                kind=SUPPLEMENTAL_RULE_EVALUATE,
            )
            assert run_id in requeued and run_id not in failed

    assert (
        _run_worker_paused_after_first_checkpoint(
            run_id,
            monkeypatch,
            worker_id="p2-lost-lease",
            after_checkpoint=invalidate_old_lease,
        )
        == run_id
    )

    async def inspect_fenced_transaction():
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None and run.status == "queued"
            checkpoint = run.metadata_json["supplemental_checkpoint"]
            assert [member["state"] for member in checkpoint["members"]] == ["saved", "pending"]
            first_ref = checkpoint["members"][0]["application_ref"]
            count = await session.scalar(
                select(func.count(InvestmentAssessment.id)).where(
                    InvestmentAssessment.kind == "rule_application"
                )
            )
            return first_ref, count

    from sqlalchemy import func, select

    first_ref, application_count = asyncio.run(inspect_fenced_transaction())
    assert application_count == 1  # second application's write rolled back with its checkpoint

    takeover = ResearchWorker(
        settings=Settings(_env_file=None),
        worker_id="p2-takeover",
        kinds=[SUPPLEMENTAL_RULE_EVALUATE],
    )
    assert asyncio.run(takeover.run_once(run_id=run_id)) == run_id
    final = api_client.get(f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY)
    assert final.status_code == 200, final.text
    assert final.json()["status"] == "success"
    assert [member["state"] for member in final.json()["members"]] == ["saved", "saved"]
    assert final.json()["members"][0]["application_ref"] == first_ref


def test_midrun_disabled_flag_settles_failure_and_preserves_saved_member(
    api_client, monkeypatch
) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")

    async def initialize_persisted_flag() -> None:
        from investment_agent.settings.loader import ensure_imported

        settings = Settings(_env_file=None)
        async with session_scope() as session:
            await ensure_imported(session, settings)
            await store_for(session, settings).apply(
                {"supplemental_rules_enabled": True}, actor="acceptance-test"
            )

    asyncio.run(initialize_persisted_flag())
    listing_id = asyncio.run(_seed_listing())
    _approve_rule(api_client, listing_id, "flag-one")
    _approve_rule(api_client, listing_id, "flag-two")
    _, run_id = _admit(api_client, listing_id)

    async def disable_after_first_checkpoint() -> None:
        async with session_scope() as session:
            await store_for(session, Settings(_env_file=None)).apply(
                {"supplemental_rules_enabled": False}, actor="acceptance-test"
            )

    try:
        assert (
            _run_worker_paused_after_first_checkpoint(
                run_id,
                monkeypatch,
                worker_id="p2-midrun-disabled",
                after_checkpoint=disable_after_first_checkpoint,
            )
            == run_id
        )
        run_view = api_client.get(f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY)
        assert run_view.status_code == 200, run_view.text
        assert run_view.json()["status"] == "failed"
        assert run_view.json()["failure_code"] == "writes_disabled"
        assert [member["state"] for member in run_view.json()["members"]] == ["saved", "pending"]
        first_ref = run_view.json()["members"][0]["application_ref"]
        exact = api_client.get(
            f"/supplemental-rule-applications/{first_ref['id']}?revision={first_ref['revision']}",
            headers=API_KEY,
        )
        assert exact.status_code == 200, exact.text

        async def count_applications() -> int:
            async with session_scope() as session:
                return int(
                    await session.scalar(
                        select(func.count(InvestmentAssessment.id)).where(
                            InvestmentAssessment.kind == "rule_application"
                        )
                    )
                    or 0
                )

        from sqlalchemy import func, select

        assert asyncio.run(count_applications()) == 1
    finally:
        _set_writes_enabled(True)


def test_recovery_skips_committed_item_failed_checkpoint_member(api_client, monkeypatch) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    first_rule = _approve_rule(
        api_client,
        listing_id,
        "terminal-failure-one",
        rule_id=UUID("00000000-0000-4000-8000-000000000001"),
        contexts=["company_research", "holding_review"],
    )

    # Establish a predecessor for only one context, then admit a two-context
    # run before a competing single-context run advances that series.
    _, baseline_run = _admit(
        api_client, listing_id, exact_rule_refs=[first_rule], contexts=["company_research"]
    )
    assert _drain_run(baseline_run) == "completed"
    _, interrupted_run = _admit(
        api_client,
        listing_id,
        exact_rule_refs=[first_rule],
        contexts=["company_research", "holding_review"],
    )
    _, advancing_run = _admit(
        api_client, listing_id, exact_rule_refs=[first_rule], contexts=["company_research"]
    )
    assert _drain_run(advancing_run) == "completed"

    from investment_agent.supplemental_rules import jobs as job_module

    original_authorize = job_module.authorize_supplemental_run
    original_save_pair = job_module.SupplementalRuleService.save_pair
    authorization_calls = 0
    save_attempts = {0: 0, 1: 0}

    async def interrupt_before_second_pair(session, run_id, *, owner_id="local_owner"):
        nonlocal authorization_calls
        authorization_calls += 1
        if authorization_calls == 3:
            raise RuntimeError("simulated crash after committed item failure")
        return await original_authorize(session, run_id, owner_id=owner_id)

    async def counted_save_pair(self, manifest, pair, **kwargs):
        save_attempts[pair.ordinal] += 1
        return await original_save_pair(self, manifest, pair, **kwargs)

    monkeypatch.setattr(job_module, "authorize_supplemental_run", interrupt_before_second_pair)
    monkeypatch.setattr(job_module.SupplementalRuleService, "save_pair", counted_save_pair)
    try:
        try:
            _drain_run(interrupted_run)
        except RuntimeError as exc:
            assert str(exc) == "simulated crash after committed item failure"
        else:
            raise AssertionError("the injected process interruption did not occur")
    finally:
        monkeypatch.setattr(job_module, "authorize_supplemental_run", original_authorize)

    async def check_terminal_checkpoint():
        async with session_scope() as session:
            run = await session.get(PipelineRun, interrupted_run)
            assert run is not None
            members = run.metadata_json["supplemental_checkpoint"]["members"]
            assert [member["state"] for member in members] == ["item_failed", "pending"]
            requeued, failed = await PipelineRunRepository(session).recover_stale(
                stale_before=datetime.now(UTC) + timedelta(seconds=5),
                kind=SUPPLEMENTAL_RULE_EVALUATE,
            )
            assert interrupted_run in requeued and interrupted_run not in failed

    asyncio.run(check_terminal_checkpoint())
    takeover = ResearchWorker(
        settings=Settings(_env_file=None),
        worker_id="p2-item-failure-recovery",
        kinds=[SUPPLEMENTAL_RULE_EVALUATE],
    )
    assert asyncio.run(takeover.run_once(run_id=interrupted_run)) == interrupted_run
    assert save_attempts == {0: 1, 1: 1}
    final = api_client.get(
        f"/supplemental-rule-evaluations/runs/{interrupted_run}", headers=API_KEY
    )
    assert final.status_code == 200, final.text
    assert final.json()["status"] == "partial"
    assert [member["state"] for member in final.json()["members"]] == [
        "item_failed",
        "saved",
    ], final.json()


def test_saved_checkpoint_cannot_be_downgraded_to_item_failed(api_client, monkeypatch) -> None:
    """A failed checkpoint entry cannot hide an application committed by this run."""
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    _approve_rule(api_client, listing_id, "checkpoint-downgrade")
    _, run_id = _admit(api_client, listing_id)
    assert _drain_run(run_id) == "completed"

    async def downgrade_saved_member() -> None:
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            assert run is not None
            checkpoint = deepcopy(run.metadata_json["supplemental_checkpoint"])
            checkpoint["members"][0] = {
                "ordinal": 0,
                "state": "item_failed",
                "application_ref": None,
                "input_hash": None,
                "error_code": "item_binding_conflict",
            }
            run.metadata_json = {**run.metadata_json, "supplemental_checkpoint": checkpoint}

    asyncio.run(downgrade_saved_member())
    response = api_client.get(
        f"/supplemental-rule-evaluations/runs/{run_id}", headers=API_KEY
    )
    assert response.status_code == 422, response.text

    async def execute_against_damaged_checkpoint() -> None:
        job = ClaimedJob(
            run_id=run_id,
            kind=SUPPLEMENTAL_RULE_EVALUATE,
            lease=RunLease(run_id, "p2-checkpoint-integrity", 1),
            request_json={},
        )
        await SupplementalRuleHandler(settings=Settings(_env_file=None)).execute(
            job, _InlineContext()
        )

    try:
        asyncio.run(execute_against_damaged_checkpoint())
    except ValueError as exc:
        assert "persisted applications" in str(exc)
    else:
        raise AssertionError("worker accepted a checkpoint that hid a committed application")


def test_checkpoint_operation_scan_is_bounded_by_batch_boundaries(api_client, monkeypatch) -> None:
    """A multi-pair worker verifies the saved-app set only at batch boundaries."""
    from sqlalchemy import event

    import investment_agent.storage.database as database_module

    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    _approve_rule(api_client, listing_id, "bounded-scan-one")
    _approve_rule(api_client, listing_id, "bounded-scan-two")
    _, run_id = _admit(api_client, listing_id)
    engine = database_module._engine
    assert engine is not None
    operation_scan_count = 0

    def count_operation_scan(conn, cursor, statement, parameters, context, executemany):
        nonlocal operation_scan_count
        normalized = statement.lower().replace('"', "")
        if "json_extract(investment_assessment.payload, '$.operation_id')" in normalized:
            operation_scan_count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_operation_scan)
    try:
        assert _drain_run(run_id) == "completed"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_operation_scan)
    assert operation_scan_count == 2

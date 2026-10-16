"""Two-connection SQLite CAS checks for append-only P2 heads."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

from sqlalchemy import func, select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investor import InvestorMutation
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.supplemental_rules import (
    ResearchEventRevision,
    SupplementalRuleEvent,
    SupplementalRuleHead,
)
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository

API_KEY = {"X-API-Key": "secret"}


async def _seed_listing() -> int:
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="P2 CAS issuer")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600010")
        session.add(listing)
        await session.flush()
        return listing.id


def _rule_content(listing_id: int, *, suffix: str) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "name": f"CAS rule {suffix}",
        "rationale": "Concurrent update fixture.",
        "source_refs": [],
        "selector": {
            "kind": "listing",
            "listing_ref": {"kind": "listing", "id": listing_id, "revision": 1},
        },
        "contexts": ["company_research"],
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
        "materiality": "CAS fixture.",
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "review_due_at": (now + timedelta(days=30)).isoformat(),
        "release_requirements": ["Review saved source."],
    }


def test_two_independent_http_writers_one_rule_head_cas_winner(api_client, monkeypatch) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    created = api_client.post(
        "/supplemental-rules",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": 0,
            "definition": _rule_content(listing_id, suffix="base"),
        },
    )
    assert created.status_code == 201, created.text
    rule_ref = created.json()["rule_ref"]
    rule_id = rule_ref["id"]

    ready = Barrier(2)

    def revise(label: str):
        ready.wait(timeout=5)
        return api_client.post(
            f"/supplemental-rules/{rule_id}/revisions",
            headers=API_KEY,
            json={
                "idempotency_key": str(uuid4()),
                "expected_head_revision": 1,
                "definition": _rule_content(listing_id, suffix=label),
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(revise, ("writer-a", "writer-b")))
    assert sorted(response.status_code for response in responses) == [201, 409], [
        (response.status_code, response.text) for response in responses
    ]


def test_two_independent_http_writers_one_event_revision_cas_winner(
    api_client, monkeypatch
) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    event = {
        "event_key": f"user-report:{uuid4()}",
        "title": "Reported policy change",
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
    event_ref = created.json()["event_ref"]
    event_id = event_ref["id"]

    async def event_generation():
        async with session_scope() as session:
            return (
                await SupplementalRulesRepository(session).get_registry_head("local_owner")
            ).events_generation

    generation_before_revision = asyncio.run(event_generation())
    ready = Barrier(2)

    operation_ids = [str(uuid4()), str(uuid4())]

    def revise(index_and_label):
        index, label = index_and_label
        ready.wait(timeout=5)
        changed = {**event, "title": f"Reported policy change {label}"}
        return api_client.post(
            f"/research-events/{event_id}/revisions",
            headers=API_KEY,
            json={
                "idempotency_key": operation_ids[index],
                "expected_event_revision": 1,
                "previous_ref": event_ref,
                "event": changed,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(revise, enumerate(("writer-a", "writer-b"))))
    assert sorted(response.status_code for response in responses) == [201, 409], [
        (response.status_code, response.text) for response in responses
    ]

    async def inspect_final_event_state():
        async with session_scope() as session:
            revisions = list(
                (
                    await session.scalars(
                        select(ResearchEventRevision)
                        .where(ResearchEventRevision.id == str(event_id))
                        .order_by(ResearchEventRevision.revision)
                    )
                ).all()
            )
            registry = await SupplementalRulesRepository(session).get_registry_head("local_owner")
            receipt_count = await session.scalar(
                select(func.count(InvestorMutation.key)).where(
                    InvestorMutation.owner_id == "local_owner",
                    InvestorMutation.key.in_(operation_ids),
                )
            )
            runs = list((await session.scalars(select(PipelineRun))).all())
            mutation_runs = [
                run
                for run in runs
                if run.request_json and run.request_json.get("operation_id") in operation_ids
            ]
            return revisions, registry, receipt_count, mutation_runs

    revisions, registry, receipt_count, mutation_runs = asyncio.run(inspect_final_event_state())
    assert len(revisions) == 2
    assert revisions[-1].revision == 2
    assert registry.events_generation == generation_before_revision + 1
    assert receipt_count == 1
    assert len(mutation_runs) == 1


def test_two_independent_http_writers_one_approval_cas_and_all_effects_once(
    api_client, monkeypatch
) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    created = api_client.post(
        "/supplemental-rules",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": 0,
            "definition": _rule_content(listing_id, suffix="approval-race"),
        },
    )
    assert created.status_code == 201, created.text
    rule_ref = created.json()["rule_ref"]
    rule_id = rule_ref["id"]
    detail = api_client.get(f"/supplemental-rules/{rule_id}", headers=API_KEY)
    assert detail.status_code == 200, detail.text
    draft = detail.json()["draft"]
    validation = api_client.post(
        f"/supplemental-rules/{rule_id}/validations",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": detail.json()["head_revision"],
            "rule_ref": rule_ref,
            "content_hash": draft["content_hash"],
        },
    )
    assert validation.status_code == 200, validation.text

    async def before_approval():
        async with session_scope() as session:
            head = await session.get(SupplementalRuleHead, ("local_owner", str(rule_id)))
            assert head is not None
            registry = await SupplementalRulesRepository(session).get_registry_head("local_owner")
            return head.head_revision, registry

    head_revision_before, registry_before = asyncio.run(before_approval())
    operation_ids = [str(uuid4()), str(uuid4())]
    ready = Barrier(2)

    def approve(operation_id: str):
        ready.wait(timeout=5)
        return api_client.post(
            f"/supplemental-rules/{rule_id}/decisions",
            headers=API_KEY,
            json={
                "idempotency_key": operation_id,
                "expected_head_revision": validation.json()["head_revision"],
                "rule_ref": rule_ref,
                "content_hash": draft["content_hash"],
                "action": "approve",
                "validation_hash": validation.json()["validation_hash"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(approve, operation_ids))
    assert sorted(response.status_code for response in responses) == [200, 409], [
        (response.status_code, response.text) for response in responses
    ]

    async def inspect_approval_effects():
        async with session_scope() as session:
            head = await session.get(SupplementalRuleHead, ("local_owner", str(rule_id)))
            assert head is not None
            registry_after = await SupplementalRulesRepository(session).get_registry_head(
                "local_owner"
            )
            audits = list(
                (
                    await session.scalars(
                        select(SupplementalRuleEvent).where(
                            SupplementalRuleEvent.owner_id == "local_owner",
                            SupplementalRuleEvent.rule_id == str(rule_id),
                            SupplementalRuleEvent.command == "rule_approved",
                        )
                    )
                ).all()
            )
            receipts = await session.scalar(
                select(func.count(InvestorMutation.key)).where(
                    InvestorMutation.owner_id == "local_owner",
                    InvestorMutation.key.in_(operation_ids),
                )
            )
            runs = list((await session.scalars(select(PipelineRun))).all())
            mutation_runs = [
                run
                for run in runs
                if run.request_json and run.request_json.get("operation_id") in operation_ids
            ]
            return head, registry_after, audits, receipts, mutation_runs

    head, registry_after, audits, receipts, mutation_runs = asyncio.run(inspect_approval_effects())
    assert head.head_revision == head_revision_before + 1
    assert head.active_revision == rule_ref["revision"] and head.active_state == "active"
    assert registry_after.registry_revision == registry_before.registry_revision + 1
    assert registry_after.rules_generation == registry_before.rules_generation + 1
    assert len(audits) == 1
    assert receipts == 1
    assert len(mutation_runs) == 1

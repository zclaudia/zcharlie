from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from investment_agent.supplemental_rules import validation

API_KEY = {"X-API-Key": "secret"}


async def _seed_listing() -> int:
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.models.reference import Company, Listing, Market

    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="P2 lifecycle review issuer")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600019")
        session.add(listing)
        await session.flush()
        return listing.id


def _rule_content(listing_id: int, *, valid_from: datetime | None = None) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "name": "Lifecycle validation rule",
        "rationale": "Synthetic lifecycle regression.",
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
        "materiality": "Synthetic lifecycle validation.",
        "valid_from": (valid_from or now - timedelta(days=1)).isoformat(),
        "review_due_at": (now + timedelta(days=30)).isoformat(),
        "release_requirements": ["Recheck exact saved inputs."],
    }


def _create_rule(api_client, listing_id: int, content: dict[str, object]):
    created = api_client.post(
        "/supplemental-rules",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": 0,
            "definition": content,
        },
    )
    assert created.status_code == 201, created.text
    rule_ref = created.json()["rule_ref"]
    detail = api_client.get(f"/supplemental-rules/{rule_ref['id']}", headers=API_KEY)
    assert detail.status_code == 200, detail.text
    return rule_ref, detail.json()["draft"]


def _validate(api_client, rule_ref, draft, *, expected_head_revision: int):
    return api_client.post(
        f"/supplemental-rules/{rule_ref['id']}/validations",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": expected_head_revision,
            "rule_ref": rule_ref,
            "content_hash": draft["content_hash"],
        },
    )


def _approve(api_client, rule_ref, draft, *, expected_head_revision: int, validation_hash: str):
    return api_client.post(
        f"/supplemental-rules/{rule_ref['id']}/decisions",
        headers=API_KEY,
        json={
            "idempotency_key": str(uuid4()),
            "expected_head_revision": expected_head_revision,
            "rule_ref": rule_ref,
            "content_hash": draft["content_hash"],
            "action": "approve",
            "validation_hash": validation_hash,
        },
    )


def test_future_valid_from_passes_validation_but_cannot_be_approved(api_client, monkeypatch) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    future = datetime.now(UTC) + timedelta(days=2)
    rule_ref, draft = _create_rule(api_client, listing_id, _rule_content(listing_id, valid_from=future))
    validated = _validate(api_client, rule_ref, draft, expected_head_revision=1)
    assert validated.status_code == 200, validated.text
    assert validated.json()["outcome"] == "pass"

    decision = _approve(
        api_client,
        rule_ref,
        draft,
        expected_head_revision=validated.json()["head_revision"],
        validation_hash=validated.json()["validation_hash"],
    )
    assert decision.status_code == 422, decision.text
    assert decision.json()["detail"]["code"] == "invalid_reference"


def test_later_failed_validation_invalidates_prior_pass(api_client, monkeypatch) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    rule_ref, draft = _create_rule(api_client, listing_id, _rule_content(listing_id))
    first = _validate(api_client, rule_ref, draft, expected_head_revision=1)
    assert first.status_code == 200 and first.json()["outcome"] == "pass"

    monkeypatch.setattr(validation.DEFAULT_REGISTRY, "get", lambda name: (_ for _ in ()).throw(KeyError(name)))
    second = _validate(
        api_client,
        rule_ref,
        draft,
        expected_head_revision=first.json()["head_revision"],
    )
    assert second.status_code == 200 and second.json()["outcome"] == "fail"

    decision = _approve(
        api_client,
        rule_ref,
        draft,
        expected_head_revision=second.json()["head_revision"],
        validation_hash=first.json()["validation_hash"],
    )
    assert decision.status_code == 409, decision.text
    assert decision.json()["detail"]["code"] == "revision_conflict"


def test_dependency_change_after_validation_blocks_http_approval(api_client, monkeypatch) -> None:
    monkeypatch.setenv("SUPPLEMENTAL_RULES_ENABLED", "true")
    listing_id = asyncio.run(_seed_listing())
    rule_ref, draft = _create_rule(api_client, listing_id, _rule_content(listing_id))
    validated = _validate(api_client, rule_ref, draft, expected_head_revision=1)
    assert validated.status_code == 200 and validated.json()["outcome"] == "pass"

    original_get = validation.DEFAULT_REGISTRY.get

    def changed_factor(name: str):
        return replace(original_get(name), version=f"{original_get(name).version}-changed")

    monkeypatch.setattr(validation.DEFAULT_REGISTRY, "get", changed_factor)
    decision = _approve(
        api_client,
        rule_ref,
        draft,
        expected_head_revision=validated.json()["head_revision"],
        validation_hash=validated.json()["validation_hash"],
    )
    assert decision.status_code == 409, decision.text
    assert decision.json()["detail"]["code"] == "revision_conflict"

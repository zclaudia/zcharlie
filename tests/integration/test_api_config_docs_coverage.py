"""industry-groups coverage-check endpoint (config-docs-plan §4.5, WP3): auth,
response shape over the baseline / a posted draft, and the rename guard that
keeps a group id referenced by a stored rule template.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import pytest

from investment_agent.config_docs import loader, registry
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.rules import RuleTemplateRecord

KEY = {"X-API-Key": "secret"}
ENDPOINT = "/config-docs/industry-groups/coverage-check"
KIND = "industry_groups"


@pytest.fixture(autouse=True)
def _isolated_loader_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(loader, "_refresh_broken", False)
    loader.invalidate()
    yield
    loader.invalidate()


def _baseline_document() -> dict[str, Any]:
    return registry.baseline_document(registry.spec_for(KIND))


def test_coverage_check_requires_auth(api_client) -> None:
    assert api_client.post(ENDPOINT).status_code == 401
    assert api_client.post(ENDPOINT, json={"value": {}}).status_code == 401


def test_coverage_check_over_the_effective_baseline(api_client) -> None:
    """Fresh database, no CSI300 members: the directory-level checks still run."""

    response = api_client.post(ENDPOINT, json={}, headers=KEY)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["unmapped_csrc"] == []
    assert body["missing_level1_defaults"] == []
    assert body["invalid_overrides"] == []
    assert body["member_count"] == 0
    assert "覆盖完整" in body["summary"]
    assert body["as_of"]


def test_coverage_check_over_a_posted_draft(api_client) -> None:
    draft = copy.deepcopy(_baseline_document())
    # pick a level-1 with no override beneath it (removing one that has children
    # is a parse error before the coverage check even runs)
    overrides = draft["shenwan"]["overrides"]
    code = next(
        item
        for item in draft["shenwan"]["level1"]
        if not any(override.startswith(item[:2]) for override in overrides)
    )
    del draft["shenwan"]["level1"][code]
    response = api_client.post(ENDPOINT, json={"value": draft}, headers=KEY)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert [gap["code"] for gap in body["missing_level1_defaults"]] == [code]
    assert body["unmapped_csrc"] == [] and body["invalid_overrides"] == []
    assert "缺 1 个" in body["summary"]

    # The draft check writes nothing: the stored document is untouched.
    detail = api_client.get(f"/config-docs/{KIND}", headers=KEY).json()
    assert detail["revision"] == 0 and detail["value"] == _baseline_document()


def test_coverage_check_rejects_an_invalid_draft(api_client) -> None:
    draft = copy.deepcopy(_baseline_document())
    draft["codes"]["J66"] = "ghost"
    response = api_client.post(ENDPOINT, json={"value": draft}, headers=KEY)
    assert response.status_code == 422
    [issue] = response.json()["detail"]["issues"]
    assert issue["key"] == KIND and "unknown group" in issue["messages"][0]


async def test_patch_rejects_removing_a_template_referenced_group(api_client) -> None:
    async with session_scope() as session:
        session.add(
            RuleTemplateRecord(
                id="tmpl_banking",
                version=1,
                name="银行模板",
                definition={"applicable_groups": ["banking"]},
                hash="cov-test",
                calibration_status="calibrated",
            )
        )

    document = copy.deepcopy(_baseline_document())
    del document["groups"]["banking"]
    document["codes"] = {
        code: group for code, group in document["codes"].items() if group != "banking"
    }
    document["legacy_name_rules"] = [
        rule for rule in document.get("legacy_name_rules", []) if rule.get("group") != "banking"
    ]
    document["shenwan"] = {
        "level1": {c: g for c, g in document["shenwan"]["level1"].items() if g != "banking"},
        "overrides": {c: g for c, g in document["shenwan"]["overrides"].items() if g != "banking"},
    }
    response = api_client.patch(
        f"/config-docs/{KIND}", json={"value": document, "note": "移除 banking"}, headers=KEY
    )
    assert response.status_code == 422
    [issue] = response.json()["detail"]["issues"]
    assert issue["key"] == KIND
    assert "tmpl_banking" in issue["messages"][0] and "不能移除" in issue["messages"][0]

    # Nothing was written.
    after = api_client.get(f"/config-docs/{KIND}", headers=KEY).json()
    assert after["revision"] == 0 and "banking" in after["value"]["groups"]


def test_patch_allows_removing_an_unreferenced_group(api_client) -> None:
    added = copy.deepcopy(_baseline_document())
    added["version"] = "cov-add"
    added["groups"]["scratch"] = "临时分组"
    assert (
        api_client.patch(
            f"/config-docs/{KIND}", json={"value": added, "note": "加临时分组"}, headers=KEY
        ).status_code
        == 200
    )

    removed = copy.deepcopy(added)
    removed["version"] = "cov-remove"
    del removed["groups"]["scratch"]
    response = api_client.patch(
        f"/config-docs/{KIND}", json={"value": removed, "note": "删临时分组"}, headers=KEY
    )
    assert response.status_code == 200
    assert "scratch" not in response.json()["value"]["groups"]

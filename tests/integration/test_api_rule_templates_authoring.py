"""/rule-templates authoring (config-docs WP4, CD6/CD7): draft the next version,
activate / rollback, explicit baseline import, and the DB-groups validation.

Sync semantics are asserted in ``tests/unit/test_rules_store.py`` (a file never
overwrites a DB-authored version); here the HTTP surface is exercised:
auth, 422 per-issue validation (``applicable_groups`` against the DB industry
groups), 404 unknown ids, the mandatory note, and actor attribution.
"""

from __future__ import annotations

import pytest

from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import DEFAULT_TEMPLATES_DIR, load_templates
from investment_agent.storage.database import session_scope
from tests.unit._candidate_support import seed_template, template_data

KEY = {"X-API-Key": "secret"}


async def _seed_second_version() -> None:
    from investment_agent.rules.validation import parse_template

    async with session_scope() as session:
        await RuleStore(session).create_version(
            parse_template(template_data(description="v2")), actor="web", note="second version"
        )


def test_writes_require_auth(api_client) -> None:
    for path, body in (
        ("/rule-templates/cand_test/validate", {"document": {}}),
        ("/rule-templates/cand_test/versions", {"document": {}, "note": "x"}),
        ("/rule-templates/cand_test/activate", {"version": 1, "note": "x"}),
        ("/rule-templates/cand_test/baseline-import", {"note": "x"}),
    ):
        assert api_client.post(path, json=body).status_code == 401, path


@pytest.mark.parametrize(
    "path",
    [
        "/rule-templates/nope/validate",
        "/rule-templates/nope/versions",
        "/rule-templates/nope/activate",
        "/rule-templates/nope/baseline-import",
    ],
)
def test_unknown_template_is_404(api_client, path) -> None:
    if "validate" in path:
        body = {"document": {"id": "nope"}}
    elif "versions" in path:
        body = {"document": {"id": "nope"}, "note": "x"}
    elif "activate" in path:
        body = {"version": 1, "note": "x"}
    else:
        body = {"note": "x"}
    assert api_client.post(path, json=body, headers=KEY).status_code == 404, path


async def test_validate_and_create_version_happy_path(api_client) -> None:
    async with session_scope() as session:
        await seed_template(session)
    document = template_data(description="via UI", thesis="更好的说明")

    # The 校验 dry-run passes without writing.
    checked = api_client.post("/rule-templates/cand_test/validate", json={"document": document}, headers=KEY)
    assert checked.status_code == 200 and checked.json() == {"ok": True, "issues": []}
    assert api_client.get("/rule-templates/cand_test", headers=KEY).json()["version_count"] == 1

    created = api_client.post(
        "/rule-templates/cand_test/versions",
        json={"document": document, "note": "补充策略说明"},
        headers=KEY,
    )
    assert created.status_code == 200
    body = created.json()
    assert (body["action"], body["version"]) == ("new_version", 2)
    assert (body["created_by"], body["note"]) == ("api", "补充策略说明")  # X-API-Key -> api
    assert body["detail"]["version"] == body["detail"]["latest_version"] == 2
    assert body["detail"]["definition"]["thesis"] == "更好的说明"
    # The version history carries actor + note; v1 stays untouched.
    versions = body["detail"]["versions"]
    assert [(v["version"], v["created_by"], v["note"]) for v in versions] == [
        (2, "api", "补充策略说明"),
        (1, None, None),
    ]

    # Identical content is a no-op, not a third version.
    again = api_client.post(
        "/rule-templates/cand_test/versions",
        json={"document": document, "note": "再提交一次"},
        headers=KEY,
    )
    assert again.json()["action"] == "unchanged"
    assert api_client.get("/rule-templates/cand_test", headers=KEY).json()["version_count"] == 2


async def test_note_is_required(api_client) -> None:
    async with session_scope() as session:
        await seed_template(session)
    for body in (
        {"document": template_data()},  # missing entirely
        {"document": template_data(), "note": "   "},  # blank
    ):
        response = api_client.post(
            "/rule-templates/cand_test/versions", json=body, headers=KEY
        )
        assert response.status_code == 422
    assert api_client.post(
        "/rule-templates/cand_test/activate", json={"version": 1, "note": ""}, headers=KEY
    ).status_code == 422
    assert api_client.post(
        "/rule-templates/cand_test/baseline-import", json={"note": " "}, headers=KEY
    ).status_code == 422


async def test_invalid_document_is_422_with_per_issue_errors(api_client) -> None:
    async with session_scope() as session:
        await seed_template(session)

    # An applicable_groups entry outside the DB industry-group mapping.
    document = template_data(applicable_groups=["consumer", "fintech"], thesis=None)
    checked = api_client.post(
        "/rule-templates/cand_test/validate", json={"document": document}, headers=KEY
    )
    assert checked.status_code == 200
    payload = checked.json()
    assert payload["ok"] is False
    issue = next(item for item in payload["issues"] if item["code"] == "unknown_industry_group")
    assert issue["path"] == "applicable_groups[1]"

    response = api_client.post(
        "/rule-templates/cand_test/versions",
        json={"document": document, "note": "尝试非法分组"},
        headers=KEY,
    )
    assert response.status_code == 422
    issues = response.json()["detail"]["issues"]
    codes = {item["code"] for item in issues}
    assert "unknown_industry_group" in codes
    # Nothing was written.
    assert api_client.get("/rule-templates/cand_test", headers=KEY).json()["version_count"] == 1

    # A broken document id is a schema issue, not a rename/fork.
    wrong_id = api_client.post(
        "/rule-templates/cand_test/versions",
        json={"document": template_data(id="other_test"), "note": "改名"},
        headers=KEY,
    )
    assert wrong_id.status_code == 422
    assert wrong_id.json()["detail"]["issues"][0]["path"] == "id"

    # Structural garbage is a schema issue per pydantic error location.
    broken = api_client.post(
        "/rule-templates/cand_test/versions",
        json={"document": {"id": "cand_test", "screen": 5}, "note": "坏结构"},
        headers=KEY,
    )
    assert broken.status_code == 422
    assert any(item["path"].startswith("screen") for item in broken.json()["detail"]["issues"])


async def test_validation_uses_the_db_industry_groups(api_client) -> None:
    """WP4: applicable_groups is checked against the DB mapping (WP3), not the
    baseline file - a group added in the industry-groups UI validates, and one
    that exists only in the file is rejected once the DB diverges."""
    async with session_scope() as session:
        await seed_template(session)

    # Before the import the effective mapping is the baseline file: the new
    # group is unknown, a file-only group is fine.
    fresh = template_data(applicable_groups=["consumer", "experimental"])
    checked = api_client.post(
        "/rule-templates/cand_test/validate", json={"document": fresh}, headers=KEY
    )
    assert checked.status_code == 200
    assert any(
        item["code"] == "unknown_industry_group" and "experimental" in item["message"]
        for item in checked.json()["issues"]
    )

    baseline = api_client.get("/config-docs/industry_groups", headers=KEY).json()["value"]
    document = {
        **baseline,
        "version": "ui-draft-1",
        "groups": {**baseline["groups"], "experimental": "试验分组"},
    }
    patched = api_client.patch(
        "/config-docs/industry_groups",
        json={"value": document, "note": "新增试验分组", "expected_revision": 0},
        headers=KEY,
    )
    assert patched.status_code == 200, patched.text

    # The same document now validates: the DB mapping is what counts.
    checked = api_client.post(
        "/rule-templates/cand_test/validate", json={"document": fresh}, headers=KEY
    )
    assert checked.status_code == 200
    assert checked.json() == {"ok": True, "issues": []}


async def test_activate_rolls_back_to_an_older_version(api_client) -> None:
    async with session_scope() as session:
        await seed_template(session)
    await _seed_second_version()
    detail = api_client.get("/rule-templates/cand_test", headers=KEY).json()
    v1_hash = next(v["hash"] for v in detail["versions"] if v["version"] == 1)

    activated = api_client.post(
        "/rule-templates/cand_test/activate",
        json={"version": 1, "note": "回滚到首版"},
        headers=KEY,
    )
    assert activated.status_code == 200
    body = activated.json()
    assert (body["action"], body["version"]) == ("new_version", 3)
    assert body["hash"] == v1_hash  # the old content, re-stored
    assert body["detail"]["definition"]["description"] == ""
    assert api_client.get("/rule-templates/cand_test", headers=KEY).json()["version_count"] == 3

    # Activating the current version writes nothing.
    same = api_client.post(
        "/rule-templates/cand_test/activate",
        json={"version": 3, "note": "已是当前"},
        headers=KEY,
    )
    assert same.json()["action"] == "unchanged"
    # Unknown version -> 404.
    assert api_client.post(
        "/rule-templates/cand_test/activate",
        json={"version": 9, "note": "不存在"},
        headers=KEY,
    ).status_code == 404


async def test_baseline_import_pulls_the_file_in_and_clears_drift(api_client) -> None:
    """The 从基线导入 flow: drift shows (baseline_changed + which version the
    file matches), the explicit import re-stores the file content, and the
    drift disappears. Uses a real config/rule_templates file."""
    templates = load_templates(DEFAULT_TEMPLATES_DIR)
    path, file_template = next((p, t) for p, t in templates if t.id == "quality_growth")
    async with session_scope() as session:
        await RuleStore(session).sync_templates([(path, file_template)])  # first import
        await RuleStore(session).create_version(
            file_template.model_copy(update={"description": "DB 起草"}),
            actor="web",
            note="draft ahead of the file",
        )

    listed = api_client.get("/rule-templates", headers=KEY).json()
    item = next(row for row in listed if row["id"] == "quality_growth")
    assert item["file_status"] == "baseline_changed"
    assert item["baseline_version"] == 1  # the file still holds v1's content
    detail = api_client.get("/rule-templates/quality_growth", headers=KEY).json()
    assert detail["file_status"] == "baseline_changed"

    imported = api_client.post(
        "/rule-templates/quality_growth/baseline-import",
        json={"note": "同步仓库基线"},
        headers=KEY,
    )
    assert imported.status_code == 200
    body = imported.json()
    assert (body["action"], body["version"]) == ("new_version", 3)
    assert body["detail"]["definition"]["description"] != "DB 起草"
    after = api_client.get("/rule-templates/quality_growth", headers=KEY).json()
    assert after["file_status"] == "synced"
    assert after["version_count"] == 3

    # Importing again while the file is current writes nothing.
    once_more = api_client.post(
        "/rule-templates/quality_growth/baseline-import",
        json={"note": "再导入"},
        headers=KEY,
    )
    assert once_more.json()["action"] == "unchanged"
    assert api_client.get("/rule-templates/quality_growth", headers=KEY).json()["version_count"] == 3


async def test_baseline_import_without_a_file_is_422(api_client) -> None:
    async with session_scope() as session:
        await seed_template(session)  # cand_test has no file in the baseline tree
    response = api_client.post(
        "/rule-templates/cand_test/baseline-import", json={"note": "导入"}, headers=KEY
    )
    assert response.status_code == 422
    assert "no baseline file" in response.json()["detail"]


async def test_actor_is_web_for_a_session(api_client) -> None:
    async with session_scope() as session:
        await seed_template(session)
    assert (
        api_client.post("/app/login", data={"api_key": "secret", "next": "/app/templates"}).status_code
        == 200
    )
    created = api_client.post(
        "/rule-templates/cand_test/versions",
        json={"document": template_data(description="web 编辑"), "note": "网页操作"},
        headers={"X-Requested-With": "fetch"},
    )
    assert created.status_code == 200
    assert created.json()["created_by"] == "web"

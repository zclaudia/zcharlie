"""/rule-templates (read-only template page API) and the /app/templates pages."""

from __future__ import annotations

import pytest

from investment_agent.api.routes import rule_templates as routes
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.rules.validation import parse_template
from investment_agent.storage.database import session_scope
from tests.unit._candidate_support import (
    seed_promotable,
    seed_template,
)
from tests.unit._candidate_support import (
    template_data as cand_template_data,
)

KEY = {"X-API-Key": "secret"}


def test_requires_auth(api_client) -> None:
    assert api_client.get("/rule-templates").status_code == 401
    assert api_client.get("/rule-templates/cand_test").status_code == 401


async def test_list_and_detail(api_client) -> None:
    ids = await seed_promotable()
    assert ids["candidate_id"]
    async with session_scope() as session:
        # A DB-authored second version (files only seed; CD6).
        await RuleStore(session).create_version(
            parse_template(cand_template_data(description="changed")),
            actor="web",
            note="update description",
        )

    items = api_client.get("/rule-templates", headers=KEY).json()
    assert [item["id"] for item in items] == ["cand_test"]
    item = items[0]
    assert item["version"] == 2
    assert item["ref"] == "cand_test@2"
    assert item["version_count"] == 2
    assert item["screen_condition_count"] == 2
    assert item["param_count"] == 2
    assert item["open_candidates"] == 1
    assert item["file_status"] == "file_missing"  # not in config/rule_templates

    detail = api_client.get("/rule-templates/cand_test", headers=KEY).json()
    assert detail["version"] == detail["latest_version"] == 2
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert detail["definition"]["description"] == "changed"
    assert "screen" in detail["definition"]
    assert detail["factor_units"]["roe"] == "fraction"
    assert detail["factor_units"]["position_weight"] == "fraction"  # P2 fallback

    old = api_client.get("/rule-templates/cand_test?version=1", headers=KEY).json()
    assert old["version"] == 1
    assert old["latest_version"] == 2
    assert old["definition"]["description"] == ""


async def test_not_found(api_client) -> None:
    assert api_client.get("/rule-templates/nope", headers=KEY).status_code == 404
    async with session_scope() as session:
        await seed_template(session)
    assert api_client.get("/rule-templates/cand_test?version=9", headers=KEY).status_code == 404
    assert api_client.get("/rule-templates/cand_test?version=0", headers=KEY).status_code == 422


async def test_config_templates_are_synced_after_a_sync(api_client) -> None:
    async with session_scope() as session:
        await RuleStore(session).sync_templates(load_templates())
    items = api_client.get("/rule-templates", headers=KEY).json()
    assert len(items) == 5
    assert {item["file_status"] for item in items} == {"synced"}
    detail = api_client.get("/rule-templates/quality_growth", headers=KEY).json()
    definition = detail["definition"]
    assert definition["thesis"] and definition["falsification"]
    assert definition["param_meta"]["roe_min"]["label"] == "ROE 下限"
    assert definition["screen"]["all"][0]["label"] == "竞争优势"


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        # CD6: a file that differs from the current version is drift, not a
        # pending import; the stored version it matches is surfaced (which).
        ({"cand_test": ("other", None)}, "baseline_changed"),
        ({"cand_test": ("", "load error")}, "file_invalid"),
        ({}, "file_missing"),
    ],
)
async def test_file_status(api_client, monkeypatch, files, expected) -> None:
    async with session_scope() as session:
        await seed_template(session)
        # A DB-authored v2 is the current version (the file still holds v1).
        await RuleStore(session).create_version(
            parse_template(cand_template_data(description="changed")),
            actor="web",
            note="v2",
        )
    monkeypatch.setattr(routes, "_baseline_files", lambda: files)
    items = api_client.get("/rule-templates", headers=KEY).json()
    assert items[0]["file_status"] == expected
    # The file matching v1's hash names that version even though v2 is current.
    if expected == "baseline_changed":
        versions = api_client.get("/rule-templates/cand_test", headers=KEY).json()["versions"]
        v1_hash = versions[-1]["hash"]  # newest first; the last row is v1
        monkeypatch.setattr(routes, "_baseline_files", lambda: {"cand_test": (v1_hash, None)})
        items = api_client.get("/rule-templates", headers=KEY).json()
        assert items[0]["file_status"] == "baseline_changed"
        assert items[0]["baseline_version"] == 1
    else:
        assert items[0]["baseline_version"] is None
    detail = api_client.get("/rule-templates/cand_test", headers=KEY).json()
    assert detail["file_status"] == expected


def test_condition_count_includes_nested_groups() -> None:
    from tests.unit._rules_support import template_data

    assert routes._count_conditions(template_data()["screen"]) == 4


# ---- pages ---------------------------------------------------------------


def _login(client) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


def test_template_pages_require_session(api_client) -> None:
    for path in ("/app/templates", "/app/templates/quality_growth"):
        response = api_client.get(path, follow_redirects=False)
        assert response.status_code == 302, path
        assert response.headers["location"] == "/app/login"


def test_template_page_shells(api_client) -> None:
    _login(api_client)
    html = api_client.get("/app/templates").text
    assert 'id="templates-list"' in html
    assert 'href="/app/templates" class="active" aria-current="page"' in html
    detail = api_client.get("/app/templates/quality_growth").text
    assert 'id="template-detail"' in detail
    assert 'data-template-id="quality_growth"' in detail


def test_invalid_template_id_page(api_client) -> None:
    _login(api_client)
    response = api_client.get("/app/templates/Bad-Id")
    assert response.status_code == 400

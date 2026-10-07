"""P1-D3 API: /candidates, /rules, /screen-runs (auth, CSRF, shapes, flows)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from sqlalchemy import select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.web.session import SESSION_COOKIE_NAME
from tests.unit._candidate_support import seed_promotable, seed_template

KEY = {"X-API-Key": "secret"}


@pytest.fixture
def verification_calls(monkeypatch):
    calls: list[dict[str, Any]] = []
    module = types.ModuleType("investment_agent.verification")

    def enqueue_verification(**kwargs: Any) -> None:
        calls.append(kwargs)

    module.enqueue_verification = enqueue_verification  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "investment_agent.verification", module)
    return calls


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= _keys(item)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= _keys(item)
        return found
    return set()


def _login(client) -> None:
    response = client.post(
        "/app/login", data={"api_key": "secret", "next": "/app"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.cookies.get(SESSION_COOKIE_NAME)


MUTATIONS = [
    ("post", "/candidates", {"ticker": "600519.SH"}),
    ("post", "/candidates/1/dismiss", {"reason": "x"}),
    ("post", "/candidates/1/promote", {}),
    ("post", "/rules/1/reject", {"reason": "x"}),
    ("patch", "/rules/1", {"params": {"pe_pct_max": 0.2}}),
    ("post", "/candidates/1/redraft", {}),
]


def test_endpoints_require_auth(api_client) -> None:
    for path in ("/candidates", "/candidates/1", "/rules/1", "/screen-runs/1"):
        assert api_client.get(path).status_code == 401
    for method, path, body in MUTATIONS:
        assert getattr(api_client, method)(path, json=body).status_code == 401
    assert api_client.get("/candidates", headers={"X-API-Key": "wrong"}).status_code == 401


def test_cookie_mutations_need_csrf_proof(api_client) -> None:
    _login(api_client)
    assert api_client.get("/candidates").status_code == 200
    for method, path, body in MUTATIONS:
        response = getattr(api_client, method)(path, json=body)
        assert response.status_code == 403, path
        response = getattr(api_client, method)(
            path, json=body, headers={"Origin": "https://evil.example", "X-Requested-With": "fetch"}
        )
        assert response.status_code == 403, path
    # With the fetch marker the request reaches the handler (404: nothing seeded).
    response = api_client.post(
        "/candidates/1/dismiss", json={"reason": "x"}, headers={"X-Requested-With": "fetch"}
    )
    assert response.status_code == 404


def test_api_key_failures_are_throttled(api_client) -> None:
    for _ in range(5):
        assert api_client.get("/candidates", headers={"X-API-Key": "nope"}).status_code == 401
    assert api_client.get("/candidates", headers={"X-API-Key": "nope"}).status_code == 429


async def test_list_and_detail(api_client) -> None:
    ids = await seed_promotable()
    response = api_client.get("/candidates?status=open&limit=10", headers=KEY)
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1 and page["limit"] == 10 and page["offset"] == 0
    [item] = page["items"]
    assert item["id"] == ids["candidate_id"] and item["ticker"] == "600519.SH"
    assert item["name"] == "贵州茅台"
    assert item["calibration_status"] == "uncalibrated"
    assert item["template_refs"] == ["cand_test@1"]
    assert item["draft_state"] == "ready"
    assert item["draft_rule"]["rule_instance_id"] == ids["draft_rule_id"]
    assert item["draft_rule"]["params"] == {"pe_pct_max": 0.25}
    latest = item["latest_screen"]
    assert latest["screen_result_id"] == ids["screen_result_id"]
    assert [r["factor"] for r in latest["reasons"]] == ["roe", "gross_margin"]
    assert latest["reasons"][0]["snapshot_ref"] == {
        "financial_snapshot_id": 11,
        "report_period": "2025-12-31",
    }
    assert not {"score", "strength", "investment_score", "business_score"} & _keys(page)

    assert api_client.get("/candidates?status=bogus", headers=KEY).status_code == 422
    assert api_client.get("/candidates?limit=1000", headers=KEY).status_code == 422
    assert api_client.get("/candidates?status=dismissed", headers=KEY).json()["total"] == 0

    detail = api_client.get(f"/candidates/{ids['candidate_id']}", headers=KEY).json()
    assert [d["kind"] for d in detail["decisions"]] == ["nominate"]
    assert detail["decisions"][0]["actor"] == "system"
    assert detail["screen_results"][0]["context"]["industry_group"] == "consumer"
    rule = detail["draft_rule_detail"]
    assert rule["draft_rationale"].startswith("valuation")
    assert rule["param_ranges"]["pe_pct_max"] == {"min": 0.1, "max": 0.6}
    assert rule["effective_params"] == {"roe_min": 0.1, "pe_pct_max": 0.25}
    assert not {"score", "strength"} & _keys(detail)
    assert api_client.get("/candidates/999", headers=KEY).status_code == 404


async def test_manual_nomination(api_client) -> None:
    async with session_scope() as session:
        await seed_template(session)
    response = api_client.post(
        "/candidates",
        json={"ticker": "600900.sh", "template_id": "cand_test", "note": "现金流稳定"},
        headers=KEY,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True and body["draft_run_id"] is not None
    candidate = body["candidate"]
    assert candidate["ticker"] == "600900.SH" and candidate["draft_state"] == "pending"
    assert candidate["sources"][0]["kind"] == "manual"
    assert candidate["decisions"][0]["kind"] == "nominate"
    assert candidate["decisions"][0]["actor"] == "user"
    for bad in ({"ticker": "600900"}, {"ticker": "AAPL"}, {"ticker": "600900.SH", "template_id": "X!"}):
        assert api_client.post("/candidates", json=bad, headers=KEY).status_code == 422
    unknown = api_client.post(
        "/candidates", json={"ticker": "600900.SH", "template_id": "unknown"}, headers=KEY
    )
    assert unknown.status_code == 422


async def test_redraft_queues_a_new_run_each_time(api_client) -> None:
    async with session_scope() as session:
        await seed_template(session)
    created = api_client.post(
        "/candidates", json={"ticker": "600900.SH", "template_id": "cand_test"}, headers=KEY
    ).json()
    candidate_id = created["candidate"]["id"]
    first_run = created["draft_run_id"]

    # A preference note changes the drafting payload: re-nominating with the
    # same template used to hit the idempotency payload guard (HTTP 500).
    other = api_client.post(
        "/candidates", json={"ticker": "601398.SH", "template_id": "cand_test"}, headers=KEY
    ).json()
    api_client.post(
        f"/candidates/{other['candidate']['id']}/dismiss", json={"reason": "估值不便宜"}, headers=KEY
    )
    again = api_client.post(
        "/candidates", json={"ticker": "600900.SH", "template_id": "cand_test"}, headers=KEY
    )
    assert again.status_code == 201, again.text

    first = api_client.post(f"/candidates/{candidate_id}/redraft", json={}, headers=KEY)
    # A double click while the re-draft is still queued reuses that run.
    double = api_client.post(f"/candidates/{candidate_id}/redraft", json={}, headers=KEY)
    assert (first.status_code, double.status_code) == (202, 202), first.text
    assert double.json()["draft_run_id"] == first.json()["draft_run_id"]
    # Once it finished, another re-draft really drafts again.
    async with session_scope() as session:
        done = await session.get(PipelineRun, first.json()["draft_run_id"])
        assert done is not None
        done.status = "success"
    second = api_client.post(f"/candidates/{candidate_id}/redraft", json={}, headers=KEY)
    assert second.status_code == 202, second.text
    redrafts = [first, second]
    run_ids = [first_run, *(r.json()["draft_run_id"] for r in redrafts)]
    assert len(set(run_ids)) == 3
    async with session_scope() as session:
        runs = {
            row.id: row
            for row in (
                await session.execute(select(PipelineRun).where(PipelineRun.kind == "rule_draft"))
            ).scalars()
        }
    keys = [runs[run_id].idempotency_key for run_id in run_ids]
    assert keys[0] == f"rule_draft:{candidate_id}:cand_test@1"
    assert keys[1] == f"{keys[0]}:r1" and keys[2] == f"{keys[0]}:r2"
    assert runs[run_ids[1]].request_json["preference_notes"][0].endswith("估值不便宜")
    assert redrafts[0].json()["candidate"]["draft_state"] == "pending"

    missing = api_client.post("/candidates/99999/redraft", json={}, headers=KEY)
    assert missing.status_code == 404
    unknown = api_client.post(
        f"/candidates/{candidate_id}/redraft", json={"template_id": "nope"}, headers=KEY
    )
    assert unknown.status_code == 422


async def test_dismiss(api_client) -> None:
    ids = await seed_promotable()
    url = f"/candidates/{ids['candidate_id']}/dismiss"
    assert api_client.post(url, json={"reason": "   "}, headers=KEY).status_code == 422
    assert api_client.post(url, json={}, headers=KEY).status_code == 422
    response = api_client.post(url, json={"reason": "周期顶部"}, headers=KEY)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "dismissed"
    assert [d["kind"] for d in body["decisions"]] == ["nominate", "dismiss"]
    assert body["decisions"][1]["reason_text"] == "周期顶部"
    assert api_client.post(url, json={"reason": "again"}, headers=KEY).status_code == 409


async def test_promote_happy_path(api_client, monkeypatch, verification_calls) -> None:
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "proxy-key")
    ids = await seed_promotable()
    response = api_client.post(
        f"/candidates/{ids['candidate_id']}/promote", json={"reason": "批准"}, headers=KEY
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["candidate"]["status"] == "promoted"
    assert body["rule"]["rule_instance_id"] == ids["draft_rule_id"]
    assert body["rule"]["status"] == "active"
    assert body["retired_rule_instance_id"] == ids["previous_rule_id"]
    assert body["watchlist"] == {
        "listing_id": ids["listing_id"],
        "ticker": "600519.SH",
        "industry": "食品饮料",
        "industry_source": "sw2021_l1",
        "report_year": 2025,
        "report_year_source": "annual_snapshot",
        "enabled": True,
    }
    assert [d["kind"] for d in body["decisions"]] == ["approve_rule", "promote"]
    assert body["verification_requested"] is True and body["warnings"] == []
    assert body["research_run_id"] is not None
    assert len(verification_calls) == 1 and verification_calls[0]["trigger"] == "promote"

    async with session_scope() as session:
        run = await session.get(PipelineRun, body["research_run_id"])
        assert run is not None and run.kind == "ifind_research" and run.status == "queued"
        previous = await session.get(RuleInstanceRecord, ids["previous_rule_id"])
        assert previous is not None and previous.status == "retired"
    watchlist = api_client.get("/watchlist", headers=KEY).json()
    assert [(w["ticker"], w["industry"], w["report_year"]) for w in watchlist] == [
        ("600519.SH", "食品饮料", 2025)
    ]
    again = api_client.post(f"/candidates/{ids['candidate_id']}/promote", json={}, headers=KEY)
    assert again.status_code == 409


async def test_promote_override_and_conflicts(api_client, verification_calls) -> None:
    ids = await seed_promotable()
    url = f"/candidates/{ids['candidate_id']}/promote"
    out_of_range = api_client.post(url, json={"params_override": {"pe_pct_max": 0.9}}, headers=KEY)
    assert out_of_range.status_code == 422
    assert out_of_range.json()["detail"]["issues"][0]["code"] == "param_out_of_range"
    unknown = api_client.post(url, json={"params_override": {"nope": 0.2}}, headers=KEY)
    assert unknown.status_code == 422
    async with session_scope() as session:
        candidate = await session.get(CandidateRecord, ids["candidate_id"])
        assert candidate is not None and candidate.status == "open"
        kinds = [row.kind for row in (await session.execute(select(DecisionRecord))).scalars()]
        assert kinds == ["nominate"]
    assert api_client.post(url, json={"rule_instance_id": 999}, headers=KEY).status_code == 404

    response = api_client.post(url, json={"params_override": {"pe_pct_max": 0.2}}, headers=KEY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [d["kind"] for d in body["decisions"]] == ["edit_rule", "approve_rule", "promote"]
    assert body["rule"]["version"] == 3 and body["rule"]["params"] == {"pe_pct_max": 0.2}
    # Research after promotion reads the free-source snapshot store (P1-B5).
    assert not any("IFIND_PROXY_API_KEY" in warning for warning in body["warnings"])


async def test_promote_without_rule_is_409(api_client) -> None:
    ids = await seed_promotable(previous_active=False, draft=False)
    response = api_client.post(f"/candidates/{ids['candidate_id']}/promote", json={}, headers=KEY)
    assert response.status_code == 409
    assert "drafting not ready" in response.json()["detail"]


async def test_rule_endpoints_and_reject_feeds_preferences(api_client) -> None:
    ids = await seed_promotable()
    rule_url = f"/rules/{ids['draft_rule_id']}"
    rule = api_client.get(rule_url, headers=KEY).json()
    assert rule["status"] == "draft" and rule["template_ref"] == "cand_test@1"
    assert rule["calibration_status"] == "uncalibrated"
    assert api_client.get("/rules/999", headers=KEY).status_code == 404

    bad = api_client.patch(rule_url, json={"params": {"roe_min": 0.9}}, headers=KEY)
    assert bad.status_code == 422
    assert api_client.patch(rule_url, json={"params": {}}, headers=KEY).status_code == 422
    edited = api_client.patch(
        rule_url, json={"params": {"roe_min": 0.15}, "reason": "更严"}, headers=KEY
    )
    assert edited.status_code == 200, edited.text
    new_rule = edited.json()["rule"]
    assert new_rule["version"] == 3 and new_rule["status"] == "draft"
    assert new_rule["params"] == {"pe_pct_max": 0.25, "roe_min": 0.15}
    assert edited.json()["decision"]["kind"] == "edit_rule"
    detail = api_client.get(f"/candidates/{ids['candidate_id']}", headers=KEY).json()
    assert detail["draft_rule"]["rule_instance_id"] == new_rule["rule_instance_id"]

    reject_url = f"/rules/{new_rule['rule_instance_id']}/reject"
    assert api_client.post(reject_url, json={"reason": ""}, headers=KEY).status_code == 422
    rejected = api_client.post(reject_url, json={"reason": "阈值太松"}, headers=KEY)
    assert rejected.status_code == 200
    assert rejected.json()["rule"]["status"] == "retired"
    assert rejected.json()["decision"]["kind"] == "reject_rule"
    assert api_client.post(reject_url, json={"reason": "again"}, headers=KEY).status_code == 409
    detail = api_client.get(f"/candidates/{ids['candidate_id']}", headers=KEY).json()
    assert detail["draft_state"] == "none" and detail["draft_rule"] is None

    nominated = api_client.post(
        "/candidates", json={"ticker": "600900.SH", "template_id": "cand_test"}, headers=KEY
    ).json()
    async with session_scope() as session:
        run = await session.get(PipelineRun, nominated["draft_run_id"])
        assert run is not None
        assert run.request_json["preference_notes"] == ["[cand_test@1] reject_rule 600519.SH: 阈值太松"]


async def test_screen_run_summary(api_client) -> None:
    ids = await seed_promotable()
    response = api_client.get(f"/screen-runs/{ids['run_id']}", headers=KEY)
    assert response.status_code == 200
    body = response.json()
    [template] = body["templates"]
    assert template["template_ref"] == "cand_test@1"
    assert template["calibration_status"] == "uncalibrated"
    assert template["counts"] == {"hit": 1}
    [hit] = template["hits"]
    assert hit["ticker"] == "600519.SH" and hit["candidate_id"] == ids["candidate_id"]
    assert str(body["as_of"]) == "2026-09-27"
    assert not {"strength", "score"} & _keys(body)
    assert api_client.get("/screen-runs/999", headers=KEY).status_code == 404
    async with session_scope() as session:
        other = (
            await session.execute(select(PipelineRun).where(PipelineRun.kind == "rule_draft"))
        ).scalar_one()
    assert api_client.get(f"/screen-runs/{other.id}", headers=KEY).status_code == 404

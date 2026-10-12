"""Watchlist rule drafts (watch-holding-plan 1.5): request identity, guarded
store, review API and the removal journal.

The real ``RuleDraftHandler`` runs against a stub LLM client and a stub stock
loader (fixed factor values, no snapshots), so the whole chain - add with a
template, worker claim, guarded store-and-link, approval/rejection/edit - is
exercised end to end on a migrated SQLite database.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.registry import FactorValue
from investment_agent.research.worker import ResearchWorker
from investment_agent.rules.drafting import RuleDraftHandler, StockContext
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.candidates import DecisionRecord
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.storage.models.user_domain import WatchDraftRequest, WatchlistItem

TICKER = "600519.SH"
TEMPLATE = "quality_growth"
KEY = {"X-API-Key": "secret"}
FACTORS: dict[str, float | None] = {
    "roe": 0.36,
    "roe_stability": 0.95,
    "revenue_growth_3y": 0.16,
    "net_profit_growth_3y": 0.15,
    "gross_margin": 0.92,
    "pe_percentile_5y": 0.08,
    "pb_percentile_5y": 0.05,
    "market_cap": 1.8e12,
    "is_st": 0.0,
}
INDUSTRY = {"group": "consumer", "shenwan": {"code": "340501", "name": "白酒Ⅲ"}}


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    from investment_agent.api.app import create_app

    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield TestClient(create_app())
    await engine.dispose()


async def _sync_templates() -> dict[tuple[str, int], Any]:
    async with session_scope() as session:
        await RuleStore(session).sync_templates(load_templates())
    async with session_scope() as session:
        templates = await RuleStore(session).latest_templates()
    return {(template.id, template.version): template for template in templates}


def _factor_values(values: dict[str, float | None]) -> dict[str, FactorValue]:
    return {
        factor.name: FactorValue(
            factor.name,
            factor.version,
            values.get(factor.name),
            "ok" if values.get(factor.name) is not None else "missing",
        )
        for factor in DEFAULT_REGISTRY
    }


async def stub_loader(session, ticker: str, as_of: date) -> StockContext:  # noqa: ANN001
    if ticker != TICKER:
        raise LookupError(f"no listing for {ticker}")
    return StockContext(factors=_factor_values(FACTORS), industry=INDUSTRY)


def _valid_output(template: Any) -> dict[str, Any]:
    """A minimal valid draft: one adjusted buy_ param with a live citation."""

    name = next(param for param in template.params if param.startswith("buy_"))
    bounds = template.param_ranges[name]
    value = round((bounds.min + template.params[name]) / 2, 4)
    cited = "pe_percentile_5y" if "_pe_" in name else "pb_percentile_5y"
    return {
        "params": {name: value},
        "rationale": [
            {
                "param": name,
                "choice": "adjusted",
                "reason": f"{cited} 显示估值位置，收紧 buy_zone 参数，属研究优先级假设。",
                "cited_factors": [cited],
            },
            {
                "param": next(iter(template.params)),
                "choice": "default",
                "reason": "模板未校准，暂无依据调整，沿用默认值。",
                "cited_factors": [],
            },
        ],
        "overall_rationale": "沿用模板全部条件，只收紧 buy_zone 的估值分位参数；模板未校准。",
        "watch_points": [f"{cited} 回升到 drop 条件附近时需要复核。"],
    }


class FakeDraftLLM:
    def __init__(self, templates: dict[tuple[str, int], Any]) -> None:
        self.templates = templates
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        output_model: Any,
        purpose: str,
        pipeline_run_id: int | None = None,
    ) -> SimpleNamespace:
        self.calls.append({"purpose": purpose, "run_id": pipeline_run_id})
        data = json.loads(user_message)
        template_id, version = data["template"]["ref"].split("@", 1)
        template = self.templates[(template_id, int(version))]
        return SimpleNamespace(
            output=output_model.model_validate(_valid_output(template)),
            model="stub-model",
            text="",
        )


def _worker(settings: Settings, llm: FakeDraftLLM) -> ResearchWorker:
    return ResearchWorker(
        settings=settings,
        worker_id="watch-test",
        heartbeat_interval_seconds=30.0,
        handlers=[
            RuleDraftHandler(
                settings=settings,
                client_factory=lambda session: llm,
                stock_loader=stub_loader,
            )
        ],
    )


async def _run(run_id: int) -> PipelineRun:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        return run


async def _item(listing_id: int) -> WatchlistItem:
    async with session_scope() as session:
        item = (
            await session.execute(
                select(WatchlistItem).where(WatchlistItem.listing_id == listing_id)
            )
        ).scalar_one()
        return item


async def _request(request_id: str) -> WatchDraftRequest:
    async with session_scope() as session:
        request = await session.get(WatchDraftRequest, request_id)
        assert request is not None
        return request


async def _decisions(kind: str) -> list[DecisionRecord]:
    async with session_scope() as session:
        rows = (
            await session.execute(select(DecisionRecord).where(DecisionRecord.kind == kind))
        ).scalars()
        return list(rows)


async def _rule_count() -> int:
    async with session_scope() as session:
        return (
            await session.execute(select(func.count()).select_from(RuleInstanceRecord))
        ).scalar_one()


def _add(client: TestClient, **extra: Any) -> dict[str, Any]:
    body = {"ticker": TICKER, "name": "贵州茅台", "industry": "食品饮料", **extra}
    response = client.post("/watchlist", json=body, headers=KEY)
    assert response.status_code == 201, response.text
    return response.json()


async def _queued_run(request_id: str) -> int:
    async with session_scope() as session:
        rows = (
            await session.execute(select(PipelineRun).where(PipelineRun.kind == "rule_draft"))
        ).scalars()
        for run in rows:
            if (run.request_json or {}).get("draft_request_id") == request_id:
                assert run.status == "queued", run.status
                return run.id
    raise AssertionError(f"no queued rule_draft run for request {request_id}")


async def test_add_with_template_runs_to_ready_and_approves(env) -> None:
    templates = await _sync_templates()
    llm = FakeDraftLLM(templates)
    worker = _worker(Settings(), llm)  # type: ignore[call-arg]

    body = _add(env, template_id=TEMPLATE)
    rule = body["rule"]
    assert rule["state"] == "queued"
    assert rule["request"]["template_ref"] == f"{TEMPLATE}@1"
    request_id = rule["request"]["id"]
    run_id = await _queued_run(request_id)

    assert await worker.run_once(run_id=run_id) == run_id
    listed = env.get("/watchlist", headers=KEY).json()
    rule = listed[0]["rule"]
    assert rule["state"] == "ready"
    assert rule["draft"]["template_ref"] == f"{TEMPLATE}@1"
    assert rule["active"] is None
    # No zone detection has run for this listing yet; the block appears once
    # the daily task has evaluated an approved rule (WP8).
    assert rule["zone_events"] is None
    rule_id = rule["draft"]["rule_instance_id"]
    assert llm.calls and llm.calls[0]["purpose"] == "rule_draft"

    approved = env.post(
        f"/watchlist/{body['listing_id']}/rule/approve",
        json={"rule_instance_id": rule_id},
        headers=KEY,
    )
    assert approved.status_code == 200, approved.text
    rule = approved.json()
    assert rule["state"] == "none"
    assert rule["draft"] is None
    assert rule["active"]["rule_instance_id"] == rule_id
    assert rule["active"]["version"] == 1

    decisions = await _decisions("approve_rule")
    assert [row.rule_instance_id for row in decisions] == [rule_id]
    assert decisions[0].candidate_id is None
    assert decisions[0].context["source"] == "watchlist"


async def test_add_without_template_needs_template_then_explicit_draft(env) -> None:
    templates = await _sync_templates()
    llm = FakeDraftLLM(templates)
    worker = _worker(Settings(), llm)  # type: ignore[call-arg]

    body = _add(env)
    assert body["rule"]["state"] == "needs_template"
    assert body["rule"]["request"] is None

    drafted = env.post(
        f"/watchlist/{body['listing_id']}/rule/draft",
        json={"template_id": TEMPLATE},
        headers=KEY,
    )
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["state"] == "queued"
    run_id = await _queued_run(drafted.json()["request"]["id"])
    await worker.run_once(run_id=run_id)
    assert env.get("/watchlist", headers=KEY).json()[0]["rule"]["state"] == "ready"


async def test_redraft_supersedes_and_late_worker_is_audit_only(env) -> None:
    templates = await _sync_templates()
    llm = FakeDraftLLM(templates)
    worker = _worker(Settings(), llm)  # type: ignore[call-arg]

    body = _add(env, template_id=TEMPLATE)
    first = body["rule"]["request"]["id"]
    first_run = await _queued_run(first)

    redrafted = env.post(
        f"/watchlist/{body['listing_id']}/rule/draft",
        json={"template_id": TEMPLATE},
        headers=KEY,
    )
    assert redrafted.status_code == 200, redrafted.text
    second = redrafted.json()["request"]["id"]
    assert second != first
    second_run = await _queued_run(second)
    assert (await _request(first)).superseded_by == second

    # The superseded worker finishes late: audit only, nothing stored.
    await worker.run_once(run_id=first_run)
    stale = await _run(first_run)
    assert stale.status == "partial"
    assert "superseded before store" in (stale.error_summary or "")
    assert await _rule_count() == 0
    item = await _item(body["listing_id"])
    assert item.draft_request_id == second
    assert item.draft_state == "queued"
    assert (await _request(first)).superseded_by == second

    # The current request still drafts normally.
    await worker.run_once(run_id=second_run)
    assert env.get("/watchlist", headers=KEY).json()[0]["rule"]["state"] == "ready"


async def test_retried_add_reuses_request_identity(env) -> None:
    await _sync_templates()
    first = _add(env, template_id=TEMPLATE)
    again = _add(env, template_id=TEMPLATE)
    assert again["rule"]["request"]["id"] == first["rule"]["request"]["id"]
    async with session_scope() as session:
        runs = list(
            (await session.execute(select(PipelineRun).where(PipelineRun.kind == "rule_draft")))
            .scalars()
        )
        assert len(runs) == 1


async def test_reject_requires_reason_and_never_redrafts(env) -> None:
    templates = await _sync_templates()
    llm = FakeDraftLLM(templates)
    worker = _worker(Settings(), llm)  # type: ignore[call-arg]
    body = _add(env, template_id=TEMPLATE)
    run_id = await _queued_run(body["rule"]["request"]["id"])
    await worker.run_once(run_id=run_id)
    rule_id = env.get("/watchlist", headers=KEY).json()[0]["rule"]["draft"]["rule_instance_id"]

    missing = env.post(
        f"/watchlist/{body['listing_id']}/rule/reject",
        json={"rule_instance_id": rule_id},
        headers=KEY,
    )
    assert missing.status_code == 422

    rejected = env.post(
        f"/watchlist/{body['listing_id']}/rule/reject",
        json={"rule_instance_id": rule_id, "reason": "参数假设太激进"},
        headers=KEY,
    )
    assert rejected.status_code == 200, rejected.text
    rule = rejected.json()
    assert rule["state"] == "rejected"
    assert rule["draft"] is None
    assert rule["request"]["status"] == "rejected"
    assert rule["request"]["reason"] == "参数假设太激进"

    async with session_scope() as session:
        record = await session.get(RuleInstanceRecord, rule_id)
        assert record is not None and record.status == "retired"
        runs = list(
            (await session.execute(select(PipelineRun).where(PipelineRun.kind == "rule_draft")))
            .scalars()
        )
        assert len(runs) == 1  # no automatic re-draft
    decisions = await _decisions("reject_rule")
    assert decisions and decisions[-1].reason_text == "参数假设太激进"


async def test_edit_params_then_approve_new_version(env) -> None:
    templates = await _sync_templates()
    llm = FakeDraftLLM(templates)
    worker = _worker(Settings(), llm)  # type: ignore[call-arg]
    body = _add(env, template_id=TEMPLATE)
    await worker.run_once(run_id=await _queued_run(body["rule"]["request"]["id"]))
    listed = env.get("/watchlist", headers=KEY).json()[0]["rule"]
    rule_id, params = listed["draft"]["rule_instance_id"], listed["draft"]["params"]
    assert params

    name = next(iter(params))
    edited = env.post(
        f"/watchlist/{body['listing_id']}/rule/edit",
        json={"rule_instance_id": rule_id, "params": {name: params[name]}, "reason": "微调"},
        headers=KEY,
    )
    assert edited.status_code == 200, edited.text
    rule = edited.json()
    assert rule["draft"]["rule_instance_id"] != rule_id
    assert rule["draft"]["version"] == 2
    assert rule["state"] == "ready"

    # The old version was retired by the edit; approving it conflicts.
    stale = env.post(
        f"/watchlist/{body['listing_id']}/rule/approve",
        json={"rule_instance_id": rule_id},
        headers=KEY,
    )
    assert stale.status_code == 409

    new_id = rule["draft"]["rule_instance_id"]
    approved = env.post(
        f"/watchlist/{body['listing_id']}/rule/approve",
        json={"rule_instance_id": new_id},
        headers=KEY,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["active"]["rule_instance_id"] == new_id
    assert any(row.kind == "edit_rule" for row in await _decisions("edit_rule"))


async def test_repeat_add_never_replaces_active_rule(env) -> None:
    templates = await _sync_templates()
    llm = FakeDraftLLM(templates)
    worker = _worker(Settings(), llm)  # type: ignore[call-arg]
    body = _add(env, template_id=TEMPLATE)
    await worker.run_once(run_id=await _queued_run(body["rule"]["request"]["id"]))
    rule_id = env.get("/watchlist", headers=KEY).json()[0]["rule"]["draft"]["rule_instance_id"]
    env.post(
        f"/watchlist/{body['listing_id']}/rule/approve",
        json={"rule_instance_id": rule_id},
        headers=KEY,
    )

    again = _add(env, template_id=TEMPLATE)
    assert again["rule"]["state"] == "none"
    assert again["rule"]["active"]["rule_instance_id"] == rule_id
    assert await _rule_count() == 1
    without_template = _add(env)
    assert without_template["rule"]["state"] == "none"
    assert without_template["rule"]["draft"] is None


async def test_redraft_invalidates_previous_review(env) -> None:
    templates = await _sync_templates()
    worker = _worker(Settings(), FakeDraftLLM(templates))
    body = _add(env, template_id=TEMPLATE)
    await worker.run_once(run_id=await _queued_run(body["rule"]["request"]["id"]))
    old = env.get("/watchlist", headers=KEY).json()[0]["rule"]["draft"]["rule_instance_id"]
    new = env.post(f"/watchlist/{body['listing_id']}/rule/draft", json={"template_id": TEMPLATE}, headers=KEY)
    assert new.status_code == 200
    assert new.json()["draft"] is None
    approve = env.post(f"/watchlist/{body['listing_id']}/rule/approve", json={"rule_instance_id": old}, headers=KEY)
    assert approve.status_code == 409


async def test_late_store_cannot_reopen_rejected_or_approved_rule(env) -> None:
    from investment_agent.rules.store import instance_from_record
    from investment_agent.watchlist.drafts import store_watch_draft

    templates = await _sync_templates()
    worker = _worker(Settings(), FakeDraftLLM(templates))
    body = _add(env, template_id=TEMPLATE)
    request_id = body["rule"]["request"]["id"]
    await worker.run_once(run_id=await _queued_run(request_id))
    old = env.get("/watchlist", headers=KEY).json()[0]["rule"]["draft"]["rule_instance_id"]
    async with session_scope() as session:
        instance = instance_from_record(await session.get(RuleInstanceRecord, old))
    response = env.post(f"/watchlist/{body['listing_id']}/rule/reject", json={"rule_instance_id": old, "reason": "reject"}, headers=KEY)
    assert response.status_code == 200
    assert await store_watch_draft(request_id, instance, templates[(TEMPLATE, 1)]) is None
    assert await _rule_count() == 1
    assert env.get("/watchlist", headers=KEY).json()[0]["rule"]["state"] == "rejected"

    redraft = env.post(f"/watchlist/{body['listing_id']}/rule/draft", json={"template_id": TEMPLATE}, headers=KEY).json()
    next_request = redraft["request"]["id"]
    await worker.run_once(run_id=await _queued_run(next_request))
    next_rule = env.get("/watchlist", headers=KEY).json()[0]["rule"]["draft"]["rule_instance_id"]
    async with session_scope() as session:
        instance = instance_from_record(await session.get(RuleInstanceRecord, next_rule))
    approved = env.post(f"/watchlist/{body['listing_id']}/rule/approve", json={"rule_instance_id": next_rule, "expected_active_rule_instance_id": None}, headers=KEY)
    assert approved.status_code == 200
    assert await store_watch_draft(next_request, instance, templates[(TEMPLATE, 1)]) is None
    # The request keeps its in-enum "ready" status after the approval; the
    # late store is refused because the produced rule is no longer an open
    # draft ("approved" itself is not a WATCH_DRAFT_REQUEST_STATUS).
    assert (await _request(next_request)).status == "ready"


async def test_approval_requires_the_active_version_seen_by_user(env) -> None:
    templates = await _sync_templates()
    worker = _worker(Settings(), FakeDraftLLM(templates))
    body = _add(env, template_id=TEMPLATE)
    await worker.run_once(run_id=await _queued_run(body["rule"]["request"]["id"]))
    path = f"/watchlist/{body['listing_id']}/rule"
    previous = env.get("/watchlist", headers=KEY).json()[0]["rule"]["draft"]["rule_instance_id"]
    assert env.post(f"{path}/approve", json={"rule_instance_id": previous}, headers=KEY).status_code == 200
    next_request = env.post(f"{path}/draft", json={"template_id": TEMPLATE}, headers=KEY).json()["request"]["id"]
    await worker.run_once(run_id=await _queued_run(next_request))
    latest = env.get("/watchlist", headers=KEY).json()[0]["rule"]["draft"]["rule_instance_id"]
    assert env.post(f"{path}/approve", json={"rule_instance_id": latest, "expected_active_rule_instance_id": None}, headers=KEY).status_code == 409
    assert env.post(f"{path}/approve", json={"rule_instance_id": latest, "expected_active_rule_instance_id": previous}, headers=KEY).status_code == 200


async def test_watch_worker_cannot_overwrite_newer_candidate_rule(env) -> None:
    from investment_agent.rules.schema import RuleInstance

    templates = await _sync_templates()
    body = _add(env, template_id=TEMPLATE)
    async with session_scope() as session:
        newer = await RuleStore(session).create_draft(RuleInstance(template_ref=f"{TEMPLATE}@1", symbol=TICKER, scope="candidate", params={}, drafted_by="user"))
        newer_id = newer.id
    await _worker(Settings(), FakeDraftLLM(templates)).run_once(run_id=await _queued_run(body["rule"]["request"]["id"]))
    async with session_scope() as session:
        newer = await session.get(RuleInstanceRecord, newer_id)
        assert newer.status == "draft"
    assert await _rule_count() == 1
    assert env.get("/watchlist", headers=KEY).json()[0]["rule"]["state"] == "failed"


async def test_remove_requires_reason_cancels_requests_and_journals(env) -> None:
    templates = await _sync_templates()
    body = _add(env, template_id=TEMPLATE)
    request_id = body["rule"]["request"]["id"]

    bare = env.request("DELETE", f"/watchlist/{body['listing_id']}", headers=KEY)
    assert bare.status_code == 422

    removed = env.request(
        "DELETE",
        f"/watchlist/{body['listing_id']}",
        json={"reason": "不再关注"},
        headers=KEY,
    )
    assert removed.status_code == 204, removed.text
    assert env.get("/watchlist", headers=KEY).json() == []
    assert (await _request(request_id)).status == "cancelled"
    decisions = await _decisions("remove_watch")
    assert decisions and decisions[-1].reason_text == "不再关注"

    # A late worker cannot revive the removed entry.
    llm = FakeDraftLLM(templates)
    worker = _worker(Settings(), llm)  # type: ignore[call-arg]
    run_id = await _queued_run(request_id)
    await worker.run_once(run_id=run_id)
    assert env.get("/watchlist", headers=KEY).json() == []
    assert await _rule_count() == 0


async def test_add_with_unknown_template_is_422(env) -> None:
    await _sync_templates()
    response = env.post(
        "/watchlist",
        json={"ticker": TICKER, "template_id": "nope", "name": "x"},
        headers=KEY,
    )
    assert response.status_code == 422
    assert env.get("/watchlist", headers=KEY).json() == []  # nothing half-persisted


async def test_request_run_lookup_filters_in_sql(env) -> None:
    """``_request_run`` pushes the request identity into the query (no full scan)."""

    from investment_agent.watchlist.drafts import WatchDraftInputError, _request_run

    await _sync_templates()
    body = _add(env, template_id=TEMPLATE)
    request_id = body["rule"]["request"]["id"]
    async with session_scope() as session:
        assert await _request_run(session, request_id) == await _queued_run(request_id)
        with pytest.raises(WatchDraftInputError, match="no rule_draft run"):
            await _request_run(session, "no-such-request")


async def test_suggestion_daily_cap_rolls_on_the_shanghai_day(env) -> None:
    """The advisory cap counts per Beijing day: it rolls at 16:00 UTC (08:00
    Beijing), not at the server's UTC midnight."""

    from investment_agent.rules import recommend
    from investment_agent.storage.models.ops import LLMCall

    async def seed(started_at: datetime) -> None:
        async with session_scope() as session:
            session.add(
                LLMCall(
                    purpose=recommend.SUGGEST_PURPOSE,
                    model="stub",
                    provider="stub",
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost_usd=Decimal("0"),
                    latency_ms=1,
                    started_at=started_at,
                    success=True,
                )
            )

    # 2026-09-27 15:00 UTC is still Beijing 09-27 (23:00); 16:30 UTC already
    # belongs to Beijing 09-28 (00:30).
    await seed(datetime(2026, 9, 27, 15, 0, tzinfo=UTC))
    await seed(datetime(2026, 9, 27, 16, 30, tzinfo=UTC))

    async def count(now: datetime) -> int:
        async with session_scope() as session:
            return await recommend._suggestions_today(session, now=now)

    assert await count(datetime(2026, 9, 27, 15, 30, tzinfo=UTC)) == 2
    assert await count(datetime(2026, 9, 27, 16, 0, tzinfo=UTC)) == 1
    assert await count(datetime(2026, 9, 28, 12, 0, tzinfo=UTC)) == 1


async def test_template_suggestion_endpoint(env, monkeypatch) -> None:
    from investment_agent.api.routes import watchlist as routes
    from investment_agent.rules.recommend import TemplateSuggestion

    async def fake(session, *, ticker, as_of, settings):
        return TemplateSuggestion(
            template_id="quality_growth",
            template_version=1,
            name="质量成长",
            reason="消费行业，高毛利与稳定 ROE 匹配该模板。",
            model="stub-model",
        )

    monkeypatch.setattr(routes, "suggest_template", fake)
    response = env.get(
        "/watchlist/template-suggestion",
        params={"ticker": TICKER.lower()},
        headers=KEY,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["template_id"] == "quality_growth"
    assert data["reason"]

    async def empty(session, *, ticker, as_of, settings):
        return None

    monkeypatch.setattr(routes, "suggest_template", empty)
    response = env.get(
        "/watchlist/template-suggestion", params={"ticker": TICKER}, headers=KEY
    )
    assert response.status_code == 200
    assert response.json()["template_id"] is None

    bad = env.get(
        "/watchlist/template-suggestion", params={"ticker": "NOT-A-TICKER"}, headers=KEY
    )
    assert bad.status_code == 422


async def test_migration_enqueue_is_rerunnable_and_keeps_rejections(env) -> None:
    """WP9: legacy entries get one deterministic request each; re-runs no-op."""

    from investment_agent.storage.database import session_scope
    from investment_agent.watchlist.drafts import migrate_pending_entries

    templates = await _sync_templates()
    worker = _worker(Settings(), FakeDraftLLM(templates))  # type: ignore[call-arg]
    body = _add(env)  # no template: needs_template
    assert body["rule"]["state"] == "needs_template"

    def template_for(_item) -> str:
        return TEMPLATE

    async with session_scope() as session:
        first = await migrate_pending_entries(
            session, template_for=template_for, as_of=date(2026, 9, 28)
        )
    assert len(first) == 1
    item = await _item(body["listing_id"])
    request_id = item.draft_request_id
    assert request_id is not None and item.draft_state == "queued"

    # A second migration run reuses the same request and queue job.
    async with session_scope() as session:
        again = await migrate_pending_entries(
            session, template_for=template_for, as_of=date(2026, 9, 28)
        )
    assert again == []
    assert (await _item(body["listing_id"])).draft_request_id == request_id
    async with session_scope() as session:
        runs = list(
            (await session.execute(select(PipelineRun).where(PipelineRun.kind == "rule_draft")))
            .scalars()
        )
        assert len(runs) == 1

    # The migrated request drafts normally; an explicit rejection is never
    # re-drafted by a later migration.
    await worker.run_once(run_id=await _queued_run(request_id))
    rule_id = env.get("/watchlist", headers=KEY).json()[0]["rule"]["draft"]["rule_instance_id"]
    assert (
        env.post(
            f"/watchlist/{body['listing_id']}/rule/reject",
            json={"rule_instance_id": rule_id, "reason": "驳回"},
            headers=KEY,
        ).status_code
        == 200
    )
    async with session_scope() as session:
        assert (
            await migrate_pending_entries(
                session, template_for=template_for, as_of=date(2026, 9, 29)
            )
            == []
        )
    assert env.get("/watchlist", headers=KEY).json()[0]["rule"]["state"] == "rejected"


async def test_template_suggestion_cache_and_daily_limit(env, monkeypatch) -> None:
    """WP9: identical selections reuse the cached suggestion; a daily cap stops calls."""

    from investment_agent.rules import recommend
    from investment_agent.storage.database import session_scope

    await _sync_templates()

    class View:
        industry_group = "consumer"

    async def fake_view(session, ticker, as_of):  # noqa: ANN001, ANN202
        return View()

    monkeypatch.setattr(recommend, "load_snapshot_view", fake_view)
    recommend.clear_suggestion_cache()
    calls = {"count": 0}
    settings = Settings()  # type: ignore[call-arg]

    def client_factory(_session):  # noqa: ANN001, ANN202
        class Client:
            async def complete(self, **_: object) -> SimpleNamespace:
                calls["count"] += 1
                return SimpleNamespace(
                    output=recommend.TemplateChoice(template_id=TEMPLATE, reason="行业匹配"),
                    model="stub-model",
                )

        return Client()

    async def suggest(**kwargs):  # noqa: ANN003, ANN202
        async with session_scope() as session:
            return await recommend.suggest_template(
                session, settings=settings, client_factory=client_factory, **kwargs
            )

    first = await suggest(ticker=TICKER, as_of=date(2026, 9, 28))
    assert first is not None and first.template_id == TEMPLATE
    second = await suggest(ticker=TICKER, as_of=date(2026, 9, 28))
    assert second is not None and calls["count"] == 1  # cache hit, no new model call
    assert second.template_id == first.template_id

    # A different date is a different selection and calls again; the daily cap
    # then refuses further calls (the entry stays needs_template by design).
    third = await suggest(ticker=TICKER, as_of=date(2026, 9, 29))
    assert third is not None and calls["count"] == 2
    fourth = await suggest(ticker=TICKER, as_of=date(2026, 9, 30), daily_limit=0)
    assert fourth is None and calls["count"] == 2
    recommend.clear_suggestion_cache()

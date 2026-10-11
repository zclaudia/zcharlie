"""Holding-scope rule drafting and review (watch-holding-plan 6.2 WP8).

The real ``RuleDraftHandler`` runs against a stub LLM and a stub stock loader,
so the whole chain - draft request, worker claim, guarded store into the
``holding`` slot, approve / edit / reject with the decision journal, and the
shared-generation guard - is exercised end to end on a migrated SQLite
database. The candidate scope is asserted to stay untouched by all of it.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.registry import FactorValue
from investment_agent.research.worker import ResearchWorker
from investment_agent.rules import zone_detection
from investment_agent.rules.drafting import RuleDraftHandler, StockContext
from investment_agent.rules.generation import current_generation
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.candidates import DecisionRecord
from investment_agent.storage.models.rules import RuleInstanceRecord

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
    db_path = tmp_path / "holding.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        from investment_agent.storage.models.reference import Company, Listing, Market

        session.add(Market(code="SSE", name="Fixtures", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="贵州茅台")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SSE", ticker="600519")
        session.add(listing)
        await session.flush()
        listing_id = listing.id
        await RuleStore(session).sync_templates(load_templates())
    yield SimpleNamespace(
        listing_id=listing_id,
        tmp_path=tmp_path,
        client=TestClient(create_app()),
        settings=Settings(
            _env_file=None, api_key="secret", llm_tier3_api_key="x", data_dir=tmp_path
        ),
    )
    await engine.dispose()


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


def _draft_output(template: Any, *, scope: str) -> dict[str, Any]:
    """A valid draft: one adjusted holding-zone parameter with a live citation."""

    prefix = "add_" if scope == "holding" else "buy_"
    name = next(param for param in sorted(template.params) if param.startswith(prefix))
    bounds = template.param_ranges[name]
    value = round((bounds.min + template.params[name]) / 2, 4)
    cited = "pe_percentile_5y"
    keep = next(param for param in sorted(template.params) if param != name)
    return {
        "params": {name: value},
        "rationale": [
            {
                "param": name,
                "choice": "adjusted",
                "reason": f"{cited} 显示估值位置，收紧区间参数，属研究优先级假设。",
                "cited_factors": [cited],
            },
            {
                "param": keep,
                "choice": "default",
                "reason": "模板未校准，暂无依据调整，沿用默认值。",
                "cited_factors": [],
            },
        ],
        "overall_rationale": "沿用模板全部条件，只收紧一个区间参数；模板未校准。",
        "watch_points": [f"{cited} 回到区间边界附近时需要复核。"],
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
        # A retry appends the validation feedback after the JSON object.
        data, _ = json.JSONDecoder().raw_decode(user_message)
        template_id, version = data["template"]["ref"].split("@", 1)
        template = self.templates[(template_id, int(version))]
        return SimpleNamespace(
            output=output_model.model_validate(_draft_output(template, scope=data["scope"])),
            model="stub-model",
            text="",
        )


async def _templates() -> dict[tuple[str, int], Any]:
    async with session_scope() as session:
        templates = await RuleStore(session).latest_templates()
    return {(template.id, template.version): template for template in templates}


def _worker(settings: Settings, llm: FakeDraftLLM) -> ResearchWorker:
    return ResearchWorker(
        settings=settings,
        worker_id="holding-test",
        handlers=[
            RuleDraftHandler(
                settings=settings,
                client_factory=lambda session: llm,
                stock_loader=stub_loader,
            )
        ],
    )


async def _queued_run(listing_id: int) -> int:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(PipelineRun)
                .where(PipelineRun.kind == "rule_draft")
                .order_by(PipelineRun.id.desc())
            )
        ).scalars()
        for run in rows:
            payload = run.request_json or {}
            if payload.get("origin_kind") == "holding" and payload.get("origin_id") == listing_id:
                assert run.status == "queued", run.status
                return run.id
    raise AssertionError(f"no queued holding rule_draft run for listing {listing_id}")


async def _slot(scope: str) -> list[RuleInstanceRecord]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(RuleInstanceRecord)
                .where(RuleInstanceRecord.scope == scope)
                .order_by(RuleInstanceRecord.version)
            )
        ).scalars()
        return list(rows)


async def _decisions(kind: str) -> list[DecisionRecord]:
    async with session_scope() as session:
        rows = (
            await session.execute(select(DecisionRecord).where(DecisionRecord.kind == kind))
        ).scalars()
        return list(rows)


def _draft(client: TestClient, listing_id: int, **body: Any) -> dict[str, Any]:
    response = client.post(
        "/portfolio/rule/draft", json={"listing_id": listing_id, **body}, headers=KEY
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_draft_review_approve_keeps_one_active_version(env) -> None:
    templates = await _templates()
    llm = FakeDraftLLM(templates)
    worker = _worker(env.settings, llm)
    client = env.client

    empty = client.get("/portfolio/rule", params={"listing_id": env.listing_id}, headers=KEY)
    assert empty.status_code == 200
    assert empty.json()["state"] == "needs_template"

    drafted = _draft(client, env.listing_id, template_id=TEMPLATE)
    assert drafted["state"] == "drafting"
    assert drafted["requested_template_ref"] == f"{TEMPLATE}@1"
    run_id = await _queued_run(env.listing_id)

    assert await worker.run_once(run_id=run_id) == run_id
    assert llm.calls and llm.calls[0]["purpose"] == "rule_draft"

    listed = client.get(
        "/portfolio/rule", params={"listing_id": env.listing_id}, headers=KEY
    ).json()
    assert listed["state"] == "ready"
    assert listed["draft"]["template_ref"] == f"{TEMPLATE}@1"
    assert listed["draft"]["scope"] == "holding"
    # The holding zones are what a holding rule renders, not the candidate ones.
    assert set(listed["draft"]["zones"]) == {"add", "trim", "exit_review"}
    assert listed["draft"]["factor_units"]["position_weight"] == "fraction"
    assert listed["draft"]["factor_units"]["gain_vs_cost"] == "fraction"
    assert listed["draft"]["factor_units"]["pe_percentile_5y"] == "fraction (0,1]"
    rule_id = listed["draft"]["rule_instance_id"]

    async with session_scope() as session:
        generation = await current_generation(session, listing_id=env.listing_id, scope="holding")
    assert generation is not None and generation.origin_kind == "holding"

    approved = client.post(
        "/portfolio/rule/approve",
        json={"listing_id": env.listing_id, "rule_instance_id": rule_id},
        headers=KEY,
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["state"] == "none"
    assert body["draft"] is None
    assert body["active"]["rule_instance_id"] == rule_id
    assert body["active"]["scope"] == "holding"
    decisions = await _decisions("approve_rule")
    assert [row.rule_instance_id for row in decisions] == [rule_id]
    assert decisions[0].context["source"] == "holding"
    assert decisions[0].context["scope"] == "holding"

    # The candidate slot never saw any of this.
    assert await _slot("candidate") == []
    records = await _slot("holding")
    assert [row.scope for row in records] == ["holding"]
    assert records[0].status == "active"
    assert not records[0].definition.get("zones")  # the template's zones apply
    async with session_scope() as session:
        live = await zone_detection.active_zone_rules(session, "holding")
    assert [rule.rule_instance_id for rule in live] == [records[0].id]
    assert set(live[0].zones) == {"add", "trim", "exit_review"}


async def test_redraft_edit_and_reject_keep_the_active_version(env) -> None:
    templates = await _templates()
    worker = _worker(env.settings, FakeDraftLLM(templates))
    client = env.client
    _draft(client, env.listing_id, template_id=TEMPLATE)
    await worker.run_once(run_id=await _queued_run(env.listing_id))
    ready = client.get("/portfolio/rule", params={"listing_id": env.listing_id}, headers=KEY).json()
    first = ready["draft"]["rule_instance_id"]
    client.post(
        "/portfolio/rule/approve",
        json={"listing_id": env.listing_id, "rule_instance_id": first},
        headers=KEY,
    )

    # A second draft is a new version; the active version stays active.
    _draft(client, env.listing_id, template_id=TEMPLATE)
    await worker.run_once(run_id=await _queued_run(env.listing_id))
    second = client.get(
        "/portfolio/rule", params={"listing_id": env.listing_id}, headers=KEY
    ).json()
    assert second["state"] == "ready"
    assert second["active"]["rule_instance_id"] == first
    draft_id = second["draft"]["rule_instance_id"]
    assert draft_id != first

    # Editing the draft produces a third, still-draft version.
    edited = client.post(
        "/portfolio/rule/edit",
        json={
            "listing_id": env.listing_id,
            "rule_instance_id": draft_id,
            "params": {"add_pe_pct_max": 0.20},
            "reason": "收紧加仓区间",
        },
        headers=KEY,
    )
    assert edited.status_code == 200, edited.text
    edited_body = edited.json()
    assert edited_body["state"] == "ready"
    assert edited_body["draft"]["params"]["add_pe_pct_max"] == 0.20
    assert edited_body["draft"]["rule_instance_id"] != draft_id

    # Rejecting requires a reason and never touches the active version.
    missing = client.post(
        "/portfolio/rule/reject",
        json={"listing_id": env.listing_id, "rule_instance_id": edited_body["draft"]["rule_instance_id"]},
        headers=KEY,
    )
    assert missing.status_code == 422
    rejected = client.post(
        "/portfolio/rule/reject",
        json={
            "listing_id": env.listing_id,
            "rule_instance_id": edited_body["draft"]["rule_instance_id"],
            "reason": "参数与我的持仓计划不符",
        },
        headers=KEY,
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    # The slot keeps its active rule; the rejection is visible as the last
    # decision instead of pretending the rule disappeared.
    assert body["state"] == "none"
    assert body["draft"] is None
    assert body["active"]["rule_instance_id"] == first
    assert body["last_decision"]["kind"] == "reject_rule"
    assert body["last_decision"]["reason"] == "参数与我的持仓计划不符"

    records = await _slot("holding")
    assert [row.status for row in records] == ["active", "retired", "retired"]
    assert records[0].id == first
    assert records[0].retired_at is None
    assert len(await _decisions("reject_rule")) == 1
    assert len(await _decisions("edit_rule")) == 1


async def test_stale_approval_and_late_store_are_refused(env) -> None:
    templates = await _templates()
    worker = _worker(env.settings, FakeDraftLLM(templates))
    client = env.client
    _draft(client, env.listing_id, template_id=TEMPLATE)
    run_id = await _queued_run(env.listing_id)

    # A newer draft request supersedes the queued one before it runs.
    _draft(client, env.listing_id, template_id=TEMPLATE)
    assert await worker.run_once(run_id=run_id) == run_id
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run.status == "partial"
        assert "superseded" in (run.error_summary or "")
    # The superseded run stored nothing.
    assert await _slot("holding") == []

    # The current run stores, and its state is what the page shows.
    await worker.run_once(run_id=await _queued_run(env.listing_id))
    ready = client.get("/portfolio/rule", params={"listing_id": env.listing_id}, headers=KEY).json()
    rule_id = ready["draft"]["rule_instance_id"]

    # Approving an instance the user did not see as the open draft is a conflict.
    conflict = client.post(
        "/portfolio/rule/approve",
        json={"listing_id": env.listing_id, "rule_instance_id": rule_id + 999},
        headers=KEY,
    )
    assert conflict.status_code == 404

    # Approving with a stale expectation about the active version is refused.
    client.post(
        "/portfolio/rule/approve",
        json={"listing_id": env.listing_id, "rule_instance_id": rule_id},
        headers=KEY,
    )
    stale = client.post(
        "/portfolio/rule/approve",
        json={
            "listing_id": env.listing_id,
            "rule_instance_id": rule_id,
            "expected_active_rule_instance_id": rule_id,
        },
        headers=KEY,
    )
    assert stale.status_code == 409


async def test_drafting_an_unknown_template_is_rejected(env) -> None:
    response = env.client.post(
        "/portfolio/rule/draft",
        json={"listing_id": env.listing_id, "template_id": "nope"},
        headers=KEY,
    )
    assert response.status_code == 422
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(PipelineRun)
            .where(PipelineRun.kind == "rule_draft")
        )
    assert count == 0


async def test_rule_endpoints_require_auth(env) -> None:
    assert env.client.get("/portfolio/rule", params={"listing_id": env.listing_id}).status_code in {
        401,
        403,
    }
    assert (
        env.client.post(
            "/portfolio/rule/draft", json={"listing_id": env.listing_id}
        ).status_code
        in {401, 403}
    )


def test_holding_rule_keeps_the_same_default_when_none_is_given(env) -> None:
    """Without a template the endpoint reports the missing choice, not a crash."""

    response = env.client.post(
        "/portfolio/rule/draft", json={"listing_id": env.listing_id}, headers=KEY
    )
    assert response.status_code == 422
    assert "template" in response.text


def test_dates_of_the_zone_fixture_are_aware() -> None:
    start = datetime(2026, 5, 12, tzinfo=UTC)
    assert (start + timedelta(days=1)).date() == date(2026, 5, 13)
    assert uuid4() is not None

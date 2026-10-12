"""Shared rule-draft generations (watch-holding-plan 6.2/7.2, WP9).

One current draft request per ``(listing, scope)`` across both origins: the
candidate lifecycle and watchlist entries claim the shared generation at
enqueue, and the store/callback guards re-check it in their own transaction.
The acceptance scenarios of the plan:

* an *old* candidate task starting after a newer watch draft was stored must
  not replace it (audit only);
* both origins queue first and the workers run out of order - the request that
  claimed last stores, whichever worker runs first;
* a shared rule change after the claim (approve/edit/retire) makes the frozen
  request stale too;
* the completion callback never rewrites the association of a superseded run.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from investment_agent.candidates.lifecycle import enqueue_rule_draft
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
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.rules import RuleDraftGenerationRecord, RuleInstanceRecord
from investment_agent.storage.repositories.listing import ListingRepository

TICKER = "600519.SH"
TEMPLATE = "quality_growth"
KEY = {"X-API-Key": "secret"}
NOW = datetime(2026, 9, 28, tzinfo=UTC)
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


def _worker(llm: FakeDraftLLM) -> ResearchWorker:
    return ResearchWorker(
        settings=Settings(),  # type: ignore[call-arg]
        worker_id="generation-test",
        heartbeat_interval_seconds=30.0,
        handlers=[
            RuleDraftHandler(
                settings=Settings(),  # type: ignore[call-arg]
                client_factory=lambda session: llm,
                stock_loader=stub_loader,
            )
        ],
    )


async def _seed_candidate_run() -> tuple[int, int]:
    """An open candidate + its first draft run (claims the generation)."""

    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing(TICKER)
        candidate = CandidateRecord(
            listing_id=listing.id,
            ticker=TICKER,
            status="open",
            opened_at=NOW,
            expires_at=NOW + timedelta(days=14),
            sources=[{"kind": "manual", "as_of": "2026-09-27"}],
            draft_state="none",
        )
        session.add(candidate)
        await session.flush()
        run = await enqueue_rule_draft(
            session,
            candidate,
            template_id=TEMPLATE,
            template_version=1,
            as_of=date(2026, 9, 27),
            screen_result_id=None,
            triggered_by="test",
        )
        return candidate.id, run.id


def _add_watch(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/watchlist",
        json={"ticker": TICKER, "name": "贵州茅台", "industry": "食品饮料", "template_id": TEMPLATE},
        headers=KEY,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _run(run_id: int) -> PipelineRun:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        return run


async def _generation() -> RuleDraftGenerationRecord | None:
    async with session_scope() as session:
        return (
            await session.execute(select(RuleDraftGenerationRecord))
        ).scalar_one_or_none()


async def _rules() -> list[RuleInstanceRecord]:
    async with session_scope() as session:
        return list((await session.execute(select(RuleInstanceRecord))).scalars())


async def _candidate(candidate_id: int) -> CandidateRecord:
    async with session_scope() as session:
        record = await session.get(CandidateRecord, candidate_id)
        assert record is not None
        return record


async def _queued_run(request_id: str) -> int:
    async with session_scope() as session:
        rows = (
            await session.execute(select(PipelineRun).where(PipelineRun.kind == "rule_draft"))
        ).scalars()
        for run in rows:
            if (run.request_json or {}).get("draft_request_id") == request_id:
                return run.id
    raise AssertionError(f"no rule_draft run for request {request_id}")


async def test_old_candidate_task_cannot_replace_a_newer_watch_draft(env) -> None:
    """Acceptance 1: the watch draft is stored *before* the old candidate run."""

    templates = await _sync_templates()
    candidate_id, candidate_run = await _seed_candidate_run()
    claim = await _generation()
    assert claim is not None and claim.current_origin_kind == "candidate"

    body = _add_watch(env)
    request_id = body["rule"]["request"]["id"]
    watch_run = await _queued_run(request_id)
    claim = await _generation()
    assert claim is not None
    assert claim.current_origin_kind == "watchlist" and claim.current_token == request_id
    assert claim.generation == 2

    llm = FakeDraftLLM(templates)
    worker = _worker(llm)
    await worker.run_once(run_id=watch_run)  # the newer request stores first

    stored = await _rules()
    assert len(stored) == 1 and stored[0].status == "draft"
    watch_draft_id = stored[0].id

    await worker.run_once(run_id=candidate_run)  # the old task starts afterwards
    stale = await _run(candidate_run)
    assert stale.status == "partial"
    assert "superseded before store" in (stale.error_summary or "")
    assert [rule.id for rule in await _rules()] == [watch_draft_id]
    assert (await _rules())[0].status == "draft"
    # The candidate shows the shared open draft instead of a stale link.
    candidate = await _candidate(candidate_id)
    assert candidate.draft_state == "ready"
    assert candidate.draft_rule_instance_id == watch_draft_id
    # The watch request is untouched.
    assert env.get("/watchlist", headers=KEY).json()[0]["rule"]["state"] == "ready"


async def test_both_queued_out_of_order_only_the_latest_request_stores(env) -> None:
    """Acceptance 2: workers run in the opposite order of the requests."""

    templates = await _sync_templates()
    candidate_id, candidate_run = await _seed_candidate_run()
    body = _add_watch(env)
    request_id = body["rule"]["request"]["id"]
    watch_run = await _queued_run(request_id)

    # The older candidate worker runs first: it must not take the slot.
    llm = FakeDraftLLM(templates)
    worker = _worker(llm)
    await worker.run_once(run_id=candidate_run)
    stale = await _run(candidate_run)
    assert stale.status == "partial"
    assert "superseded before store" in (stale.error_summary or "")
    assert await _rules() == []
    assert llm.calls == []  # stale pre-flight: no model call at all
    candidate = await _candidate(candidate_id)
    assert candidate.draft_state == "failed"  # nothing shared to adopt yet

    # The newer watch worker then stores normally.
    await worker.run_once(run_id=watch_run)
    stored = await _rules()
    assert len(stored) == 1 and stored[0].draft_rationale
    assert json.loads(stored[0].draft_rationale)["origin_kind"] == "watchlist"
    assert env.get("/watchlist", headers=KEY).json()[0]["rule"]["state"] == "ready"


async def test_newer_candidate_request_supersedes_a_queued_watch_request(env) -> None:
    """The mirror direction: a later candidate claim wins over the watch one."""

    templates = await _sync_templates()
    body = _add_watch(env)
    request_id = body["rule"]["request"]["id"]
    watch_run = await _queued_run(request_id)

    # A candidate nomination for the same listing claims a newer generation.
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing(TICKER)
        candidate = CandidateRecord(
            listing_id=listing.id,
            ticker=TICKER,
            status="open",
            opened_at=NOW,
            expires_at=NOW + timedelta(days=14),
            sources=[],
            draft_state="none",
        )
        session.add(candidate)
        await session.flush()
        candidate_run = (
            await enqueue_rule_draft(
                session,
                candidate,
                template_id=TEMPLATE,
                template_version=1,
                as_of=date(2026, 9, 27),
                screen_result_id=None,
                triggered_by="test",
            )
        ).id
        candidate_id = candidate.id

    llm = FakeDraftLLM(templates)
    worker = _worker(llm)
    await worker.run_once(run_id=watch_run)
    stale = await _run(watch_run)
    assert stale.status == "partial"
    assert "superseded before store" in (stale.error_summary or "")
    assert await _rules() == []
    listed = env.get("/watchlist", headers=KEY).json()[0]["rule"]
    assert listed["state"] == "failed"
    assert "superseded" in listed["request"]["error"]

    # The candidate request is still current and may store.
    await worker.run_once(run_id=candidate_run)
    stored = await _rules()
    assert len(stored) == 1 and json.loads(stored[0].draft_rationale)["origin_kind"] == "candidate"
    candidate = await _candidate(candidate_id)
    assert candidate.draft_state == "ready" and candidate.draft_rule_instance_id == stored[0].id


async def test_shared_rule_change_after_claim_makes_the_run_stale(env) -> None:
    """A direct shared-rule change (edit/approve) is frozen into the claim."""

    from investment_agent.rules.schema import RuleInstance

    templates = await _sync_templates()
    _candidate_id, candidate_run = await _seed_candidate_run()

    async with session_scope() as session:
        newer = await RuleStore(session).create_draft(
            RuleInstance(
                template_ref=f"{TEMPLATE}@1",
                symbol=TICKER,
                scope="candidate",
                params={},
                drafted_by="user",
            )
        )
        newer_id = newer.id

    llm = FakeDraftLLM(templates)
    worker = _worker(llm)
    await worker.run_once(run_id=candidate_run)
    stale = await _run(candidate_run)
    assert stale.status == "partial"
    assert "superseded before store" in (stale.error_summary or "")
    assert llm.calls == []
    rules = await _rules()
    assert [rule.id for rule in rules] == [newer_id]
    assert rules[0].status == "draft"


async def test_retried_enqueue_does_not_reclaim_the_generation(env) -> None:
    """A network retry / repeated screen run keeps its own claim (no bump)."""

    await _sync_templates()
    candidate_id, first_run = await _seed_candidate_run()
    claim = await _generation()
    assert claim is not None and claim.generation == 1

    async with session_scope() as session:
        candidate = await session.get(CandidateRecord, candidate_id)
        assert candidate is not None
        again = await enqueue_rule_draft(
            session,
            candidate,
            template_id=TEMPLATE,
            template_version=1,
            as_of=date(2026, 9, 27),
            screen_result_id=None,
            triggered_by="test",
        )
    assert again.id == first_run
    claim = await _generation()
    assert claim is not None and claim.generation == 1 and claim.current_token
    async with session_scope() as session:
        runs = await session.execute(
            select(func.count()).select_from(PipelineRun).where(PipelineRun.kind == "rule_draft")
        )
        assert runs.scalar_one() == 1


async def test_first_concurrent_claim_adopts_the_winners_row(tmp_path, monkeypatch) -> None:
    """Two first claims race on an empty slot: the loser's insert hits the
    primary key inside its savepoint and adopts the winner's row (P3: the
    first-ever concurrent claim heals instead of erroring)."""

    from investment_agent.rules import generation

    url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    engine = init_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing(TICKER)
        listing_id = listing.id

    real_locked = generation._locked
    raced = {"done": False}

    async def racing_locked(session, listing_id, scope):  # noqa: ANN001
        record = await real_locked(session, listing_id=listing_id, scope=scope)
        if record is None and not raced["done"]:
            # The concurrent claimant's row becomes visible only after our
            # snapshot was taken; the primary-key conflict then hits flush.
            raced["done"] = True
            await session.execute(
                text(
                    "INSERT INTO rule_draft_generation (listing_id, scope, generation) "
                    "VALUES (:listing_id, :scope, 0)"
                ),
                {"listing_id": listing_id, "scope": scope},
            )
            return None
        return record

    monkeypatch.setattr(generation, "_locked", racing_locked)
    try:
        async with session_scope() as session:
            state = await generation.claim_draft_generation(
                session,
                listing_id=listing_id,
                symbol=TICKER,
                scope="candidate",
                token="claim-b",
                origin_kind="candidate",
                origin_id=2,
            )
        assert state.generation == 1 and state.token == "claim-b"
        claim = await _generation()
        assert claim is not None and claim.current_token == "claim-b"
    finally:
        await engine.dispose()


async def test_reservation_lock_keeps_updated_at(tmp_path, monkeypatch) -> None:
    """The SQLite writer reservation is a no-op that must not refresh the
    generation row's audit timestamp."""

    from investment_agent.rules import generation

    url = f"sqlite+aiosqlite:///{tmp_path / 'lock.db'}"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    engine = init_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with session_scope() as session:
            listing = await ListingRepository(session).ensure_a_share_listing(TICKER)
            await generation.claim_draft_generation(
                session,
                listing_id=listing.id,
                symbol=TICKER,
                scope="candidate",
                token="claim-a",
                origin_kind="candidate",
                origin_id=1,
            )
        pinned = datetime(2000, 1, 1)
        async with session_scope() as session:
            await session.execute(
                update(RuleDraftGenerationRecord).values(updated_at=pinned)
            )
        async with session_scope() as session:
            record = await generation._locked(session, listing.id, "candidate")
            assert record is not None
            assert record.updated_at == pinned
    finally:
        await engine.dispose()

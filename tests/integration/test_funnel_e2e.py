"""Offline end-to-end smoke of the whole P1 funnel (P1-F1).

One fresh SQLite database migrated from empty (``ia migrate up``), then the
real job handlers driven through the worker, with fakes only at the provider
edges (:mod:`tests.integration._funnel_support`):

1. ``universe_sync`` + ``industry_sync`` (CSRC + Shenwan 2021) of a ten-stock
   target pool;
2. ``market_snapshot`` / ``financial_snapshot`` / ``dividend_snapshot``, a
   two-year ``snapshot_backfill`` and the AkShare FY summary;
3. ``screen_run`` with the shipped templates: hits whose every reason traces
   back to stored snapshot rows;
4. candidates and their LLM ``rule_draft`` jobs (respx-stubbed LLM);
5. promotion over HTTP with a parameter override: journal, watchlist,
   iFinD ``promote`` request, queued research;
6. the snapshot-mode research run (no iFinD) with an explanation;
7. ``ifind_verify`` against a fake proxy within a small budget;
8. ``GET /health/data``;
9. determinism: a screen rerun is byte-identical, collection reruns add no
   snapshot versions, the decision journal is complete and immutable.

Time is frozen (and moved) so the point-in-time semantics are deterministic:
collection and screen on Wed 2026-09-30 after the close, promotion /
research / verification on 2026-10-01, the health check on Thu 2026-10-08.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from freezegun import freeze_time
from sqlalchemy import func, select, text
from typer.testing import CliRunner

from investment_agent.api.app import create_app
from investment_agent.candidates.lifecycle import CandidatePolicy
from investment_agent.cli.main import app as cli_app
from investment_agent.config import Settings
from investment_agent.factors.view import load_snapshot_view
from investment_agent.providers.akshare_eastmoney import AkshareEastmoneyClient
from investment_agent.providers.akshare_shenwan import AkshareShenwanClient
from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.providers.baostock_reference import BaostockReferenceClient
from investment_agent.research.ifind_chain import run_ifind_research_chain
from investment_agent.research.worker import IfindResearchHandler, ResearchWorker, run_worker
from investment_agent.rules.drafting import RuleDraftHandler
from investment_agent.rules.screen import (
    ScreenConfig,
    required_factors,
    run_screen,
    screen_json,
)
from investment_agent.rules.screen_job import (
    ScreenRunHandler,
    ScreenRunPayload,
    enqueue_screen_run,
    load_members,
    screen_idempotency_key,
)
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.snapshots.backfill import plan_backfill
from investment_agent.snapshots.handlers import SnapshotRuntime, snapshot_job_handlers
from investment_agent.snapshots.jobs import (
    DividendSnapshotPayload,
    FinancialSnapshotPayload,
    IndustrySyncPayload,
    MarketSnapshotPayload,
    SnapshotBackfillPayload,
    SnapshotPayload,
    UniverseSyncPayload,
    enqueue_snapshot_job,
    idempotency_key,
)
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.analysis import PipelineRun, ResearchSnapshot
from investment_agent.storage.models.candidates import (
    CandidateRecord,
    DecisionRecord,
    ImmutableDecisionError,
)
from investment_agent.storage.models.ops import LLMCall
from investment_agent.storage.models.pit_snapshot import (
    DividendEventSnapshot,
    FinancialSnapshot,
    IndustryClassification,
    MarketSnapshot,
)
from investment_agent.storage.models.reference import UniverseMembership
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.models.verification import IfindCallLedger, VerificationRequest
from investment_agent.storage.repositories.universe import UniverseRepository
from investment_agent.verification.job import (
    IfindVerifyHandler,
    VerifyRuntime,
    enqueue_verify_run,
)
from tests.integration._funnel_support import (
    LAST_TRADE_DAY,
    PROFILE_BY_TICKER,
    PROFILES,
    StubLLM,
    SyntheticBaostock,
    SyntheticEastmoney,
    ifind_answers,
    shenwan_history,
)
from tests.unit._shenwan_fake import FakeShenwanBackend
from tests.unit._snapshot_support import FakeProxy

REPO_ROOT = Path(__file__).resolve().parents[2]
LLM_BASE_URL = "https://llm.funnel.test/v1"
AS_OF = date(2026, 9, 30)
FY2025 = date(2025, 12, 31)
UNIVERSE = "CSI300"
T_COLLECT = "2026-09-30T10:00:00Z"  # Wed 18:00 Asia/Shanghai, after the close
T_PROMOTE = "2026-10-01T02:00:00Z"  # Thu 10:00 (holiday; the user reviews)
T_HEALTH = "2026-10-08T02:00:00Z"  # Thu 10:00, first trading day after the holiday
API_KEY = "funnel-smoke-api-key-7f3a9c1e5b"
KEY = {"X-API-Key": API_KEY}
SNAPSHOT_REF = re.compile(r"\b(market_snapshot|financial_snapshot|dividend_event):(\d+)\b")


async def _no_sleep(_: float) -> None:
    return None


class Funnel:
    """Wires the real handlers to the synthetic provider edges."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.baostock = SyntheticBaostock()
        self.eastmoney = SyntheticEastmoney()
        self.proxy = FakeProxy()
        runtime = SnapshotRuntime(
            settings=settings,
            limiter=AsyncRateLimiter(6000, burst=1000),
            proxy_factory=lambda: self.proxy,
            reference_factory=lambda: BaostockReferenceClient(
                self.baostock, retry_backoff_seconds=0
            ),
            baostock_factory=self._market_client,
            backfill_factory=self._market_client,
            dividend_factory=self._market_client,
            shenwan_factory=lambda: AkshareShenwanClient(
                FakeShenwanBackend(history=shenwan_history()),
                retry_backoff_seconds=0,
                constituent_pause_seconds=0,
                sleep=_no_sleep,
            ),
            eastmoney_factory=lambda: AkshareEastmoneyClient(
                self.eastmoney,
                min_interval_seconds=0,
                sleep=_no_sleep,
                retry_backoff_seconds=0,
            ),
            sleep=_no_sleep,
            batch_backoff_seconds=0,
        )
        verify_runtime = VerifyRuntime(
            settings=settings,
            proxy_factory=lambda: self.proxy,
            limiter=AsyncRateLimiter(6000, burst=1000),
            sleep=_no_sleep,
            batch_backoff_seconds=0,
        )
        self.worker = ResearchWorker(
            settings=settings,
            worker_id="e2e-worker",
            heartbeat_interval_seconds=30.0,
            handlers=[
                IfindResearchHandler(settings=settings, runner=run_ifind_research_chain),
                *snapshot_job_handlers(settings, runtime),
                ScreenRunHandler(candidate_policy=CandidatePolicy.from_settings(settings)),
                RuleDraftHandler(settings=settings),
                IfindVerifyHandler(settings, verify_runtime),
            ],
        )

    def _market_client(self) -> BaostockMarketClient:
        return BaostockMarketClient(self.baostock, retry_backoff_seconds=0)

    async def drain(self, *, limit: int = 200) -> int:
        """Run queued jobs until none is left (the ``ia worker start --once`` loop)."""

        executed = 0
        while await _count(PipelineRun, PipelineRun.status == "queued"):
            assert executed < limit, "queue did not drain"
            await run_worker(
                settings=self.settings, worker_id="e2e-worker", once=True, worker=self.worker
            )
            executed += 1
        return executed

    async def collect(self, payload: SnapshotPayload, *, rerun: str | None = None) -> PipelineRun:
        key = idempotency_key(payload) + (f":rerun-{rerun}" if rerun else "")
        async with session_scope() as session:
            run, created = await enqueue_snapshot_job(
                session, payload, triggered_by="e2e", key=key
            )
            run_id = run.id
        assert created, key
        await self.worker.run_once(run_id=run_id)
        return await _run(run_id)


async def _run(run_id: int) -> PipelineRun:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        return run


async def _count(model: Any, *where: Any) -> int:
    async with session_scope() as session:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return int((await session.execute(stmt)).scalar_one())


async def _snapshot_counts() -> dict[str, int]:
    return {
        "market_snapshot": await _count(MarketSnapshot),
        "financial_snapshot": await _count(FinancialSnapshot),
        "dividend_event": await _count(DividendEventSnapshot),
        "industry_classification": await _count(IndustryClassification),
        "universe_membership": await _count(UniverseMembership),
    }


def _assert_success(run: PipelineRun) -> None:
    assert run.status == "success", (run.kind, run.status, run.error_summary, run.metadata_json)


@pytest.fixture
def funnel_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'funnel.db'}"
    for name, value in {
        "DATABASE_URL": db_url,
        "DATA_DIR": str(tmp_path / "data"),
        "API_KEY": API_KEY,
        "LLM_TIER3_API_KEY": "stub-key",
        "LLM_TIER3_BASE_URL": LLM_BASE_URL,
        "LLM_TIER3_MODEL": "stub-model",
        "IFIND_PROXY_URL": "http://ifind-proxy.invalid",
        "IFIND_PROXY_API_KEY": "stub-proxy-key",
        "IFIND_BUDGET_PER_5H": "12",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(REPO_ROOT)
    # Step 0: the documented migration path into an empty database.
    migrated = CliRunner().invoke(cli_app, ["migrate", "up"])
    assert migrated.exit_code == 0, migrated.output
    return Settings()  # type: ignore[call-arg]


async def test_offline_funnel_end_to_end(funnel_env: Settings) -> None:
    settings = funnel_env
    templates = {template.id: template for _, template in load_templates()}
    llm = StubLLM(templates)
    async with engine_lifespan(settings.database_url):
        async with session_scope() as session:
            head = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
        assert head == "045", head
        # Build the app before freezing time: FastAPI resolves the routes'
        # ``datetime`` annotations when they are registered.
        api = TestClient(create_app())
        with (
            freeze_time(T_COLLECT, tick=True, real_asyncio=True) as clock,
            respx.mock(assert_all_called=False) as router,
        ):
            router.post(f"{LLM_BASE_URL}/chat/completions").mock(side_effect=llm)
            funnel = Funnel(settings)

            await _collect(funnel)
            screen_run_id, screen_text = await _screen(funnel)
            candidates = await _drafts(funnel, llm)

            clock.move_to(T_PROMOTE)
            research_run_id = await _promote(api, candidates[PROMOTED])
            await _research(funnel, research_run_id, llm)
            await _verify(funnel)

            clock.move_to(T_HEALTH)
            _health(api)
            await _determinism(funnel, screen_run_id, screen_text)


PROMOTED = "600900.SH"
EXPECTED_HITS = {
    ("dividend_blue_chip", "600900.SH"),
    ("dividend_blue_chip", "601398.SH"),
    ("quality_growth", "300750.SZ"),
    ("quality_growth", "600519.SH"),
    ("steady_quality_growth", "600519.SH"),
}
JOURNAL = ["nominate", "edit_rule", "approve_rule", "promote"]
BACKFILL = SnapshotBackfillPayload(
    universe_code=UNIVERSE, as_of=AS_OF, part="membership", price_years=2, annual_years=4,
    quarters=2,
)
AKSHARE_FY2025 = FinancialSnapshotPayload(
    provider="akshare", report_period=date(2025, 12, 31), as_of=AS_OF, universe_code=UNIVERSE
)
LIVE_COLLECTIONS: tuple[SnapshotPayload, ...] = (
    UniverseSyncPayload(universe_code=UNIVERSE, as_of=AS_OF),
    IndustrySyncPayload(as_of=AS_OF, universe_codes=(UNIVERSE,)),
    MarketSnapshotPayload(trade_date=AS_OF, universe_code=UNIVERSE),
    FinancialSnapshotPayload(report_period=date(2026, 6, 30), as_of=AS_OF, universe_code=UNIVERSE),
    DividendSnapshotPayload(as_of=AS_OF, universe_code=UNIVERSE),
    AKSHARE_FY2025,
)


# --------------------------------------------------------------------------- #
# Steps 1-2: collection
# --------------------------------------------------------------------------- #


async def _collect(funnel: Funnel) -> None:
    # Step 1: target pool and industries.
    for payload in LIVE_COLLECTIONS[:2]:
        _assert_success(await funnel.collect(payload))
    async with session_scope() as session:
        members = await UniverseRepository(session).members_as_of(UNIVERSE, AS_OF)
    assert sorted(m.ticker for m in members) == sorted(p.ticker for p in PROFILES)
    for system in ("csrc", "sw2021"):
        assert await _count(IndustryClassification, IndustryClassification.system == system) == 10

    # Step 2: daily bars, financials, dividends, the history backfill, AkShare.
    for payload in LIVE_COLLECTIONS[2:5]:
        _assert_success(await funnel.collect(payload))
    async with session_scope() as session:
        await plan_backfill(session, BACKFILL, triggered_by="e2e")
    await funnel.drain()
    async with session_scope() as session:
        backfill = list(
            (
                await session.execute(
                    select(PipelineRun).where(PipelineRun.kind == "snapshot_backfill")
                )
            ).scalars()
        )
    assert {run.request_json["part"] for run in backfill} == {
        "membership", "industry", "prices", "financials", "dividends",
    }
    for run in backfill:
        _assert_success(run)
    _assert_success(await funnel.collect(AKSHARE_FY2025))

    # Two years of raw + backward bars (the IPO since its listing), four annual
    # and two periodic reports per member, AkShare FY2025 summaries, dividends.
    assert await _count(MarketSnapshot, MarketSnapshot.adjust == "none") > 9 * 500
    assert await _count(FinancialSnapshot, FinancialSnapshot.provider == "akshare") == len(PROFILES)
    async with session_scope() as session:
        periods = set(
            (
                await session.execute(
                    select(FinancialSnapshot.report_period).where(
                        FinancialSnapshot.provider == "baostock"
                    )
                )
            ).scalars()
        )
    expected = {date(year, 12, 31) for year in range(2022, 2026)}
    assert expected | {date(2026, 3, 31), date(2026, 6, 30)} <= periods
    assert await _count(DividendEventSnapshot) > 0


# --------------------------------------------------------------------------- #
# Step 3: screen
# --------------------------------------------------------------------------- #


def _canonical_rows(rows: list[ScreenResultRecord]) -> str:
    return json.dumps(
        [
            {
                "template": f"{row.template_id}@{row.template_version}",
                "ticker": row.ticker,
                "outcome": row.outcome,
                "hit": row.hit,
                "rank": row.rank,
                "strength": row.strength,
                "reasons": row.reasons,
                "context": row.context,
            }
            for row in sorted(rows, key=lambda r: (r.template_id, r.ticker))
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


async def _screen_rows(run_id: int) -> list[ScreenResultRecord]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(ScreenResultRecord).where(ScreenResultRecord.screen_run_id == run_id)
                )
            ).scalars()
        )


async def _screen_text() -> str:
    """``screen_json`` recomputed from the snapshot store as of ``AS_OF``."""

    async with session_scope() as session:
        stored = await RuleStore(session).latest_templates()
        config = ScreenConfig()
        loaded = await load_members(
            session, UNIVERSE, AS_OF, sorted(required_factors(stored, config))
        )
    assert not loaded.errors, loaded.errors
    return screen_json(
        run_screen(as_of=AS_OF, members=loaded.members, templates=stored, config=config)
    )


async def _market_row_on(session: Any, listing_id: int, day: str, adjust: str) -> bool:
    row = (
        await session.execute(
            select(MarketSnapshot.id).where(
                MarketSnapshot.listing_id == listing_id,
                MarketSnapshot.trade_date == date.fromisoformat(day),
                MarketSnapshot.adjust == adjust,
            )
        )
    ).first()
    return row is not None


async def _resolve_reason(session: Any, listing_id: int, ref: Any) -> list[str]:
    """Snapshot rows a reason's ``snapshot_ref`` points at (row refs, dated bars)."""

    resolved: list[str] = []
    models: dict[str, Any] = {
        "market_snapshot": MarketSnapshot,
        "financial_snapshot": FinancialSnapshot,
        "dividend_event": DividendEventSnapshot,
    }
    for table, row_id in SNAPSHOT_REF.findall(json.dumps(ref)):
        row = await session.get(models[table], int(row_id))
        assert row is not None, f"{table}:{row_id} does not exist"
        assert row.listing_id == listing_id, f"{table}:{row_id} belongs to another listing"
        resolved.append(f"{table}:{row_id}")
    if isinstance(ref, dict):
        # Percentiles and one-year statistics cite dated bars of one series.
        if "column" in ref and "current_date" in ref:
            assert await _market_row_on(session, listing_id, ref["current_date"], "none")
            resolved.append(f"market_snapshot@{ref['current_date']}")
        if "series" in ref and ref.get("last"):
            for day in (ref["first"], ref["last"]):
                assert await _market_row_on(session, listing_id, day, ref["series"])
                resolved.append(f"market_snapshot[{ref['series']}]@{day}")
    return resolved


async def _screen(funnel: Funnel) -> tuple[int, str]:
    payload = ScreenRunPayload(universe_code=UNIVERSE, as_of=AS_OF)
    async with session_scope() as session:
        run, created = await enqueue_screen_run(session, payload, triggered_by="e2e")
        run_id = run.id
    assert created and run.idempotency_key == screen_idempotency_key(payload)
    await funnel.worker.run_once(run_id=run_id)
    run = await _run(run_id)
    _assert_success(run)
    assert run.metadata_json["member_count"] == len(PROFILES)
    assert run.metadata_json["load_errors"] == {}
    rows = await _screen_rows(run_id)
    hits = {(row.template_id, row.ticker) for row in rows if row.hit}
    assert hits == EXPECTED_HITS, hits
    async with session_scope() as session:
        for row in rows:
            if not row.hit:
                continue
            assert row.reasons, (row.template_id, row.ticker)
            for reason in row.reasons:
                ref = reason["snapshot_ref"]
                assert ref, (row.ticker, reason["factor"])
                resolved = await _resolve_reason(session, row.listing_id, ref)
                assert resolved, (row.ticker, reason["factor"], ref)
    return run_id, await _screen_text()


# --------------------------------------------------------------------------- #
# Step 4: candidates and LLM rule drafts
# --------------------------------------------------------------------------- #


async def _candidates() -> dict[str, CandidateRecord]:
    async with session_scope() as session:
        rows = (await session.execute(select(CandidateRecord))).scalars()
        return {row.ticker: row for row in rows}


async def _drafts(funnel: Funnel, llm: StubLLM) -> dict[str, int]:
    opened = await _candidates()
    assert set(opened) == {ticker for _, ticker in EXPECTED_HITS}
    assert {row.status for row in opened.values()} == {"open"}
    assert {row.draft_state for row in opened.values()} == {"pending"}
    assert await _count(PipelineRun, PipelineRun.kind == "rule_draft") == len(opened)
    await funnel.drain()
    assert llm.requests.count("rule_draft") == len(opened)
    async with session_scope() as session:
        drafts = list(
            (
                await session.execute(select(PipelineRun).where(PipelineRun.kind == "rule_draft"))
            ).scalars()
        )
    for run in drafts:
        _assert_success(run)
    ready = await _candidates()
    for row in ready.values():
        assert row.draft_state == "ready", (row.ticker, row.draft_state)
        assert row.draft_rule_instance_id is not None
    assert await _count(LLMCall) == len(opened)
    return {ticker: row.id for ticker, row in ready.items()}


# --------------------------------------------------------------------------- #
# Step 5: promotion over HTTP
# --------------------------------------------------------------------------- #


async def _journal(ticker: str) -> list[DecisionRecord]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(DecisionRecord)
                    .where(DecisionRecord.ticker == ticker)
                    .order_by(DecisionRecord.id)
                )
            ).scalars()
        )


async def _promote(api: TestClient, candidate_id: int) -> int:
    response = api.post(
        f"/candidates/{candidate_id}/promote",
        headers=KEY,
        json={
            "params_override": {"dividend_yield_min": 0.035},
            "reason": "股息率门槛提高到 3.5%（e2e）",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [d["kind"] for d in body["decisions"]] == JOURNAL[1:]
    assert body["rule"]["status"] == "active"
    assert body["rule"]["params"]["dividend_yield_min"] == 0.035
    assert body["watchlist"]["industry"] == "公用事业"
    assert body["watchlist"]["industry_source"] == "sw2021_l1"
    assert body["watchlist"]["report_year"] == 2025
    assert body["verification_requested"] is True
    assert body["warnings"] == []
    research_run_id = body["research_run_id"]
    assert isinstance(research_run_id, int)

    journal = await _journal(PROMOTED)
    assert [d.kind for d in journal] == JOURNAL
    assert journal[0].actor == "system" and journal[-1].actor == "user"
    async with session_scope() as session:
        item = (await session.execute(select(WatchlistItem))).scalar_one()
        assert (item.provider, item.enabled, item.industry) == ("auto", True, "公用事业")
        requests = list((await session.execute(select(VerificationRequest))).scalars())
        assert [(r.ticker, r.trigger, r.status) for r in requests] == [
            (PROMOTED, "promote", "pending")
        ]
        run = await session.get(PipelineRun, research_run_id)
        assert run is not None and run.status == "queued"
        assert run.request_json["provider"] == "snapshot"
        candidate = await session.get(CandidateRecord, candidate_id)
        assert candidate is not None and candidate.status == "promoted"
    return research_run_id


# --------------------------------------------------------------------------- #
# Step 6: snapshot-mode research
# --------------------------------------------------------------------------- #


def _read_json(path: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload


async def _research(funnel: Funnel, run_id: int, llm: StubLLM) -> None:
    await funnel.drain()
    run = await _run(run_id)
    _assert_success(run)
    assert run.metadata_json["source_mode"] == "snapshot"
    assert llm.requests.count("explanation") == 1
    assert funnel.proxy.calls == [], "snapshot mode must not call iFinD"
    async with session_scope() as session:
        snapshot = (
            await session.execute(
                select(ResearchSnapshot).where(ResearchSnapshot.pipeline_run_id == run_id)
            )
        ).scalar_one()
    assert snapshot.score_json["source_mode"] == "snapshot"
    assert snapshot.explanation_json is not None
    artifacts = {
        name: _read_json(str(snapshot.artifact_manifest[name]))
        for name in ("pool", "market", "financial")
    }
    (stock,) = artifacts["pool"]["items"]
    assert stock["industry_group"] == "utility"
    # The promotion's Shenwan level-1 text restates the data classification.
    assert stock["field_sources"]["industry"]["provider"] == "sw2021"
    for name, field in (("market", "pe"), ("market", "dividend_yield"), ("financial", "roe")):
        (item,) = artifacts[name]["items"]
        source = item["field_sources"][field]
        assert source["provider"] == "baostock", (field, source)
        assert source["source"].startswith("snapshot_view:"), (field, source)
        assert source["verification"] == "unverified", (field, source)
    (financial,) = artifacts["financial"]["items"]
    assert financial["field_sources"]["revenue"]["provider"] == "akshare"
    # No free source has capital expenditure: one A13 ``missing`` request.
    assert run.metadata_json["verification_requested_fields"] == [
        "capital_expenditure",
        "free_cash_flow",
    ]


# --------------------------------------------------------------------------- #
# Step 7: iFinD verification
# --------------------------------------------------------------------------- #


async def _verify(funnel: Funnel) -> None:
    profile = PROFILE_BY_TICKER[PROMOTED]
    # iFinD agrees with the free sources except ROE (20% vs baostock 16%).
    ifind_answers(funnel.proxy, profile, FY2025, roe_override=0.20)
    async with session_scope() as session:
        queued, _ = await enqueue_verify_run(
            session, slot="e2e-1", triggered_by="e2e", max_requests=5
        )
        run_id = queued.id
    await funnel.worker.run_once(run_id=run_id)
    run = await _run(run_id)
    _assert_success(run)
    calls = await _count(IfindCallLedger)
    assert 0 < calls <= funnel.settings.ifind_budget_per_5h
    assert len(funnel.proxy.calls) == calls
    async with session_scope() as session:
        requests = list((await session.execute(select(VerificationRequest))).scalars())
        # The promote request covers every key field: it absorbs the open
        # ``missing`` request of the research run.
        assert {(r.trigger, r.status) for r in requests} == {
            ("promote", "done"), ("missing", "superseded")
        }
        assert await _count(FinancialSnapshot, FinancialSnapshot.provider == "ifind") > 0
        # View precedence (P1-A12): iFinD-verified values win, a conflict takes
        # the iFinD value; the screen as of AS_OF does not see them (captured later).
        view = await load_snapshot_view(session, PROMOTED, date(2026, 10, 8))
        before = await load_snapshot_view(session, PROMOTED, AS_OF)
    latest = view.latest_annual()
    assert latest is not None and latest.report_period == FY2025
    roe = latest.source("roe")
    assert roe is not None and roe.verification == "conflict"
    assert latest.value("roe") == pytest.approx(0.20)
    revenue = latest.source("revenue_total")
    assert revenue is not None and revenue.verification == "verified"
    assert latest.value("capital_expenditure") is not None
    old = before.latest_annual()
    assert old is not None and old.source("roe").verification == "unverified"  # type: ignore[union-attr]
    assert old.value("roe") == pytest.approx(profile.roe_of(2025))


# --------------------------------------------------------------------------- #
# Step 8: data health
# --------------------------------------------------------------------------- #


def _health(api: TestClient) -> None:
    response = api.get("/health/data", headers=KEY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["as_of"] == "2026-10-08"
    assert body["target_universe"] == UNIVERSE
    # The only warning is the ROE conflict the verification found.
    assert not [w for w in body["warnings"] if w["level"] == "error"], body["warnings"]
    assert {w["code"] for w in body["warnings"]} == {"provider_conflict"}
    accumulation = body["accumulation"]
    assert accumulation["streak_weeks"] == 1  # W40 qualified; W41 still in progress
    assert accumulation["latest_live_trade_date"] == AS_OF.isoformat()
    market = body["freshness"]["market"]
    assert (market["members_at_latest"], market["lag_trading_days"]) == (len(PROFILES), 0)
    # The universe sync refreshed the stock directory (D4) from the last trade day.
    directory = body["freshness"]["directory"]
    assert (directory["synced_on"], directory["total"]) == (LAST_TRADE_DAY.isoformat(), len(PROFILES))
    collections = {item["key"]: item for item in body["collections"]}
    assert {
        "universe_sync:baostock:CSI300",
        "industry_sync:baostock+akshare:csrc+sw2021:CSI300",
        "market_snapshot:baostock:CSI300",
        "financial_snapshot:baostock:CSI300",
        "financial_snapshot:akshare:summary:CSI300",
    } <= set(collections), sorted(collections)
    for key, item in collections.items():
        assert item["last_run"]["status"] == "success", (key, item["last_run"])
    for part in body["backfill"]["parts"]:
        assert part["done"] == part["total"] == len(PROFILES), part
    assert body["verification"]["conflicts_total"] == 1
    assert body["verification"]["conflicts"][0]["fields"] == ["roe"]
    assert body["overdue"]["open"] == 0


# --------------------------------------------------------------------------- #
# Step 9: determinism and idempotency
# --------------------------------------------------------------------------- #


def _stable_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    volatile = {"timing_seconds", "candidates", "template_sync"}
    return {key: value for key, value in metadata.items() if key not in volatile}


async def _determinism(funnel: Funnel, screen_run_id: int, screen_text: str) -> None:
    decisions = await _count(DecisionRecord)
    journal = [(d.id, d.kind, d.at, d.context) for d in await _journal(PROMOTED)]
    counts = await _snapshot_counts()

    # Re-running every live collection (new run keys) stores no new snapshot
    # versions: unchanged captures are skipped.
    for index, payload in enumerate(LIVE_COLLECTIONS):
        _assert_success(await funnel.collect(payload, rerun=str(index)))
    assert await _snapshot_counts() == counts

    # Re-running the screen for the same as_of (``ia screen run --rerun``) is
    # byte-identical, although verified iFinD values were written meanwhile
    # (captured after AS_OF, so invisible to it).
    payload = ScreenRunPayload(universe_code=UNIVERSE, as_of=AS_OF)
    async with session_scope() as session:
        rerun, created = await enqueue_screen_run(
            session, payload, triggered_by="e2e", key=f"{screen_idempotency_key(payload)}:rerun"
        )
        rerun_id = rerun.id
    assert created
    await funnel.worker.run_once(run_id=rerun_id)
    first, second = await _run(screen_run_id), await _run(rerun_id)
    _assert_success(second)
    assert _canonical_rows(await _screen_rows(rerun_id)) == _canonical_rows(
        await _screen_rows(screen_run_id)
    )
    assert _stable_metadata(second.metadata_json) == _stable_metadata(first.metadata_json)
    assert await _screen_text() == screen_text

    # The decision journal is complete and append-only: the rerun merged into
    # the open candidates without new decisions, and rows refuse changes.
    assert await _count(DecisionRecord) == decisions
    assert [(d.id, d.kind, d.at, d.context) for d in await _journal(PROMOTED)] == journal
    async with session_scope() as session:
        kinds = (await session.execute(select(DecisionRecord.kind))).scalars().all()
        assert sorted(kinds) == sorted(
            ["nominate"] * len({ticker for _, ticker in EXPECTED_HITS}) + JOURNAL[1:]
        )
        decision = await session.get(DecisionRecord, journal[-1][0])
        assert decision is not None
        decision.reason_text = "rewritten"
        with pytest.raises(ImmutableDecisionError):
            await session.flush()
        await session.rollback()
        decision = await session.get(DecisionRecord, journal[0][0])
        assert decision is not None
        await session.delete(decision)
        with pytest.raises(ImmutableDecisionError):
            await session.flush()
        await session.rollback()
    assert [(d.id, d.kind, d.at, d.context) for d in await _journal(PROMOTED)] == journal

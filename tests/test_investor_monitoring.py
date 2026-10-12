"""AG15 committed SQLite source receipts, native review and in-app delivery."""
import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select

from investment_agent.agent.executor import AgentExecutor
from investment_agent.agent.research import ResearchOutput
from investment_agent.agent.tasks import TaskService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import TextNode
from investment_agent.investor.controls import CaseAction
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.investor.monitoring import MonitoringService
from investment_agent.investor.publication import PublicationService
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.monitoring import (
    InvestorNotification,
    MonitorReceipt,
    ReviewCycle,
)
from investment_agent.storage.models.publication import PublicationOutbox
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from tests.support.conversations import base
from tests.support.investor import case_ref, execute, full_progress, saved  # noqa: F401
from tests.support.publication import fixture, publish, unchanged_review
from tests.support.publication import publication_db as publication_db


async def stats():
    async with session_scope() as session:
        return tuple([await session.scalar(select(func.count()).select_from(model)) for model in (MonitorReceipt, ReviewCycle, InvestorNotification, AgentTask)])


@pytest.mark.asyncio
async def test_repeat_scan_calendar_and_restriction_are_not_self_triggering(publication_db):
    candidate, _, _, result = await saved()
    async with session_scope() as session:
        await PublicationService(session).restrict(result.recommendation_ref, reason="critical_counter_evidence:formal_review_required")
        await MonitoringService(session, base()).maintain()
    initial = await stats()
    for _ in range(5):
        async with session_scope() as session:
            await MonitoringService(session, base()).maintain()
    assert await stats() == initial
    async with session_scope() as session:
        cycle = await session.scalar(select(ReviewCycle))
        assert cycle.state == "blocked" and "background_disabled" in cycle.payload["pending_reason"]
        assert len(cycle.payload["events"]) == 1
        assert len(await MonitoringService(session, base()).notifications()) == 2  # independent original publication + change
        assert (await PublicationService(session).get_recommendation(result.recommendation_ref)).status == "review_required"
        assert (await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)).current_recommendation_ref == result.recommendation_ref


@pytest.mark.asyncio
async def test_calendar_exact_boundary_and_no_daily_replay(publication_db):
    candidate, _, _, result = await saved()
    async with session_scope() as session:
        view = await PublicationService(session).get_recommendation(result.recommendation_ref)
        due = view.effective_next_review_at
        await MonitoringService(session, base()).scan(now=due - timedelta(microseconds=1))
        assert await session.scalar(select(func.count()).select_from(ReviewCycle)) == 0
    async with session_scope() as session:
        await MonitoringService(session, base()).scan(now=due)
        assert await session.scalar(select(func.count()).select_from(ReviewCycle)) == 1
        assert await session.scalar(select(func.count()).select_from(InvestorNotification)) == 0
    initial = await stats()
    async with session_scope() as session:
        await MonitoringService(session, base()).scan(now=due + timedelta(days=1))
    assert await stats() == initial


@pytest.mark.asyncio
async def test_actual_snapshot_batches_coalesce_and_old_unrelated_history_is_quiet(publication_db):
    candidate, _, subject, _ = await saved()
    async with session_scope() as session:
        repository = FinancialSnapshotRepository(session)
        for day in range(30):
            await repository.insert(listing_id=subject.id, report_period=date(2010, 12, 31), period_kind="annual", fields={"net_profit": str(day)}, provider="fixture", captured_at=datetime.now(UTC), availability="actual", available_from=date(2011, 4, 1), is_backfill=True)
    async with session_scope() as session:
        await MonitoringService(session, base()).scan()
        receipts = (await session.scalars(select(MonitorReceipt))).all()
        assert len(receipts) == 2 and not any(r.payload.get("changed") for r in receipts)
        assert await session.scalar(select(func.count()).select_from(ReviewCycle)) == 0
    before = await stats()
    async with session_scope() as session:
        await MonitoringService(session, base()).scan()
    assert await stats() == before


@pytest.mark.asyncio
async def test_two_file_sessions_claim_one_cycle_and_manual_receipt(publication_db):
    candidate, _, _, _ = await saved()
    key = uuid4()
    async def request():
        async with session_scope() as session:
            return await MonitoringService(session, base()).manual(candidate.thesis.case_ref.id, key, expected_revision=2)
    first, second = await asyncio.gather(request(), request())
    assert first == second and first["task_id"]
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(ReviewCycle)) == 1
        with pytest.raises(InvestorError, match="different revision"):
            await MonitoringService(session, base()).manual(candidate.thesis.case_ref.id, key, expected_revision=3)


@pytest.mark.asyncio
async def test_pause_background_manual_still_authorized(publication_db):
    candidate, _, _, _ = await saved()
    async with session_scope() as session:
        await execute(session, CaseAction(case_ref=await case_ref(candidate), action="pause", reason="User paused background"))
    async with session_scope() as session:
        case = await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)
        receipt = await MonitoringService(session, base()).manual(case.id, uuid4(), expected_revision=case.revision)
        assert receipt["state"] == "running"


@pytest.mark.asyncio
async def test_outbox_delivery_checks_current_mute_and_consumes_without_unmute_replay(publication_db):
    candidate, _, _, _ = await saved()
    async with session_scope() as session:
        await execute(session, CaseAction(case_ref=await case_ref(candidate), action="mute", reason="No reminders"))
        await MonitoringService(session, base()).consume_outbox()
        assert await session.scalar(select(func.count()).select_from(InvestorNotification)) == 0
        assert (await session.scalar(select(PublicationOutbox))).state == "consumed"
    async with session_scope() as session:
        await execute(session, CaseAction(case_ref=await case_ref(candidate), action="unmute", reason="Enable future reminders"))
        await MonitoringService(session, base()).consume_outbox()
        assert await session.scalar(select(func.count()).select_from(InvestorNotification)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_question", [False, True])
async def test_default_worker_native_maintain_extend_cycle_completed_quiet(publication_db, monkeypatch, pending_question):
    candidate, evidence, subject, result = await saved()
    selected = base()
    async with session_scope() as session:
        receipt = await MonitoringService(session, selected).manual(candidate.thesis.case_ref.id, uuid4(), expected_revision=2)
    if pending_question:
        async with session_scope() as session:
            await MonitoringService(session, selected).manual(candidate.thesis.case_ref.id, uuid4(), expected_revision=2, reason="Recheck the inference even on unchanged materials")
    calls = []
    def respond(messages, info):
        returns = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
        calls.append(len(returns))
        if not returns:
            return ModelResponse([ToolCallPart("get_evidence", {"ref": evidence.model_dump(mode="json"), "subject_ref": subject.model_dump(mode="json"), "as_of": receipt["started_at"]})], usage=RequestUsage(input_tokens=10, output_tokens=10))
        review = unchanged_review(result.recommendation_ref, (evidence,))
        output = ResearchOutput(progress=full_progress(candidate, evidence), review=review, extend=True)
        return ModelResponse([TextPart(output.model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=10))
    original = AgentExecutor.__init__
    def constructor(self, *args, **kwargs):
        kwargs["model"] = FunctionModel(respond)
        original(self, *args, **kwargs)
    monkeypatch.setattr(AgentExecutor, "__init__", constructor)
    # Default handlers and maintenance: only model transport is offline.
    worker = ResearchWorker(settings=selected, worker_id="native-review")
    assert await worker.run_once() is not None
    await worker.run_once()
    if pending_question:
        await worker.run_once()
    async with session_scope() as session:
        cycle = await session.get(ReviewCycle, receipt["id"])
        task = await TaskService(session).get_task(UUID(receipt["task_id"]))
        assert task.status == "succeeded" and cycle.state == "completed", task.model_dump()
        assert any(r.kind == "review" for r in task.artifact_refs)
        view = await PublicationService(session).get_recommendation(result.recommendation_ref)
        assert view.effective_next_review_at > view.recommendation.next_review_at
        assert view.effective_valid_until > view.recommendation.valid_until
        notes = await MonitoringService(session, selected).notifications()
        assert len(notes) == 1 and notes[0]["stage"] == "published"
        assert calls == ([0, 1, 0, 1] if pending_question else [0, 1])
        cycles = (await session.scalars(select(ReviewCycle))).all()
        assert len(cycles) == (2 if pending_question else 1) and all(c.state == "completed" for c in cycles)
    before = await stats()
    await ResearchWorker(settings=selected, worker_id="restart").run_once()
    assert await stats() == before


@pytest.mark.asyncio
async def test_dirty_merge_during_running_and_failed_stops_no_paid_retry(publication_db):
    candidate, _, _, result = await saved()
    async with session_scope() as session:
        receipt = await MonitoringService(session, base()).manual(candidate.thesis.case_ref.id, uuid4(), expected_revision=2)
        await PublicationService(session).restrict(result.recommendation_ref, reason="critical_counter_evidence:formal_review_required")
    async with session_scope() as session:
        await MonitoringService(session, base()).scan()
        cycle = await session.get(ReviewCycle, receipt["id"])
        assert len(cycle.payload["dirty"]) == 1
    # Real default worker failure: disabled live feature stops before model I/O.
    await ResearchWorker(settings=base(investor_agent_enabled=False), worker_id="disabled").run_once()
    async with session_scope() as session:
        await MonitoringService(session, base()).maintain()
        cycle = await session.get(ReviewCycle, receipt["id"])
        assert cycle.state == "stopped" and cycle.payload["pending_reason"]
    before = await stats()
    for _ in range(3):
        async with session_scope() as session:
            await MonitoringService(session, base()).maintain()
    assert await stats() == before


@pytest.mark.asyncio
async def test_notification_cursor_read_cas_and_atomic_concurrent_consumption(publication_db):
    await saved()
    async def consume():
        async with session_scope() as session:
            return await MonitoringService(session, base()).consume_outbox()
    await asyncio.gather(consume(), consume())
    async with session_scope() as session:
        svc = MonitoringService(session, base())
        notes = await svc.notifications(limit=1)
        assert len(notes) == 1
        note = notes[0]
        assert await svc.notifications(after=note["sequence"]) == []
        read = await svc.read(note["sequence"], expected_revision=1, read=True)
        assert read["revision"] == 2 and not await svc.notifications(unread=True)
        assert await svc.read(note["sequence"], expected_revision=1, read=True) == read
        with pytest.raises(InvestorError, match="changed"):
            await svc.read(note["sequence"], expected_revision=1, read=False)


@pytest.mark.asyncio
async def test_authenticated_api_csrf_manual_and_notification_reads(publication_db):
    import httpx

    from investment_agent.api.app import create_app
    from investment_agent.web.session import SESSION_COOKIE_NAME, create_session_token
    candidate, _, _, _ = await saved()
    selected = base()
    app = create_app(settings=selected)
    from investment_agent.api.auth import get_bootstrap_settings
    app.dependency_overrides[get_bootstrap_settings] = lambda: selected
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        path = f"/investor/cases/{candidate.thesis.case_ref.id}/reviews"
        body = {"mutation": {"expected_revision": 2, "idempotency_key": str(uuid4())}}
        assert (await client.post(path, json=body)).status_code == 401
        client.cookies.set(SESSION_COOKIE_NAME, create_session_token(api_key=selected.api_key, ttl_minutes=15))
        assert (await client.post(path, json=body, headers={"Origin": "https://evil.example"})).status_code == 403
        response = await client.post(path, json=body, headers={"Origin": "http://testserver"})
        assert response.status_code == 202, response.text
        assert response.json()["task_id"]
        assert (await client.post(path, json=body, headers={"Origin": "http://testserver"})).json() == response.json()
        assert len((await client.get(path)).json()) == 1
        async with session_scope() as session:
            await MonitoringService(session, selected).consume_outbox()
        notes = (await client.get("/investor/notifications?unread=true&limit=1")).json()
        assert len(notes) == 1
        note = notes[0]
        response = await client.patch(f"/investor/notifications/{note['sequence']}/read", headers={"Origin": "http://testserver"}, json={"read": True, "mutation": {"expected_revision": note["revision"], "idempotency_key": str(uuid4())}})
        assert response.status_code == 200 and response.json()["read"]
        assert (await client.get("/investor/notifications?unread=true")).json() == []


@pytest.mark.asyncio
async def test_limited_page_does_not_starve_other_cases(publication_db):
    candidate, _, _, _ = await saved()
    from investment_agent.investor.foundations import CreateCase
    from investment_agent.storage.repositories.listing import ListingRepository
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing("000001.SZ", company_name="Fixture other")
        second = await InvestorService(session).create_case("local_owner", CreateCase(listing_ref=ObjectRef(kind="listing", id=listing.id, revision=1), question="Other", idempotency_key=uuid4()))
    for _ in range(2):
        async with session_scope() as session:
            assert await MonitoringService(session, base()).scan(limit=1) == 1
    async with session_scope() as session:
        assert await session.get(MonitorReceipt, f"scan:{candidate.thesis.case_ref.id}")
        assert await session.get(MonitorReceipt, f"scan:{second.id}")


@pytest.mark.asyncio
async def test_payload_noop_and_new_result_unread_same_notification(publication_db):
    candidate, _, _, result = await saved()
    async with session_scope() as session:
        await PublicationService(session).restrict(result.recommendation_ref, reason="critical_counter_evidence:formal_review_required")
        svc = MonitoringService(session, base())
        await svc.scan()
        cycle = await session.scalar(select(ReviewCycle))
        note = (await svc.notifications())[0]
        await svc.read(note["sequence"], expected_revision=note["revision"], read=True)
        now = datetime.now(UTC)
        await svc._notification(cycle, category="major_change", stage="review_required", now=now)
        unchanged = (await svc.notifications())[0]
        assert unchanged["read"] and unchanged["revision"] == note["revision"] + 1
        await svc._notification(cycle, category="major_change", stage="reviewed", now=now)
        updated = (await svc.notifications())[0]
        assert not updated["read"] and updated["sequence"] == note["sequence"]
        await svc._notification(cycle, category="major_change", stage="reviewed", now=now)
        assert (await svc.notifications())[0] == updated


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", ["unknown", "cny", "usd"])
async def test_background_budget_and_verified_price_fail_closed(publication_db, quantity):
    from investment_agent.investor.common import Mutation, Quantity
    candidate, _, _, result = await saved()
    selected = base(investor_agent_background_enabled=True)
    async with session_scope() as session:
        foundation = InvestorService(session)
        mandate = await foundation.get_mandate("local_owner")
        budget = Quantity(value=None, unit="USD", unknown_reason="Unspecified") if quantity == "unknown" else Quantity(value="1", unit="CNY" if quantity == "cny" else "USD")
        auth = mandate.authorization.model_copy(update={"background_enabled": True, "exploration_scope_refs": mandate.scope_refs, "daily_budget": budget, "per_task_budget": budget})
        await foundation.revise_mandate("local_owner", mandate.model_copy(update={"authorization": auth, "revision": 2}), Mutation(expected_revision=1, idempotency_key=uuid4()))
        await PublicationService(session).restrict(result.recommendation_ref, reason="critical_counter_evidence:formal_review_required")
        await MonitoringService(session, selected).maintain()
        cycle = await session.scalar(select(ReviewCycle))
        assert cycle.task_id is None and cycle.state == "blocked"
        expected = {"unknown": "background_budget_unavailable", "cny": "verified_exchange_rate_unavailable", "usd": "background_price_unavailable"}[quantity]
        assert expected in cycle.payload["pending_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result_kind", ["withdraw", "inconclusive"])
async def test_default_worker_real_review_outcome_notification_and_terminal_rescan(publication_db, monkeypatch, result_kind):
    candidate, evidence, _, published = await saved()
    selected = base()
    async with session_scope() as session:
        receipt = await MonitoringService(session, selected).manual(candidate.thesis.case_ref.id, uuid4(), expected_revision=2)
    output = ResearchOutput(progress=full_progress(candidate, evidence), review=unchanged_review(published.recommendation_ref, (evidence,)).model_copy(update={"result": result_kind}))
    original = AgentExecutor.__init__
    def constructor(self, *args, **kwargs):
        kwargs["model"] = FunctionModel(lambda messages, info: ModelResponse([TextPart(output.model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=10)))
        original(self, *args, **kwargs)
    monkeypatch.setattr(AgentExecutor, "__init__", constructor)
    await ResearchWorker(settings=selected, worker_id="review-outcome").run_once()
    async with session_scope() as session:
        svc = MonitoringService(session, selected)
        await svc.settle()
        cycle = await session.get(ReviewCycle, receipt["id"])
        assert cycle.state == ("completed" if result_kind == "withdraw" else "stopped")
        notes = await svc.notifications()
        changes = [note for note in notes if note.get("cycle_id") == cycle.id]
        assert len(changes) == 1 and changes[0]["category"] == ("major_change" if result_kind == "withdraw" else "needs_input")
        assert (await PublicationService(session).get_recommendation(published.recommendation_ref)).status == ("withdrawn" if result_kind == "withdraw" else "review_required")
    if result_kind == "withdraw":
        before = await stats()
        for days in (0, 20, 50):
            async with session_scope() as session:
                await MonitoringService(session, selected).scan(now=datetime.now(UTC) + timedelta(days=days))
        # Only the scanner cursor is added; no task/cycle/notification is revived.
        after = await stats()
        assert after[1:] == before[1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [False, True])
async def test_native_capture_receipt_old_snapshot_unknown_cannot_extend(publication_db, monkeypatch, fresh):
    from decimal import Decimal

    from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
    from tests.support.publication import market_candidate
    candidate, evidence, old_evidence, old_bar = await market_candidate()
    published = await publish(candidate)
    selected = base()
    # seed_bars includes a future-known recapture; remove it so the old-data
    # control really has no post-opinion source update. Only this temp fixture.
    from sqlalchemy import delete

    from investment_agent.storage.models.pit_snapshot import MarketSnapshot
    async with session_scope() as session:
        await session.execute(delete(MarketSnapshot).where(MarketSnapshot.listing_id == old_bar.listing_id, MarketSnapshot.captured_at > candidate.recommendation.as_of))
    if fresh:
        async with session_scope() as session:
            value = Decimal("10")
            await MarketSnapshotRepository(session).insert(listing_id=old_bar.listing_id, trade_date=date(2026, 10, 9), open=value, high=value, low=value, close=value, volume=Decimal(100), provider="baostock", captured_at=datetime(2026, 10, 9, 9, tzinfo=UTC))
    async with session_scope() as session:
        receipt = await MonitoringService(session, selected).manual(candidate.thesis.case_ref.id, uuid4(), expected_revision=2)
    def respond(messages, info):
        returns = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse([ToolCallPart("capture_snapshot", {"ticker": "600519.SH", "field": "close", "as_of": receipt["started_at"]})], usage=RequestUsage(input_tokens=10, output_tokens=10))
        data = returns[0].content
        refs = tuple(ObjectRef.model_validate(ref) for ref in data.get("data") or ())
        review = unchanged_review(published.recommendation_ref, (evidence, *refs))
        output = ResearchOutput(progress=full_progress(candidate, evidence), review=review, extend=True)
        return ModelResponse([TextPart(output.model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=10))
    original = AgentExecutor.__init__
    def constructor(self, *args, **kwargs):
        kwargs["model"] = FunctionModel(respond)
        original(self, *args, **kwargs)
    monkeypatch.setattr(AgentExecutor, "__init__", constructor)
    await ResearchWorker(settings=selected, worker_id="capture-review").run_once()
    async with session_scope() as session:
        await MonitoringService(session, selected).settle()
        cycle = await session.get(ReviewCycle, receipt["id"])
        view = await PublicationService(session).get_recommendation(published.recommendation_ref)
        assert cycle.state == ("completed" if fresh else "stopped")
        assert (view.effective_valid_until > view.recommendation.valid_until) is fresh


@pytest.mark.asyncio
async def test_real_revision_updates_same_change_and_outbox_does_not_duplicate(publication_db, monkeypatch):
    from investment_agent.agent.research_publication import (
        CompanyViewInput,
        HypothesisInput,
        MonitorInput,
        SemanticInput,
    )
    from investment_agent.investor.publication import PublicationCandidate, SemanticAssessment
    candidate, evidence, _, published = await saved()
    selected = base()
    # AG20 crosses the user-decision boundary before material restriction/revision.
    from investment_agent.investor.controls import DecisionAction, UserControlService
    from investment_agent.storage.models.user_domain import HoldingItem
    async with session_scope() as session:
        holdings_before = await session.scalar(select(func.count()).select_from(HoldingItem))
        decision = await execute(session, DecisionAction(case_ref=await case_ref(candidate),
            recommendation_ref=published.recommendation_ref, decision="defer",
            user_text="I will defer while the cash premise is checked"))
    async with session_scope() as session:
        await MonitoringService(session, selected).consume_outbox()
        await PublicationService(session).restrict(published.recommendation_ref, reason="critical_counter_evidence:formal_review_required")
        await MonitoringService(session, selected).scan()
        receipt = await MonitoringService(session, selected).manual(candidate.thesis.case_ref.id, uuid4(), expected_revision=2)
        case = await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)
    plan = full_progress(candidate, evidence)
    business = CompanyViewInput(case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision), question_scope="Bounded revised company research", change_reason="Critical premises checked after counter evidence",
        assessed_dimensions=candidate.thesis.assessed_dimensions, claims=candidate.thesis.claims, counter_case=candidate.thesis.counter_case,
        gaps=candidate.recommendation.gaps, dependencies=candidate.recommendation.dependency_refs,
        business_view="undetermined", price_view="undetermined", sufficiency="bounded",
        hypotheses=(HypothesisInput(key="cash", question="Is cash durable?", scope="Operating cash", support_condition="Cash follows demand", falsification_condition="Cash reverses"),),
        condition_hypothesis_keys=("cash",), monitors=(MonitorInput(hypothesis_key="cash", trigger="calendar"),))
    def respond(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "save_research_progress" for p in returns):
            return ModelResponse([ToolCallPart("save_research_progress", {"business": plan.model_dump(mode="json")})], usage=RequestUsage(input_tokens=10, output_tokens=10))
        if not any(p.tool_name == "prepare_company_view" for p in returns):
            return ModelResponse([ToolCallPart("prepare_company_view", {"business": business.model_dump(mode="json")})], usage=RequestUsage(input_tokens=10, output_tokens=10))
        prepared = next(p.content for p in returns if p.tool_name == "prepare_company_view")
        exact = PublicationCandidate.model_validate(prepared["candidate"])
        semantic = SemanticInput(preparation_ref=ObjectRef.model_validate(prepared["preparation_ref"]), assessments=tuple(SemanticAssessment(claim_id=c.id, fact_relation="Historical cash supports only a limited view", counter_evidence_relevance="Temporary cash remains unresolved", materiality_reason="Cash persistence matters") for c in exact.thesis.claims), checked_sources=(evidence,), unresolved_questions=(), conclusion="pass")
        return ModelResponse([TextPart(ResearchOutput(progress=plan, semantic_review=semantic).model_dump_json())], usage=RequestUsage(input_tokens=10, output_tokens=10))
    original = AgentExecutor.__init__
    def constructor(self, *args, **kwargs):
        kwargs["model"] = FunctionModel(respond)
        original(self, *args, **kwargs)
    monkeypatch.setattr(AgentExecutor, "__init__", constructor)
    await ResearchWorker(settings=selected, worker_id="revision").run_once()
    await ResearchWorker(settings=selected, worker_id="consume-restart").run_once()
    async with session_scope() as session:
        svc = MonitoringService(session, selected)
        cycle = await session.get(ReviewCycle, receipt["id"])
        assert cycle.state == "completed"
        changes = [note for note in await svc.notifications() if note.get("cycle_id") == cycle.id]
        assert len(changes) == 1 and changes[0]["stage"] == "reviewed"
        old = await PublicationService(session).get_recommendation(published.recommendation_ref)
        assert old.status == "superseded" and old.replacement_ref
        assert len(await svc.notifications()) == 2  # original independent + one change
        notes = await svc.notifications()
        await svc.settle()
        await svc.consume_outbox()
        assert await svc.notifications() == notes
        saved_decisions = await UserControlService(session).list_objects("decision")
        assert saved_decisions[0]["id"] == str(decision.id)
        assert saved_decisions[0]["recommendation_ref"] == published.recommendation_ref.model_dump(mode="json")
        assert await session.scalar(select(func.count()).select_from(HoldingItem)) == holdings_before



@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["correction", "new_period", "conflict", "missing"])
async def test_real_financial_new_version_or_period_and_unknown_restrict_merge(publication_db, change):
    from investment_agent.evidence.snapshots import materialize
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.investor.contracts import Dependency, NumericNode
    candidate, _, subject = await fixture()
    async with session_scope() as session:
        from investment_agent.storage.models.reference import Listing
        listing = await session.get(Listing, subject.id)
        from investment_agent.domain.a_share import A_SHARE_MARKETS
        from investment_agent.storage.repositories.listing import ListingRepository
        await ListingRepository(session).ensure_a_share_market(A_SHARE_MARKETS["SH"])
        listing.ticker = "600000"
        listing.market_code = A_SHARE_MARKETS["SH"].market_code
        original = await FinancialSnapshotRepository(session).insert(listing_id=subject.id, report_period=date(2025, 12, 31), period_kind="annual", fields={"net_profit": 123456789}, provider="baostock", captured_at=candidate.thesis.as_of - timedelta(days=1), availability="actual", available_from=date(2026, 4, 1))
        ref = await EvidenceService(session).save_evidence("local_owner", materialize(original, "net_profit_total"), uuid4())
    claim = candidate.thesis.claims[0].model_copy(update={"materiality": "context", "verification": "pending", "support_refs": (ref,), "text": (TextNode(type="text", text="Historical reported profit "), NumericNode(type="numeric_claim", ref=ref.model_copy(update={"field_path": "value"}), role="historical_fact", display_format="decimal"))})
    candidate = candidate.model_copy(update={"thesis": candidate.thesis.model_copy(update={"claims": (claim,)}), "recommendation": candidate.recommendation.model_copy(update={"claims": (claim,), "provenance": (ref,), "dependency_refs": (Dependency(ref=ref, materiality="critical", fields=("value",)),)})})
    published = await publish(candidate)
    assert published.published, published.check.model_dump()
    async with session_scope() as session:
        await MonitoringService(session, base()).scan()
        assert await session.scalar(select(func.count()).select_from(ReviewCycle)) == 0
        await FinancialSnapshotRepository(session).insert(listing_id=subject.id, report_period=date(2026, 6, 30) if change in {"new_period", "missing"} else date(2025, 12, 31), period_kind="semi" if change in {"new_period", "missing"} else "annual", fields={} if change == "missing" else {"net_profit_total": 999999999} if change == "conflict" else {"net_profit": 123456789 if change == "new_period" else 999999999}, provider="akshare" if change == "conflict" else "baostock", captured_at=datetime.now(UTC), availability="actual", available_from=date(2026, 9, 30))
    # Existing AG12 reads restrict before any monitor consumer runs.
    async with session_scope() as session:
        assert (await PublicationService(session).get_recommendation(published.recommendation_ref)).status == "review_required"
        await MonitoringService(session, base()).scan()
        cycles = (await session.scalars(select(ReviewCycle))).all()
        assert len(cycles) == 1
        assert len([note for note in await MonitoringService(session, base()).notifications() if note.get("cycle_id")]) == 1
        if change == "missing":
            assert any("missing" in reason for reason in cycles[0].payload["reasons"])
    before = await stats()
    for _ in range(3):
        async with session_scope() as session:
            await MonitoringService(session, base()).scan()
    assert await stats() == before


@pytest.mark.asyncio
async def test_manual_reason_is_preserved_and_idempotency_payload_checked(publication_db):
    candidate, _, _, _ = await saved()
    key = uuid4()
    async with session_scope() as session:
        svc = MonitoringService(session, base())
        first = await svc.manual(candidate.thesis.case_ref.id, key, expected_revision=2, reason="Recheck my old inference")
        assert "Recheck my old inference" in repr(first["questions"])
        with pytest.raises(InvestorError, match="different revision"):
            await svc.manual(candidate.thesis.case_ref.id, key, expected_revision=2, reason="New question")
        second = await svc.manual(candidate.thesis.case_ref.id, uuid4(), expected_revision=2, reason="Compare the new alternative")
        assert second["id"] == first["id"] and len(second["dirty"]) == 1
        assert "Compare the new alternative" in repr(second["questions"])


@pytest.mark.asyncio
async def test_pause_existing_background_cycle_manual_gets_new_interactive_task(publication_db):
    from investment_agent.agent.model import AgentModelConfig
    from investment_agent.investor.common import Mutation, Quantity
    from investment_agent.storage.models.agent import ExecutionGrantRow
    candidate, _, _, published = await saved()
    selected = base(investor_agent_background_enabled=True, investor_agent_price_version="offline-verified", investor_agent_price_model="fixture-model", investor_agent_input_usd_per_million=Decimal("1"), investor_agent_output_usd_per_million=Decimal("1"))
    selected = selected.model_copy(update={"investor_agent_price_config_version": AgentModelConfig.from_settings(selected, 0).version})
    async with session_scope() as session:
        foundation = InvestorService(session)
        mandate = await foundation.get_mandate("local_owner")
        authorization = mandate.authorization.model_copy(update={"background_enabled": True, "exploration_scope_refs": mandate.scope_refs, "daily_budget": Quantity(value="10", unit="USD"), "per_task_budget": Quantity(value="1", unit="USD")})
        await foundation.revise_mandate("local_owner", mandate.model_copy(update={"revision": 2, "authorization": authorization}), Mutation(expected_revision=1, idempotency_key=uuid4()))
        await PublicationService(session).restrict(published.recommendation_ref, reason="critical_counter_evidence:formal_review_required")
        svc = MonitoringService(session, selected)
        await svc.maintain()
        background = await session.scalar(select(ReviewCycle))
        assert background.state == "running" and background.task_id
        background_id = background.task_id
        await execute(session, CaseAction(case_ref=await case_ref(candidate), action="pause", reason="Pause ongoing automatic work"))
        case = await foundation.get_case("local_owner", candidate.thesis.case_ref.id)
        manual = await svc.manual(case.id, uuid4(), expected_revision=case.revision, reason="Please review despite the background pause")
        assert manual["task_id"] != background_id
        new_grant = await session.get(ExecutionGrantRow, manual["task_id"])
        assert new_grant.payload["mode"] == "interactive"
        assert (await TaskService(session).get_task(UUID(background_id))).status == "cancelled"
        assert background.state == "stopped"


@pytest.mark.asyncio
async def test_archived_pending_review_is_closed_without_replaying_after_reopen(publication_db):
    candidate, _, _, published = await saved()
    async with session_scope() as session:
        svc = MonitoringService(session, base())
        await PublicationService(session).restrict(published.recommendation_ref, reason="critical_counter_evidence:formal_review_required")
        await svc.scan()
        cycle = await session.scalar(select(ReviewCycle))
        await execute(session, CaseAction(case_ref=await case_ref(candidate), action="archive", reason="Close research"))
        await svc.maintain()
        assert cycle.state == "stopped" and cycle.task_id is None
        current = await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)
        await execute(session, CaseAction(case_ref=ObjectRef(kind="case", id=current.id, revision=current.revision), action="reopen", reason="User reopens the archive"))
        await svc.maintain()
        assert cycle.state == "stopped" and cycle.task_id is None
        assert await session.scalar(select(func.count()).select_from(ReviewCycle)) == 1

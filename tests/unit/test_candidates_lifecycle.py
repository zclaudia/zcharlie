"""P1-D1 / P1-D2: candidate lifecycle and the append-only decision journal."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from investment_agent.candidates.lifecycle import (
    RULE_DRAFT,
    CandidateConflictError,
    CandidateInputError,
    CandidatePolicy,
    create_candidates_from_screen,
    dismiss_candidate,
    effective_draft,
    expire_candidates,
    mark_draft_result,
    nominate_candidate,
    rule_draft_idempotency_key,
)
from investment_agent.config import Settings
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import (
    CandidateRecord,
    DecisionRecord,
    ImmutableDecisionError,
)
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.storage.repositories.candidate import (
    DecisionRepository,
    ReasonRequiredError,
    as_utc,
)
from investment_agent.storage.repositories.watchlist import WatchlistRepository
from tests.unit._candidate_support import (
    AS_OF,
    TEMPLATE_ID,
    cand_db,  # noqa: F401 - fixture
    seed_draft,
    seed_listing,
    seed_result,
    seed_screen_run,
    seed_template,
)

NOW = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)
POLICY = CandidatePolicy(expiry_days=14, dismiss_cooldown_days=30)

pytestmark = pytest.mark.usefixtures("cand_db")


async def _candidates() -> list[CandidateRecord]:
    async with session_scope() as session:
        return list(
            (await session.execute(select(CandidateRecord).order_by(CandidateRecord.id))).scalars()
        )


async def _decisions(kind: str | None = None) -> list[DecisionRecord]:
    async with session_scope() as session:
        stmt = select(DecisionRecord).order_by(DecisionRecord.id)
        if kind is not None:
            stmt = stmt.where(DecisionRecord.kind == kind)
        return list((await session.execute(stmt)).scalars())


async def _draft_runs() -> list[PipelineRun]:
    async with session_scope() as session:
        stmt = select(PipelineRun).where(PipelineRun.kind == RULE_DRAFT).order_by(PipelineRun.id)
        return list((await session.execute(stmt)).scalars())


async def _seed_hit_run(*, extra_templates: bool = False) -> tuple[int, int, int]:
    """Template v1 + one screen run with a hit for 600519 (and noise rows)."""

    async with session_scope() as session:
        await seed_template(session)
        moutai = await seed_listing(session)
        icbc = await seed_listing(session, "601398.SH", "工商银行")
        run_id = await seed_screen_run(session)
        hit = await seed_result(session, run_id=run_id, listing=moutai)
        await seed_result(session, run_id=run_id, listing=icbc, ticker="601398.SH", outcome="miss")
        if extra_templates:
            await seed_template(session, id="cand_other", name="Other")
            await seed_result(
                session,
                run_id=run_id,
                listing=moutai,
                template_id="cand_other",
                strength=0.9,
            )
        return run_id, moutai.id, hit.id


async def test_screen_hit_opens_candidate_with_decision_and_draft_job() -> None:
    async with session_scope() as session:
        # Earlier user feedback: becomes preference notes, newest first.
        listing = await seed_listing(session, "000001.SZ", "平安银行")
        for index in range(12):
            kind = "dismiss" if index % 2 else "reject_rule"
            await DecisionRepository(session).append(
                listing_id=listing.id,
                ticker="000001.SZ",
                kind=kind,
                actor="user",
                reason_text=f"reason {index}",
                context={"template_ref": f"tmpl_{index}@1"},
                at=NOW - timedelta(days=30 - index),
            )
        await DecisionRepository(session).append(
            listing_id=listing.id,
            ticker="000001.SZ",
            kind="promote",
            actor="user",
            reason_text="not a preference note",
            at=NOW,
        )
    run_id, listing_id, hit_id = await _seed_hit_run()

    async with session_scope() as session:
        outcome = await create_candidates_from_screen(
            session, run_id=run_id, policy=POLICY, now=NOW
        )
    assert [item["ticker"] for item in outcome.created] == ["600519.SH"]
    assert outcome.drafts_enqueued == 1

    [candidate] = await _candidates()
    assert candidate.status == "open" and candidate.listing_id == listing_id
    assert as_utc(candidate.expires_at) == NOW + timedelta(days=14)
    assert candidate.draft_state == "pending"
    assert candidate.last_screen_result_id == hit_id
    assert candidate.sources == [
        {
            "kind": "screen",
            "screen_run_id": run_id,
            "template_id": TEMPLATE_ID,
            "template_version": 1,
            "screen_result_id": hit_id,
            "as_of": AS_OF.isoformat(),
        }
    ]

    [nominate] = await _decisions("nominate")
    assert nominate.actor == "system" and nominate.candidate_id == candidate.id
    [result_ctx] = nominate.context["screen_results"]
    assert result_ctx["screen_result_id"] == hit_id
    assert [r["factor"] for r in result_ctx["reasons"]] == ["roe", "gross_margin"]
    assert result_ctx["reasons"][0]["actual"] == 0.31
    assert result_ctx["reasons"][0]["snapshot_ref"]["financial_snapshot_id"] == 11

    [run] = await _draft_runs()
    assert run.status == "queued"
    assert run.idempotency_key == f"rule_draft:{candidate.id}:{TEMPLATE_ID}@1"
    assert run.idempotency_key == rule_draft_idempotency_key(candidate.id, TEMPLATE_ID, 1)
    expected_notes = [
        f"[tmpl_{index}@1] {'dismiss' if index % 2 else 'reject_rule'} 000001.SZ: reason {index}"
        for index in range(11, 1, -1)
    ]
    assert run.request_json == {
        "candidate_id": candidate.id,
        "ticker": "600519.SH",
        "template_id": TEMPLATE_ID,
        "template_version": 1,
        "as_of": "2026-09-27",
        "screen_result_id": hit_id,
        "preference_notes": expected_notes,
    }


async def test_second_hit_merges_sources_and_is_idempotent() -> None:
    run_id, listing_id, _hit = await _seed_hit_run(extra_templates=True)
    async with session_scope() as session:
        first = await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    # Strongest hit drafts; both templates are sources of one candidate.
    assert first.created[0]["templates"] == ["cand_other@1", "cand_test@1"]
    [candidate] = await _candidates()
    assert [s["template_id"] for s in candidate.sources] == ["cand_other", "cand_test"]
    [run] = await _draft_runs()
    assert run.request_json["template_id"] == "cand_other"

    # Same run again (retried job): nothing new.
    async with session_scope() as session:
        again = await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    assert again.created == [] and again.merged[0]["added_templates"] == []
    [candidate] = await _candidates()
    assert len(candidate.sources) == 2

    # A later run hitting the same listing appends a source, no new draft / decision.
    async with session_scope() as session:
        listing = await session.get(Listing, listing_id)
        later = await seed_screen_run(session, date(2026, 10, 4))
        await seed_result(session, run_id=later, listing=listing, as_of=date(2026, 10, 4))
    async with session_scope() as session:
        merged = await create_candidates_from_screen(session, run_id=later, policy=POLICY, now=NOW)
    assert merged.merged == [
        {"candidate_id": candidate.id, "ticker": "600519.SH", "added_templates": ["cand_test"]}
    ]
    [candidate] = await _candidates()
    assert len(candidate.sources) == 3 and candidate.status == "open"
    assert len(await _draft_runs()) == 1
    assert len(await _decisions("nominate")) == 1


async def test_only_hits_count_not_truncated() -> None:
    async with session_scope() as session:
        await seed_template(session)
        listing = await seed_listing(session)
        run_id = await seed_screen_run(session)
        await seed_result(session, run_id=run_id, listing=listing, outcome="truncated", rank=21)
    async with session_scope() as session:
        outcome = await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    assert outcome.created == [] and await _candidates() == []


async def test_watchlisted_listing_is_skipped() -> None:
    run_id, listing_id, _ = await _seed_hit_run()
    async with session_scope() as session:
        await WatchlistRepository(session).add(listing_id, industry="食品饮料", report_year=2025)
    async with session_scope() as session:
        outcome = await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    assert outcome.skipped_watchlist == ["600519.SH"]
    assert await _candidates() == [] and await _draft_runs() == []


@pytest.mark.parametrize(("days_ago", "skipped"), [(5, True), (31, False)])
async def test_dismiss_cooldown(days_ago: int, skipped: bool) -> None:
    run_id, listing_id, _ = await _seed_hit_run()
    async with session_scope() as session:
        session.add(
            CandidateRecord(
                listing_id=listing_id,
                ticker="600519.SH",
                status="dismissed",
                opened_at=NOW - timedelta(days=days_ago + 3),
                expires_at=NOW + timedelta(days=10),
                closed_at=NOW - timedelta(days=days_ago),
                sources=[],
                draft_state="none",
            )
        )
    async with session_scope() as session:
        outcome = await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    if skipped:
        assert [item["ticker"] for item in outcome.skipped_cooldown] == ["600519.SH"]
        assert outcome.created == []
    else:
        assert outcome.skipped_cooldown == []
        assert [item["ticker"] for item in outcome.created] == ["600519.SH"]


async def test_expiry_sweep() -> None:
    run_id, _listing_id, _ = await _seed_hit_run()
    async with session_scope() as session:
        await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    async with session_scope() as session:
        assert await expire_candidates(session, now=NOW + timedelta(days=13)) == []
    async with session_scope() as session:
        expired = await expire_candidates(session, now=NOW + timedelta(days=14, minutes=1))
    [candidate] = await _candidates()
    assert expired == [candidate.id] and candidate.status == "expired"
    assert candidate.closed_at is not None
    [decision] = await _decisions("expire")
    assert decision.actor == "system" and decision.candidate_id == candidate.id
    assert decision.context["screen_results"]
    # Idempotent.
    async with session_scope() as session:
        assert await expire_candidates(session, now=NOW + timedelta(days=20)) == []


async def test_dismiss_requires_reason_and_journals() -> None:
    run_id, _listing_id, _ = await _seed_hit_run()
    async with session_scope() as session:
        await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    [candidate] = await _candidates()
    async with session_scope() as session:
        with pytest.raises(CandidateInputError):
            await dismiss_candidate(session, candidate.id, reason="   ")
    async with session_scope() as session:
        await dismiss_candidate(session, candidate.id, reason="估值已不便宜", now=NOW)
    [candidate] = await _candidates()
    assert candidate.status == "dismissed"
    [decision] = await _decisions("dismiss")
    assert decision.reason_text == "估值已不便宜" and decision.actor == "user"
    assert decision.context["template_ref"] == f"{TEMPLATE_ID}@1"
    assert decision.context["screen_results"][0]["reasons"][1]["actual"] == 0.91
    async with session_scope() as session:
        with pytest.raises(CandidateConflictError):
            await dismiss_candidate(session, candidate.id, reason="again")


async def test_decision_and_state_change_roll_back_together(monkeypatch) -> None:
    run_id, _listing_id, _ = await _seed_hit_run()
    async with session_scope() as session:
        await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    [candidate] = await _candidates()
    before = len(await _decisions())

    async def broken_append(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        raise RuntimeError("journal write failed")

    monkeypatch.setattr(DecisionRepository, "append", broken_append)
    with pytest.raises(RuntimeError, match="journal write failed"):
        async with session_scope() as session:
            await dismiss_candidate(session, candidate.id, reason="injected")
    [candidate] = await _candidates()
    assert candidate.status == "open" and candidate.closed_at is None
    assert len(await _decisions()) == before

    # Screen creation: the candidate, its decision and its draft job are one unit.
    async with session_scope() as session:
        listing = await seed_listing(session, "600900.SH", "长江电力")
        other_run = await seed_screen_run(session)
        await seed_result(session, run_id=other_run, listing=listing, ticker="600900.SH")
    with pytest.raises(RuntimeError):
        async with session_scope() as session:
            await create_candidates_from_screen(session, run_id=other_run, policy=POLICY, now=NOW)
    assert [c.ticker for c in await _candidates()] == ["600519.SH"]
    assert len(await _draft_runs()) == 1


async def test_decisions_are_append_only() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
        decision = await DecisionRepository(session).append(
            listing_id=listing.id, ticker="600519.SH", kind="nominate", actor="user"
        )
        decision_id = decision.id
    with pytest.raises(ImmutableDecisionError):
        async with session_scope() as session:
            record = await session.get(DecisionRecord, decision_id)
            assert record is not None
            record.reason_text = "rewritten"
            await session.flush()
    with pytest.raises(ImmutableDecisionError):
        async with session_scope() as session:
            record = await session.get(DecisionRecord, decision_id)
            await session.delete(record)
            await session.flush()
    async with session_scope() as session:
        repo = DecisionRepository(session)
        assert not hasattr(repo, "update") and not hasattr(repo, "delete")
        for kind in ("dismiss", "reject_rule"):
            with pytest.raises(ReasonRequiredError):
                await repo.append(
                    listing_id=listing.id, ticker="600519.SH", kind=kind, actor="user",
                    reason_text=" ",
                )
        with pytest.raises(ValueError, match="unknown decision kind"):
            await repo.append(listing_id=listing.id, ticker="600519.SH", kind="buy", actor="user")


async def test_mark_draft_result_and_lazy_state() -> None:
    run_id, _listing_id, _ = await _seed_hit_run()
    async with session_scope() as session:
        await create_candidates_from_screen(session, run_id=run_id, policy=POLICY, now=NOW)
    [candidate] = await _candidates()
    async with session_scope() as session:
        record = await session.get(CandidateRecord, candidate.id)
        assert record is not None
        assert await effective_draft(session, record) == ("pending", None)
        draft = await seed_draft(session)
        state, rule = await effective_draft(session, record)
        assert state == "ready" and rule is not None and rule.id == draft.id
        draft_id = draft.id

    await mark_draft_result(candidate.id, rule_instance_id=None, failed=True)
    [candidate] = await _candidates()
    assert candidate.draft_state == "failed"
    await mark_draft_result(candidate.id, rule_instance_id=draft_id)
    [candidate] = await _candidates()
    assert candidate.draft_state == "ready" and candidate.draft_rule_instance_id == draft_id


async def test_manual_nomination() -> None:
    async with session_scope() as session:
        await seed_template(session)
    async with session_scope() as session:
        outcome = await nominate_candidate(
            session,
            ticker="600900.SH",
            template_id=TEMPLATE_ID,
            note="水电现金流稳定",
            policy=POLICY,
            as_of=date(2026, 10, 7),
            now=NOW,
        )
        assert outcome.created and outcome.draft_run_id is not None
    [candidate] = await _candidates()
    assert candidate.sources[0]["kind"] == "manual" and candidate.note == "水电现金流稳定"
    [decision] = await _decisions("nominate")
    assert decision.actor == "user" and decision.reason_text == "水电现金流稳定"
    [run] = await _draft_runs()
    assert run.request_json["screen_result_id"] is None
    assert run.request_json["as_of"] == "2026-10-07"
    assert run.request_json["preference_notes"] == []

    # Second nomination merges; unknown template / watchlisted listing refused.
    async with session_scope() as session:
        again = await nominate_candidate(
            session, ticker="600900.SH", policy=POLICY, as_of=date(2026, 10, 7)
        )
        assert not again.created and again.candidate.id == candidate.id
    async with session_scope() as session:
        with pytest.raises(CandidateInputError):
            await nominate_candidate(
                session, ticker="600519.SH", template_id="nope", policy=POLICY, as_of=AS_OF
            )
    async with session_scope() as session:
        listing = await seed_listing(session)
        await WatchlistRepository(session).add(listing.id, industry="食品饮料")
    async with session_scope() as session:
        with pytest.raises(CandidateConflictError):
            await nominate_candidate(session, ticker="600519.SH", policy=POLICY, as_of=AS_OF)


async def test_at_most_one_open_candidate_per_listing() -> None:
    from sqlalchemy.exc import IntegrityError

    async with session_scope() as session:
        listing = await seed_listing(session)
        listing_id = listing.id

    def row(status: str) -> CandidateRecord:
        return CandidateRecord(
            listing_id=listing_id,
            ticker="600519.SH",
            status=status,
            opened_at=NOW,
            expires_at=NOW,
            sources=[],
            draft_state="none",
        )

    async with session_scope() as session:
        session.add_all([row("open"), row("dismissed"), row("expired")])
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(row("open"))


async def test_policy_from_settings() -> None:
    settings = Settings(  # type: ignore[call-arg]
        api_key="x" * 32,
        llm_tier3_api_key="x",
        candidate_expiry_days=7,
        candidate_dismiss_cooldown_days=60,
    )
    assert CandidatePolicy.from_settings(settings) == CandidatePolicy(7, 60)
    default = Settings(api_key="x" * 32, llm_tier3_api_key="x")  # type: ignore[call-arg]
    assert CandidatePolicy.from_settings(default) == CandidatePolicy(14, 30)


async def test_rule_instance_lookup_ignores_old_drafts() -> None:
    """A draft older than the candidate does not count as its draft."""

    async with session_scope() as session:
        await seed_template(session)
        listing = await seed_listing(session)
        await seed_draft(session)
    async with session_scope() as session:
        run_id = await seed_screen_run(session)
        await seed_result(session, run_id=run_id, listing=listing)
    async with session_scope() as session:
        await create_candidates_from_screen(
            session, run_id=run_id, policy=POLICY, now=datetime.now(UTC) + timedelta(seconds=5)
        )
    [candidate] = await _candidates()
    async with session_scope() as session:
        record = await session.get(CandidateRecord, candidate.id)
        assert record is not None
        assert await effective_draft(session, record) == ("pending", None)
        rules = list((await session.execute(select(RuleInstanceRecord))).scalars())
        assert len(rules) == 1


def test_screen_context_gains_shenwan_industry_name() -> None:
    from investment_agent.candidates.lifecycle import _context_with_industry_name

    stored = {"industry": {"system": "sw2021", "code": "220804", "group": "cyclical"}}
    shown = _context_with_industry_name(stored)
    assert shown["industry"]["name"] == "钾肥"
    assert "name" not in stored["industry"]  # display only: the stored context is untouched
    assert _context_with_industry_name({"industry": {"system": "csrc", "code": "C26"}}) == {
        "industry": {"system": "csrc", "code": "C26"}
    }
    assert _context_with_industry_name(None) == {}

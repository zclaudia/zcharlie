"""Card 19: candidate-shadow operations defaults (§7.3 cost knobs).

``Settings.method_candidate_shadow_min_positives`` raises the approval
minimum of matched positive candidate shadows (default 1 keeps the card-16
gate; the coverage and block-counterexample rules are never relaxed), and
``Settings.method_candidate_shadow_max_per_proposal`` caps how many candidate
shadow tasks one package proposal may start. The fixtures are the card-15/16
ones: ``shadow_proposal`` builds a passed package proposal, and
``register_candidate_shadow`` runs one real worker shadow and registers its
result - the returned ``proposal`` payload carries the new revision every
later ``decision_for`` must freeze its evidence against.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.methods import CandidateShadowRequest, MethodService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.investor import MethodArtifact
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.test_method_evolution import decision_for, register_candidate_shadow
from tests.unit.test_candidate_shadow import (
    shadow_proposal,
    shadow_ready_mandate,
    shadow_settings,
)


async def start_shadow_under(settings, proposal_id, *, expected, mutation=None):
    """``start_shadow`` with caller-supplied Settings so card-19 overrides reach
    the per-proposal initiation cap."""
    request = CandidateShadowRequest(mutation_id=mutation or uuid4(), proposal_id=UUID(proposal_id), prompt="研究这家公司的现金持续性与价格预期差", expected=expected)
    async with session_scope() as session:
        return await MethodService(session).start_candidate_shadow(request, base=settings)


@pytest.mark.asyncio
async def test_default_gate_approves_block_plus_one_positive_without_base(conversation_db):
    # Card 19 §4.1: without ``base`` the existing threshold is untouched - one
    # matched expected-block run plus one matched positive run approves.
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    blocked = await register_candidate_shadow(proposal, weak, expected="block")
    assert blocked["candidate_shadow"]["matched"]
    passed = await register_candidate_shadow(blocked["proposal"], weak, expected="pass")
    assert passed["candidate_shadow"]["matched"] and passed["candidate_shadow"]["outcome"] == "passed"
    decision = await decision_for(passed["proposal"], subject)
    async with session_scope() as session:
        svc = MethodService(session)
        active = await svc.decide(decision, actor="authenticated_user")
        assert active["status"] == "active"
        assert (await svc.current(subject))[0] == 1


@pytest.mark.asyncio
async def test_min_positives_two_requires_a_second_matched_positive(conversation_db):
    # Card 19 §4.2: min_positives=2 refuses a single matched positive naming
    # the counts; a second matched positive approves. The second positive must
    # itself be matched - it becomes the latest run the coverage rule reads.
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    gate = shadow_settings(method_candidate_shadow_min_positives=2)
    blocked = await register_candidate_shadow(proposal, weak, expected="block")
    single = await register_candidate_shadow(blocked["proposal"], weak, expected="pass")
    assert single["candidate_shadow"]["matched"] and single["candidate_shadow"]["outcome"] == "passed"
    short = await decision_for(single["proposal"], subject)
    async with session_scope() as session:
        svc = MethodService(session)
        with pytest.raises(InvestorError) as excinfo:
            await svc.decide(short, actor="authenticated_user", base=gate)
        assert excinfo.value.detail.code == "invalid_reference"
        message = excinfo.value.detail.message
        assert "正例" in message and "2" in message and "1" in message
        assert (await svc.current(subject))[0] == 0  # the refusal moved no head
        stored = (await svc.proposal(UUID(single["proposal"]["id"]))).payload
        assert stored["status"] == "shadow"  # and left the proposal open
    second = await register_candidate_shadow(single["proposal"], weak, expected="pass")
    assert second["candidate_shadow"]["matched"] and second["candidate_shadow"]["outcome"] == "passed"
    decision = await decision_for(second["proposal"], subject)
    async with session_scope() as session:
        svc = MethodService(session)
        active = await svc.decide(decision, actor="authenticated_user", base=gate)
        assert active["status"] == "active"
        assert (await svc.current(subject))[0] == 1


@pytest.mark.asyncio
async def test_max_per_proposal_refuses_the_third_initiation(conversation_db):
    # Card 19 §4.3: with the cap at 2 the first two initiations queue normally
    # and the third is refused before any write - no third task or claim.
    _, _, subject, _, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    gate = shadow_settings(method_candidate_shadow_max_per_proposal=2)
    first = await start_shadow_under(gate, proposal["id"], expected="block")
    second = await start_shadow_under(gate, proposal["id"], expected="pass")
    assert first["status"] == "queued" and second["status"] == "queued"
    with pytest.raises(InvestorError) as excinfo:
        await start_shadow_under(gate, proposal["id"], expected="pass")
    assert excinfo.value.detail.code == "invalid_reference"
    message = excinfo.value.detail.message
    assert "上限 2" in message and "已发起 2" in message
    async with session_scope() as session:
        claims = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "candidate_shadow_claim"))).all()
        assert sum(1 for item in claims if item.payload["proposal_id"] == proposal["id"]) == 2
        tasks = (await session.scalars(select(AgentTask).where(AgentTask.input_key.like(f"candidate-shadow:{proposal['id']}%")))).all()
        assert len(tasks) == 2


@pytest.mark.asyncio
async def test_base_with_default_minimum_matches_the_baseless_gate(conversation_db):
    # Card 19 §4.4: ``base`` with the default minimum (1) behaves exactly like
    # no ``base`` - the same evidence state raises the same refusal, and the
    # same minimal evidence (block counterexample + one matched positive, the
    # evidence the baseless approval above runs on) approves.
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    gate = shadow_settings()
    assert gate.method_candidate_shadow_min_positives == 1
    blocked = await register_candidate_shadow(proposal, weak, expected="block")
    short = await decision_for(blocked["proposal"], subject)
    async with session_scope() as session:
        svc = MethodService(session)
        with pytest.raises(InvestorError) as plain:
            await svc.decide(short, actor="authenticated_user")
        with pytest.raises(InvestorError) as configured:
            await svc.decide(short, actor="authenticated_user", base=gate)
        assert plain.value.detail.code == configured.value.detail.code
        assert plain.value.detail.message == configured.value.detail.message
    passed = await register_candidate_shadow(blocked["proposal"], weak, expected="pass")
    decision = await decision_for(passed["proposal"], subject)
    async with session_scope() as session:
        svc = MethodService(session)
        active = await svc.decide(decision, actor="authenticated_user", base=gate)
        assert active["status"] == "active"
        assert (await svc.current(subject))[0] == 1

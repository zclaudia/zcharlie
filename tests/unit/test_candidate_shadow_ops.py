"""Cards 19/20: candidate-shadow operations defaults (§7.3 cost knobs) and
the batch initiation + workbench API additions.

``Settings.method_candidate_shadow_min_positives`` raises the approval
minimum of matched positive candidate shadows (default 1 keeps the card-16
gate; the coverage and block-counterexample rules are never relaxed), and
``Settings.method_candidate_shadow_max_per_proposal`` caps how many candidate
shadow tasks one package proposal may start. The fixtures are the card-15/16
ones: ``shadow_proposal`` builds a passed package proposal, and
``register_candidate_shadow`` runs one real worker shadow and registers its
result - the returned ``proposal`` payload carries the new revision every
later ``decision_for`` must freeze its evidence against.

Card 20 adds on top: ``start_candidate_shadows`` queues up to four
distinct-prompt items under one mutation (input_key
``candidate-shadow:{proposal_id}:{mutation_id}:{index}``), the cap counts the
whole batch before any write, ``list_methods`` carries ``default_head``, the
package-proposal detail carries ``candidate_shadow_claims`` (config-only
detail keeps its card-17 pinned key set) and ``POST /investor/methods/
proposals`` accepts a ``package_yaml`` candidate.
"""

from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
import yaml
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.methods import (
    ActivateGlobal,
    CandidateShadowBatchRequest,
    CandidateShadowItem,
    CandidateShadowRequest,
    CandidateShadowResultRequest,
    MethodService,
)
from investment_agent.investor.publication import FIXED_METHOD
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.investor import MethodArtifact
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.test_method_evolution import (
    decision_for,
    dimension_gap_candidate,
    proposal_for,
    register_candidate_shadow,
    replay_samples,
)
from tests.unit.test_candidate_shadow import (
    release_fixture_budget,
    run_shadow,
    shadow_block_model,
    shadow_pass_model,
    shadow_plan,
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


# --------------------------------------------------------------------------- #
# Card 20: batch initiation + workbench API additions
# --------------------------------------------------------------------------- #


async def start_batch_under(settings, proposal_id, items, *, mutation=None):
    """``start_candidate_shadows`` with caller-supplied Settings; items are
    (prompt, expected) pairs in submission order."""
    request = CandidateShadowBatchRequest(mutation_id=mutation or uuid4(), proposal_id=UUID(proposal_id), items=tuple(CandidateShadowItem(prompt=prompt, expected=expected) for prompt, expected in items))
    async with session_scope() as session:
        return await MethodService(session).start_candidate_shadows(request, base=settings)


@asynccontextmanager
async def workbench_client():
    """The card-17 workbench app fixture: read-only routes over the same db."""
    from investment_agent.api.app import create_app
    from investment_agent.api.auth import get_bootstrap_settings
    from tests.support.conversations import base

    app = create_app()
    app.dependency_overrides[get_bootstrap_settings] = lambda: base()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"X-API-Key": "fixture-app"}) as client:
        yield client


@pytest.mark.asyncio
async def test_batch_queues_distinct_shadows_replays_and_registers_each(conversation_db):
    # Card 20 §1: one mutation queues two positives plus one negative as three
    # real tasks with three claims and pairwise-distinct input_keys; the same
    # mutation replays the original receipt, and every task registers its own
    # candidate_shadow_result afterwards.
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    items = (("研究这家公司的现金持续性", "pass"), ("核查价格预期差是否有证据", "pass"), ("故意不给预期差证据", "block"))
    mutation = uuid4()
    result = await start_batch_under(shadow_settings(), proposal["id"], items, mutation=mutation)
    assert result["status"] == "queued" and len(result["task_ids"]) == 3 and len(result["claims"]) == 3
    assert [claim["expected"] for claim in result["claims"]] == [expected for _, expected in items]
    assert await start_batch_under(shadow_settings(), proposal["id"], items, mutation=mutation) == result  # replay returns the original task ids
    async with session_scope() as session:
        claims = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "candidate_shadow_claim"))).all()
        mine = [item.payload for item in claims if item.payload["proposal_id"] == proposal["id"]]
        assert len(mine) == 3
        assert {claim["task_id"] for claim in mine} == set(result["task_ids"])
        tasks = (await session.scalars(select(AgentTask).where(AgentTask.input_key.like(f"candidate-shadow:{proposal['id']}:{mutation}:%")))).all()
        assert len(tasks) == 3 and len({task.input_key for task in tasks}) == 3
        assert {task.input_key for task in tasks} == {f"candidate-shadow:{proposal['id']}:{mutation}:{index}" for index in range(3)}
    # Each queued task really runs and registers its own result, in order.
    plan = shadow_plan(weak)
    revision = proposal["revision"]
    for task_id, (_, expected) in zip(result["task_ids"], items, strict=True):
        await release_fixture_budget()
        if expected == "pass":
            finished = await run_shadow(UUID(task_id), FunctionModel(shadow_pass_model(plan)))
            assert finished.status.value == "succeeded"
        else:
            finished = await run_shadow(UUID(task_id), FunctionModel(shadow_block_model(plan)), shadow_settings(agent_retries=0))
            assert finished.status.value == "partial"
        async with session_scope() as session:
            registered = await MethodService(session).candidate_shadow_result(
                CandidateShadowResultRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), task_id=UUID(task_id), expected_revision=revision))
        assert registered["candidate_shadow"]["matched"]
        revision = registered["proposal"]["revision"]


@pytest.mark.asyncio
async def test_batch_over_cap_is_refused_whole_before_any_write(conversation_db):
    # Card 20 §1: with the cap at 3 a four-item batch is refused atomically -
    # no claim artifact and no task exist afterwards.
    _, _, subject, _, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    gate = shadow_settings(method_candidate_shadow_max_per_proposal=3)
    items = tuple((f"研究主题 {index}", "pass" if index % 2 == 0 else "block") for index in range(4))
    with pytest.raises(InvestorError) as excinfo:
        await start_batch_under(gate, proposal["id"], items)
    assert excinfo.value.detail.code == "invalid_reference"
    message = excinfo.value.detail.message
    assert "上限 3" in message and "4" in message
    async with session_scope() as session:
        claims = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "candidate_shadow_claim"))).all()
        assert not [item for item in claims if item.payload["proposal_id"] == proposal["id"]]
        tasks = (await session.scalars(select(AgentTask).where(AgentTask.input_key.like(f"candidate-shadow:{proposal['id']}%")))).all()
        assert not tasks


@pytest.mark.asyncio
async def test_list_methods_carries_default_head(conversation_db):
    # Card 20 §2: ``default_head`` is None until activate-global moves the
    # global default, then projects the stored revision and v2 binding.
    async with workbench_client() as client:
        assert (await client.get("/investor/methods")).json()["default_head"] is None
    async with session_scope() as session:
        svc = MethodService(session)
        saved = await svc.save_package(dimension_gap_candidate(), source="test")
        await svc.activate_global(ActivateGlobal(mutation_id=uuid4(), package_ref=ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]), expected_revision=0, reason="启用已评审基线包"), actor="authenticated_user")
    async with workbench_client() as client:
        page = (await client.get("/investor/methods")).json()
    head = page["default_head"]
    assert head is not None and head["revision"] == 1
    assert head["binding"]["schema_version"] == 2
    assert head["binding"]["package_ref"]["id"] == saved["artifact_id"]


@pytest.mark.asyncio
async def test_package_detail_carries_candidate_shadow_claims(conversation_db):
    # Card 20 §2: package-proposal detail gains ``candidate_shadow_claims`` -
    # every started claim with prompt truncated to 200 chars, ``registered``
    # flipping per task only after its result registers.
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    long_prompt = "研究" * 130  # 260 chars: the display bound keeps the first 200
    items = ((long_prompt, "pass"), ("只研究预期差证据", "block"))
    started = await start_batch_under(shadow_settings(), proposal["id"], items)
    pass_task, block_task = (claim["task_id"] for claim in started["claims"])
    async with workbench_client() as client:
        detail = (await client.get("/investor/methods/proposals/" + proposal["id"])).json()
    claims = detail["candidate_shadow_claims"]
    assert claims["total"] == 2 and not claims["truncated"]
    entries = {item["task_id"]: item for item in claims["items"]}
    assert not any(item["registered"] for item in claims["items"])  # nothing registered yet
    assert entries[pass_task]["expected"] == "pass" and entries[pass_task]["prompt"] == long_prompt[:200]
    assert entries[block_task]["expected"] == "block" and entries[block_task]["prompt"] == "只研究预期差证据"
    await release_fixture_budget()
    finished = await run_shadow(UUID(pass_task), FunctionModel(shadow_pass_model(shadow_plan(weak))))
    assert finished.status.value == "succeeded"
    async with session_scope() as session:
        await MethodService(session).candidate_shadow_result(
            CandidateShadowResultRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), task_id=UUID(pass_task), expected_revision=proposal["revision"]))
    async with workbench_client() as client:
        detail = (await client.get("/investor/methods/proposals/" + proposal["id"])).json()
    entries = {item["task_id"]: item for item in detail["candidate_shadow_claims"]["items"]}
    assert entries[pass_task]["registered"] is True
    assert entries[block_task]["registered"] is False


@pytest.mark.asyncio
async def test_config_only_detail_key_set_stays_pinned(conversation_db):
    # Card 17 regression, card 20 scope: config-only proposals keep the exact
    # detail shape - the claims block never appears for them.
    from tests.support.methods import samples

    subject, _, _, failure, _, _, _ = await samples()
    proposal = await proposal_for(subject, failure)
    async with workbench_client() as client:
        response = await client.get("/investor/methods/proposals/" + proposal["id"])
        assert response.status_code == 200
        detail = response.json()
    assert set(detail) == {"proposal", "history_counts", "detail_loaded", "evaluations", "shadows", "prospective", "artifact_hashes", "head_revision", "current_binding", "history_note", "evidence_hash", "approval_summary"}


@pytest.mark.asyncio
async def test_package_yaml_entry_creates_package_proposal_and_rejects_bad_yaml(conversation_db):
    # Card 20 §3: ``package_yaml`` creates the package proposal with config
    # filled from the parsed discipline; a config-less, yaml-less request and
    # unparseable/non-mapping YAML stay 422.
    _, _, subject, _, failure, _ = await replay_samples()
    definition = dimension_gap_candidate()
    body = {"mutation_id": str(uuid4()), "review_id": failure["id"], "listing_ref": subject.model_dump(mode="json"),
            "old_method": FIXED_METHOD.model_dump(mode="json"), "error_pattern": "Price view moved without an expectations gap",
            "summary": "Require the expectations gap before directional price views",
            "package_yaml": yaml.safe_dump(definition.model_dump(mode="json"))}
    async with workbench_client() as client:
        created = await client.post("/investor/methods/proposals", json=body)
        assert created.status_code == 200, created.text
        proposal = created.json()
        assert proposal["request"]["package_ref"]["kind"] == "package"  # the candidate froze through the stored artifact
        assert proposal["request"]["package"]["package_key"] == definition.package_key
        no_config = {key: value for key, value in body.items() if key != "package_yaml"}
        assert (await client.post("/investor/methods/proposals", json=no_config)).status_code == 422  # config still required without yaml
        unparseable = await client.post("/investor/methods/proposals", json={**body, "mutation_id": str(uuid4()), "package_yaml": ":\n\t- not yaml ["})
        assert unparseable.status_code == 422 and unparseable.json()["detail"]["code"] == "invalid_reference"
        non_mapping = await client.post("/investor/methods/proposals", json={**body, "mutation_id": str(uuid4()), "package_yaml": "- a\n- b\n"})
        assert non_mapping.status_code == 422 and non_mapping.json()["detail"]["code"] == "invalid_reference"

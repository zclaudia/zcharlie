"""Card 14: candidate-package offline shadow (§7.2).

The candidate FunctionModel prompt carries the rendered candidate method, the
fixed output contract and the frozen action summary (never three boolean
switches); the enforcing replay boundary rebuilds frozen content plus only the
new plan_bindings/step_results metadata and intercepts hallucinated action
ids / output refs deterministically; every applicable sample is judged by the
package-level ``validate_final`` against its role expectation (failure blocked,
in-scope counterexample passing or failing only on the offline not-replayable
order item). The config-only shadow path stays unchanged
(``tests/test_method_evolution.py`` runs as the regression).
"""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select

from investment_agent.agent.company_plan import reuse_question_plan
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.method_receipt import (
    VALIDATOR_V1,
    TaskMethodReceipt,
    build_receipt,
    render_method,
)
from investment_agent.agent.research import (
    MethodCompanyStudyOutput,
    MethodPlannedProgress,
    ResearchPolicy,
    registrations,
    validate_progress,
)
from investment_agent.agent.tasks import TaskClaim, TaskService
from investment_agent.api.auth import get_bootstrap_settings
from investment_agent.api.routes.methods import (
    _candidate_progress_replay,
    _offline_progress_replay,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.investor.methods import (
    CANDIDATE_OUTPUT_CONTRACT,
    CANDIDATE_SHADOW_FORBIDDEN_TOOLS,
    CandidateShadowRequest,
    CandidateShadowResultRequest,
    MethodBinding,
    MethodConfig,
    MethodService,
    ShadowRequest,
    _candidate_shadow_replay,
    _frozen_ref_identities,
    run_offline_shadow,
    task_method,
    task_method_receipt,
)
from investment_agent.investor.publication import FIXED_METHOD
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask, ExecutionGrantRow, MessageRow
from investment_agent.storage.models.investor import InvestorCase, InvestorMutation, MethodArtifact
from investment_agent.storage.models.publication import PublicationArtifact
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.pipeline_run import RunLease
from tests.support.conversations import (
    base,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.support.research import full_progress
from tests.test_method_evolution import (
    dimension_gap_candidate,
    evaluate_package,
    explanation_review,
    package_request,
    replay_samples,
)

BLOCKING_STEPS = ("falsifiable_plan", "dimension_assessment", "expectations_gap")


class _Part:
    def __init__(self, content: object, kind: str) -> None:
        self.content, self.part_kind = content, kind


class _Message:
    def __init__(self, *parts: _Part) -> None:
        self.parts = parts


class _Info:
    class _Tool:
        name = "candidate_output"

    output_tools = [_Tool()]


def _prompt(prompt: dict[str, Any]) -> list[_Message]:
    return [_Message(_Part("instructions", "system-prompt"), _Part(json.dumps(prompt), "user-prompt"))]


def candidate_model(seen: list[dict[str, Any]], *, fabricated: bool = False):
    """Stub candidate model: echo frozen content, add metadata citing real
    frozen identities. With ``fabricated`` the in-scope failure sample cites a
    made-up action id and the in-scope success sample a made-up output ref."""
    def model(messages: Any, info: Any) -> ModelResponse:
        prompt = next(p.content for m in reversed(messages) for p in m.parts if getattr(p, "part_kind", None) == "user-prompt")
        payload = json.loads(prompt)
        seen.append({key: payload.get(key) for key in ("candidate_rendered_method", "candidate_output_contract", "frozen_actions")})
        progress = payload["frozen_progress"]
        research = progress.get("kind") == "company_research"
        fake_action = fabricated and research and not progress.get("answered")
        fake_ref = fabricated and research and bool(progress.get("answered"))
        actions = [action["id"] for action in payload.get("frozen_actions", [])]
        metadata = {
            "plan_bindings": [{"evidence_step_index": 0, "method_step_id": step} for step in BLOCKING_STEPS],
            "step_results": [{
                "method_step_id": step,
                "evidence_step_indices": (0,),
                "status": "satisfied",
                "action_ids": (str(uuid4()),) if fake_action else tuple(actions[:1]),
                "output_refs": ({"kind": "evidence", "id": str(uuid4()), "revision": 1},) if fake_ref else (),
                "hypothesis_indices": (0,) if progress.get("hypotheses") else (),
            } for step in BLOCKING_STEPS],
        }
        return ModelResponse([ToolCallPart(info.output_tools[0].name, {**progress, **metadata})])
    return model


async def shadowed_candidate():
    """One proposal-frozen package candidate through a passed evaluation, so the
    proposal sits in exactly the status the offline shadow requires (§7.2)."""
    _, _, subject, _, failure, raised = await replay_samples()
    candidate = dimension_gap_candidate()
    async with session_scope() as session:
        proposal = await MethodService(session).create(package_request(uuid4(), failure, subject, candidate))
    outside = await explanation_review()
    result = await evaluate_package(proposal, failure, raised, outside)
    assert result["evaluation"]["passed"]
    proposal = result["proposal"]
    request = ShadowRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), expected_revision=proposal["revision"], evaluation_id=UUID(result["evaluation"]["id"]))
    return candidate, request, failure, raised, outside


@pytest.mark.asyncio
async def test_package_offline_shadow_prompts_judges_and_passes(conversation_db):
    candidate, request, failure, raised, outside = await shadowed_candidate()
    seen: list[dict[str, Any]] = []
    shadow = await run_offline_shadow(session_scope, request, FunctionModel(candidate_model(seen)))
    assert shadow["passed"] is True and shadow["candidate_shadow"] is True
    assert shadow["quality"] == "offline_FunctionModel_engineering_only"
    assert shadow["rendered_method_hash"] == hashlib.sha256(render_method(candidate).encode("utf-8")).hexdigest()
    cases = {case["review_id"]: case for case in shadow["cases"]}
    # The failure sample is rejected by the package-level validate_final on the
    # strengthened requirement the frozen content cannot satisfy.
    failure_case = cases[str(failure["id"])]
    assert failure_case["candidate_conclusion"] == "block" and failure_case["matched"] and failure_case["applicable"]
    assert any("method_step_output_required" in error and "expectations_gap" in error for error in failure_case["errors"])
    # The in-scope success satisfies every recomputable requirement; only the
    # order item over NULL-sequence historical actions is not replayable, and
    # it does not count against a counterexample (§7.1).
    in_scope = cases[str(raised["id"])]
    assert in_scope["candidate_conclusion"] == "not_replayable_only" and in_scope["matched"]
    assert any("method_step_order_violation" in error and "缺少已提交动作顺序" in error for error in in_scope["errors"])
    # The out-of-scope sample keeps its own method and baseline pass semantics.
    outside_case = cases[str(outside["id"])]
    assert outside_case["candidate_conclusion"] == "pass" and outside_case["matched"] and not outside_case["applicable"]
    # §7.2: the prompt carries rendered instructions, the fixed contract and
    # the per-sample action summary - not three boolean switches.
    assert len(seen) == 3
    for captured in seen:
        assert captured["candidate_rendered_method"] == render_method(candidate)
        assert captured["candidate_output_contract"] == CANDIDATE_OUTPUT_CONTRACT
        assert captured["frozen_actions"] and all({"id", "tool", "committed_sequence"} <= set(action) for action in captured["frozen_actions"])
    assert all(case["output"]["usage"]["requests"] == 1 for case in shadow["cases"])
    async with session_scope() as session:
        stored = (await MethodService(session).proposal(request.proposal_id)).payload
        assert stored["shadow_ids"] == [shadow["id"]]
    # Stable mutation replay never repeats an uncertain model call.
    assert await run_offline_shadow(session_scope, request, FunctionModel(lambda m, i: pytest.fail("lost ack must not call again"))) == shadow


@pytest.mark.asyncio
async def test_package_offline_shadow_intercepts_fabricated_identities(conversation_db):
    _, request, failure, raised, _ = await shadowed_candidate()
    shadow = await run_offline_shadow(session_scope, request, FunctionModel(candidate_model([], fabricated=True)))
    # Deterministic interception: the fabricated run is stored as failing
    # engineering evidence and can never enter the record as a pass.
    assert shadow["passed"] is False and shadow["candidate_shadow"] is True
    cases = {case["role"]: case for case in shadow["cases"] if case["applicable"]}
    assert cases["failure"]["candidate_conclusion"] == "model_error" and not cases["failure"]["matched"]
    assert "不存在的动作 id" in cases["failure"]["errors"][0]
    assert cases["counterexample"]["candidate_conclusion"] == "model_error" and not cases["counterexample"]["matched"]
    assert "不存在的 output ref" in cases["counterexample"]["errors"][0]
    async with session_scope() as session:
        stored = await MethodService(session).artifact(UUID(shadow["id"]), "shadow")
        assert stored["candidate_shadow"] is True and stored["passed"] is False


@pytest.mark.asyncio
async def test_candidate_replay_boundary_rebuilds_frozen_content_and_intercepts(conversation_db):
    real_action, real_ref = str(uuid4()), {"kind": "evidence", "id": str(uuid4()), "revision": 2}
    progress = {"question": "q", "kind": "explanation", "answered": False, "counter_refs": (real_ref,)}
    frozen = {"progress": progress, "actions": [{"id": real_action, "tool": "get_snapshot", "committed_sequence": None}],
        "action_ids": {real_action}, "refs": _frozen_ref_identities(progress)}
    metadata = {"plan_bindings": (), "step_results": ({"method_step_id": "expectations_gap", "evidence_step_indices": (),
        "status": "satisfied", "action_ids": (real_action,), "output_refs": (real_ref,), "hypothesis_indices": ()},)}

    def inner(messages: Any, info: Any) -> ModelResponse:
        prompt = json.loads(next(p.content for m in reversed(messages) for p in m.parts if getattr(p, "part_kind", None) == "user-prompt"))
        return ModelResponse([ToolCallPart(info.output_tools[0].name, {**prompt["frozen_progress"], "answered": True, **metadata})])

    async def async_inner(messages: Any, info: Any) -> ModelResponse:
        return inner(messages, info)

    replayed = _candidate_shadow_replay(inner, frozen)(_prompt({"frozen_progress": progress}), _Info())
    payload = replayed.parts[0].args
    assert payload["answered"] is False  # the model can never alter a frozen field
    assert payload["step_results"][0]["action_ids"] == [real_action] and payload["step_results"][0]["output_refs"] == [dict(real_ref, field_path=None)]
    replayed_async = await _candidate_shadow_replay(async_inner, frozen)(_prompt({"frozen_progress": progress}), _Info())
    assert replayed_async.parts[0].args == payload

    def fabricating(messages: Any, info: Any) -> ModelResponse:
        made_up = {"plan_bindings": (), "step_results": (dict(metadata["step_results"][0], action_ids=(str(uuid4()),)),)}
        return ModelResponse([ToolCallPart(info.output_tools[0].name, {**progress, **made_up})])

    with pytest.raises(InvestorError) as excinfo:
        _candidate_shadow_replay(fabricating, frozen)(_prompt({"frozen_progress": progress}), _Info())
    assert excinfo.value.detail.code == "invalid_reference" and "动作 id" in excinfo.value.detail.message

    def foreign_ref(messages: Any, info: Any) -> ModelResponse:
        made_up = {"plan_bindings": (), "step_results": (dict(metadata["step_results"][0], output_refs=({"kind": "evidence", "id": str(uuid4()), "revision": 1},)),)}
        return ModelResponse([ToolCallPart(info.output_tools[0].name, {**progress, **made_up})])

    with pytest.raises(InvestorError) as excinfo:
        _candidate_shadow_replay(foreign_ref, frozen)(_prompt({"frozen_progress": progress}), _Info())
    assert "output ref" in excinfo.value.detail.message


def test_route_candidate_replay_and_dispatch_cite_only_frozen_identities():
    frozen = {"question": "q", "kind": "company_research", "evidence_steps": ("read",),
        "hypotheses": ({"alternative": "a"},), "counter_refs": ({"kind": "evidence", "id": str(uuid4()), "revision": 3},)}
    actions = [{"id": str(uuid4()), "tool": "get_snapshot", "committed_sequence": None}]
    prompt = {"frozen_progress": frozen, "candidate_rendered_method": render_method(dimension_gap_candidate()),
        "candidate_output_contract": "contract", "frozen_actions": actions}
    response = _candidate_progress_replay(_prompt(prompt), _Info())
    (part,) = response.parts
    assert part.tool_name == "candidate_output"
    replayed = part.args
    assert replayed["question"] == "q"  # frozen content preserved verbatim
    steps = {result["method_step_id"] for result in replayed["step_results"]}
    assert steps == set(BLOCKING_STEPS)
    assert {binding["method_step_id"] for binding in replayed["plan_bindings"]} == steps
    for result in replayed["step_results"]:
        assert set(result["action_ids"]) <= {action["id"] for action in actions}
        assert result["output_refs"] == tuple(ref for ref in frozen["counter_refs"] if ref["kind"] == "evidence")
        assert result["hypothesis_indices"] == (0,)
    # The dispatch sends candidate prompts to the candidate replay and keeps
    # every other prompt on the exact frozen-progress echo.
    assert _offline_progress_replay(_prompt(prompt), _Info()).parts[0].args == replayed
    legacy = {"frozen_progress": frozen, "method_config": {}}
    echoed = json.loads(json.dumps(frozen))  # the prompt is JSON: tuples arrive as lists
    assert _offline_progress_replay(_prompt(legacy), _Info()).parts[0].args == echoed
    with pytest.raises(InvestorError):
        _offline_progress_replay([_Message(_Part("not json", "user-prompt"))], _Info())


# --------------------------------------------------------------------------- #
# Card 15: candidate-package prospective shadow (§7.3)
# --------------------------------------------------------------------------- #


async def shadow_proposal():
    """A package proposal through a passed evaluation, plus its fixture pieces."""
    candidate, evidence, subject, weak, failure, raised = await replay_samples()
    async with session_scope() as session:
        proposal = await MethodService(session).create(package_request(uuid4(), failure, subject, dimension_gap_candidate()))
    outside = await explanation_review()
    result = await evaluate_package(proposal, failure, raised, outside)
    assert result["evaluation"]["passed"]
    return candidate, evidence, subject, weak, result["proposal"]


async def shadow_ready_mandate(subject, *, exploration_scope=True):
    """The existing fixture mandate enabled for background shadow admission."""
    from investment_agent.investor.common import Mutation

    async with session_scope() as session:
        foundation = InvestorService(session)
        mandate = await foundation.get_mandate("local_owner")
        auth = mandate.authorization.model_copy(update={
            "background_enabled": True,
            "exploration_scope_refs": (subject,) if exploration_scope else ()})
        await foundation.revise_mandate("local_owner", mandate.model_copy(update={"authorization": auth, "revision": mandate.revision + 1}), Mutation(expected_revision=mandate.revision, idempotency_key=uuid4()))


def shadow_settings(**overrides):
    return base(investor_agent_background_enabled=True, **overrides)


async def release_fixture_budget():
    """Earlier fixture runs share today's budget day bucket with unpriced
    entries (the shared ``base()`` carries no verified prices); the shadow's
    own known-cost check over the mandate caps would fail closed on them.
    Releasing the fixture entries keeps the shadow task's priced accounting
    authoritative without touching production code."""
    from sqlalchemy import update

    from investment_agent.storage.models.agent import BudgetEntry

    async with session_scope() as session:
        await session.execute(update(BudgetEntry).where(BudgetEntry.state != "released").values(state="released"))


async def start_shadow(proposal_id, *, expected, mutation=None):
    request = CandidateShadowRequest(mutation_id=mutation or uuid4(), proposal_id=UUID(proposal_id), prompt="研究这家公司的现金持续性与价格预期差", expected=expected)
    async with session_scope() as session:
        return await MethodService(session).start_candidate_shadow(request, base=shadow_settings())


async def run_shadow(task_id, model, settings=None):
    """One real worker round over the queued agent_research shadow run."""
    from investment_agent.agent.executor import AgentExecutor

    selected = settings or shadow_settings()
    executor = AgentExecutor(selected, model=model, registrations=registrations(), specification=ResearchPolicy(selected).specification())
    worker = ResearchWorker(settings=selected, worker_id="candidate-shadow-fixture", handlers=[AgentJobHandler("agent_research", executor)])
    await worker.run_once()
    async with session_scope() as session:
        return await TaskService(session).get_task(task_id)


def shadow_plan(weak):
    """First committed plan mapping every blocking step onto the evidence step."""
    return MethodPlannedProgress.model_validate({**weak.model_dump(mode="json"),
        "plan_bindings": ({"evidence_step_index": 0, "method_step_id": "falsifiable_plan"},
                          {"evidence_step_index": 0, "method_step_id": "dimension_assessment"},
                          {"evidence_step_index": 0, "method_step_id": "expectations_gap"})})


def raised_final(plan, saved):
    """Final settlement citing the real committed save actions in commit order."""
    from investment_agent.investor.contracts import DimensionAssessment

    return MethodPlannedProgress.model_validate({**plan.model_dump(mode="json"), "answered": True,
        "dimensions": tuple(DimensionAssessment(dimension=d.dimension, status="qualitative" if d.dimension == "price_expectations" else d.status, applicable=d.applicable, reason=d.reason, refs=d.refs) for d in plan.dimensions),
        "step_results": ({"method_step_id": "falsifiable_plan", "evidence_step_indices": (0,), "status": "satisfied", "hypothesis_indices": (0,), "action_ids": [saved[0]]},
                         {"method_step_id": "dimension_assessment", "evidence_step_indices": (0,), "status": "satisfied", "action_ids": [saved[0]]},
                         {"method_step_id": "expectations_gap", "evidence_step_indices": (0,), "status": "satisfied", "action_ids": [saved[-1]]})})


def shadow_pass_model(plan):
    """Real-run stub: plan, a later distinct progress save, then a legal settlement
    citing the actually committed action ids in dependency order."""
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        saved = [UUID(p.content["artifact_ref"]["id"]) for p in returns if p.tool_name == "save_research_progress"]
        if not saved:
            return ModelResponse([ToolCallPart("save_research_progress", {"business": plan.model_dump(mode="json")})])
        if len(saved) == 1:
            progressed = plan.model_copy(update={"completed_steps": (*plan.completed_steps, "Checked cash durability against the price expectation")})
            return ModelResponse([ToolCallPart("save_research_progress", {"business": progressed.model_dump(mode="json")})])
        return ModelResponse([TextPart(MethodCompanyStudyOutput(progress=raised_final(plan, saved)).model_dump_json())], usage=RequestUsage(input_tokens=20, output_tokens=10))
    return model


def shadow_block_model(plan):
    """Final progress missing every blocking step result: validate_final must reject."""
    def model(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not any(p.tool_name == "save_research_progress" for p in returns):
            return ModelResponse([ToolCallPart("save_research_progress", {"business": plan.model_dump(mode="json")})])
        final = MethodPlannedProgress.model_validate({**plan.model_dump(mode="json"), "answered": True})
        return ModelResponse([TextPart(MethodCompanyStudyOutput(progress=final).model_dump_json())], usage=RequestUsage(input_tokens=20, output_tokens=10))
    return model


@pytest.mark.asyncio
async def test_candidate_shadow_real_worker_pass_registers_coverage(conversation_db):
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    started = await start_shadow(proposal["id"], expected="pass")
    assert started["status"] == "queued"
    task_id = UUID(started["task_id"])
    async with session_scope() as session:
        frozen = await task_method_receipt(session, task_id)
        binding = await task_method(session, task_id)
        grant = ExecutionGrant.model_validate((await session.get(ExecutionGrantRow, str(task_id))).payload)
    assert frozen is not None and frozen.shadow == "candidate_package" and frozen.validator_version == VALIDATOR_V1
    assert frozen.candidate_base_method_ref == FIXED_METHOD  # = proposal old_method
    assert binding.schema_version == 2
    assert binding.package_ref == ObjectRef.model_validate(proposal["request"]["package_ref"])
    assert binding.package_hash == proposal["request"]["content_hash"]
    assert grant.mode == "background" and grant.source == "authorized_background"
    assert grant.scope_refs == (subject,) and grant.research_kind == "company_research"
    assert not set(grant.tool_names) & CANDIDATE_SHADOW_FORBIDDEN_TOOLS
    assert "prepare_company_view" not in grant.tool_names and "publish_recommendation" not in grant.tool_names
    await release_fixture_budget()
    task = await run_shadow(task_id, FunctionModel(shadow_pass_model(shadow_plan(weak))))
    assert task.status.value == "succeeded"  # package validation completed the shadow
    request = CandidateShadowResultRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), task_id=task_id, expected_revision=proposal["revision"])
    async with session_scope() as session:
        result = await MethodService(session).candidate_shadow_result(request)
        assert await MethodService(session).candidate_shadow_result(request) == result
    artifact = result["candidate_shadow"]
    assert artifact["expected"] == "pass" and artifact["outcome"] == "passed" and artifact["matched"]
    covered = set(artifact["requirement_ids"])
    assert covered == set(result["proposal"]["requires_candidate_shadow"])
    assert {"blocking:expectations_gap", "results:expectations_gap", "actions:expectations_gap", "depends_on:expectations_gap:dimension_assessment"} <= covered
    assert artifact["rendered_method_hash"] == proposal["request"]["rendered_method_hash"]
    assert result["proposal"]["candidate_shadow_ids"] == [artifact["id"]]


@pytest.mark.asyncio
async def test_candidate_shadow_block_registers_counterexample(conversation_db):
    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    started = await start_shadow(proposal["id"], expected="block")
    task_id = UUID(started["task_id"])
    await release_fixture_budget()
    task = await run_shadow(task_id, FunctionModel(shadow_block_model(shadow_plan(weak))), shadow_settings(agent_retries=0))
    assert task.status.value == "partial"  # validate_final rejected the missing results
    async with session_scope() as session:
        result = await MethodService(session).candidate_shadow_result(CandidateShadowResultRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), task_id=task_id, expected_revision=proposal["revision"]))
    artifact = result["candidate_shadow"]
    assert artifact["expected"] == "block" and artifact["outcome"] == "blocked" and artifact["matched"]
    assert artifact["requirement_ids"] == []  # a counterexample covers nothing
    assert any("method_step_required" in error for error in artifact["errors"])
    assert result["proposal"]["candidate_shadow_ids"] == [artifact["id"]]


@pytest.mark.asyncio
async def test_candidate_shadow_has_no_formal_side_effects_or_plan_reuse(conversation_db):
    candidate, evidence, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    started = await start_shadow(proposal["id"], expected="block")
    task_id = UUID(started["task_id"])
    async with session_scope() as session:
        before = [await session.scalar(select(func.count()).select_from(t)) for t in (PublicationArtifact, InvestorMutation, InvestorCase, WatchlistItem)]
        case = await session.scalar(select(InvestorCase))
        case_payload = case.payload
    await release_fixture_budget()
    task = await run_shadow(task_id, FunctionModel(shadow_block_model(shadow_plan(weak))), shadow_settings(agent_retries=0))
    assert task.status.value == "partial"
    async with session_scope() as session:
        after = [await session.scalar(select(func.count()).select_from(t)) for t in (PublicationArtifact, InvestorMutation, InvestorCase, WatchlistItem)]
        assert before == after  # no publication artifact, mutation, case or watchlist change
        assert (await session.get(InvestorCase, case.id)).payload == case_payload  # case head unchanged
    # The real worker leaves no assistant/publication trace: the shadow task owns
    # no conversation message and never publishes.
    # §7.3: formal plan reuse never selects the shadow task even when every
    # other selection condition (same scope/kind/method, fresh, failed) matches.
    continuation = await submit(await new_conversation(), message(text="继续研究现金问题", context_refs=(subject,)))
    async with session_scope() as session:
        current_message = await session.get(MessageRow, str(continuation.task.message_ref.id))
        session.add(MessageRow(id=str(uuid4()), conversation_id=current_message.conversation_id, client_message_id=str(uuid4()), completion_key=None, input_hash="fixture", sequence=0,
            payload={"role": "user", "text": "继续研究现金问题", "task_ref": {"kind": "task", "id": str(task_id), "revision": 1}}))
        grant = ExecutionGrant.model_validate((await session.get(ExecutionGrantRow, str(task_id))).payload)
        claim = TaskClaim(task_id=continuation.task.id, attempt_id=uuid4(), lease=RunLease(run_id=1, worker_id="fixture", attempt=1))
        assert await reuse_question_plan(session, claim, grant, current_message, FIXED_METHOD, base=base()) is None
    # The same evidence the shadow read still re-verifies normally for a formal task.
    formal = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,)))
    async with session_scope() as session:
        text, refs = await validate_progress(session, formal.task.id, full_progress(candidate.thesis.assessed_dimensions, fact_refs=(evidence,), answered=False))
    assert any(str(item.id) == str(evidence.id) for item in refs)
    async with session_scope() as session:
        result = await MethodService(session).candidate_shadow_result(CandidateShadowResultRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), task_id=task_id, expected_revision=proposal["revision"]))
    assert result["candidate_shadow"]["outcome"] == "blocked" and result["candidate_shadow"]["matched"]


@pytest.mark.asyncio
async def test_candidate_shadow_initiation_validates_state_scope_and_tools(conversation_db):
    _, _, subject, _, failure, raised = await replay_samples()
    async with session_scope() as session:
        proposal = await MethodService(session).create(package_request(uuid4(), failure, subject, dimension_gap_candidate()))
    # 1. Not yet status="shadow": initiation refuses before any task exists.
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="已通过评估的包提案"):
            await start_shadow(proposal["id"], expected="pass")
    outside = await explanation_review()
    result = await evaluate_package(proposal, failure, raised, outside)
    proposal = result["proposal"]
    # 2. A config-only proposal never qualifies even in status="shadow". This
    # runs real fixture tasks while the mandate still carries no budgets; the
    # candidate-shadow steps below enable them and every run after that must
    # price its own cost (the existing daily/task cost configuration).
    from tests.test_method_evolution import evaluated, proposal_for
    config_only, _ = await evaluated(await proposal_for(subject, failure), failure, raised)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="已通过评估的包提案"):
            await start_shadow(config_only["id"], expected="pass")
    # 3. Scope mismatch: the mandate's background scope excludes the listing.
    await shadow_ready_mandate(subject, exploration_scope=False)
    with pytest.raises(ExecutionDeniedError) as excinfo:
        await start_shadow(proposal["id"], expected="pass")
    assert "scope_revoked" in str(excinfo.value)
    async with session_scope() as session:
        claims = (await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == "candidate_shadow_claim"))).all()
        assert not claims  # the denied initiation left no claim or task
        tasks = (await session.scalars(select(AgentTask).where(AgentTask.input_key.like("candidate-shadow%")))).all()
        assert not tasks
    # 4. With the mandate scope fixed, initiation succeeds, is idempotent per
    # mutation and freezes a grant without publication/portfolio tools.
    await shadow_ready_mandate(subject)
    started = await start_shadow(proposal["id"], expected="pass", mutation=uuid4())
    request = CandidateShadowRequest(mutation_id=uuid4(), proposal_id=UUID(proposal["id"]), prompt="研究这家公司的现金持续性与价格预期差", expected="pass")
    async with session_scope() as session:
        service = MethodService(session)
        assert await service.start_candidate_shadow(request, base=base(investor_agent_background_enabled=True)) == await service.start_candidate_shadow(request, base=base(investor_agent_background_enabled=True))
        grant = ExecutionGrant.model_validate((await session.get(ExecutionGrantRow, started["task_id"])).payload)
        assert not set(grant.tool_names) & {"prepare_company_view", "get_portfolio_context", "calculate_portfolio_options", "prepare_portfolio_plan"}


def test_receipt_without_shadow_keeps_exact_payload_shape():
    binding = MethodBinding(schema_version=2, method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
        config=MethodConfig(), package_ref=ObjectRef(kind="package", id=uuid4(), revision=1), package_hash="0" * 64)
    receipt = build_receipt(binding, dimension_gap_candidate(), validator_version=VALIDATOR_V1)
    dump = receipt.model_dump(mode="json")
    assert "shadow" not in dump and "candidate_base_method_ref" not in dump
    assert TaskMethodReceipt.model_validate(dump) == receipt


@pytest.mark.asyncio
async def test_candidate_shadow_routes_wire_through_the_service(conversation_db, monkeypatch, tmp_path):
    from investment_agent.api.app import create_app

    monkeypatch.setattr("investment_agent.web.routes.get_bootstrap_settings", lambda: shadow_settings())
    app = create_app(settings=shadow_settings())
    app.dependency_overrides[get_bootstrap_settings] = lambda: shadow_settings()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"X-API-Key": "fixture-app"}) as client:
        unknown = (await client.post(f"/investor/methods/proposals/{uuid4()}/candidate-shadow", json={"mutation_id": str(uuid4()), "prompt": "q", "expected": "pass"})).status_code
        assert unknown == 404  # reaches the service; the proposal does not exist
        overlong = (await client.post(f"/investor/methods/proposals/{uuid4()}/candidate-shadow", json={"mutation_id": str(uuid4()), "prompt": "x" * 3000, "expected": "pass"})).status_code
        assert overlong == 422  # wire contract bounds the prompt before the service
        result = (await client.post("/investor/methods/candidate-shadow-results", json={"mutation_id": str(uuid4()), "proposal_id": str(uuid4()), "task_id": str(uuid4()), "expected_revision": 1})).status_code
        assert result == 404

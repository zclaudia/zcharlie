"""Card 13: three-state historical replay of frozen reviews against a candidate (§7.1).

The replayable requirements (plan, hypotheses, dimension, counter_refs,
calculation, snapshot_field) are recomputed from saved progress fields, saved
outputs and committed action parameters. Plan-binding coverage, step-result
existence, action attribution, peer binding consistency and dependency order
have no historical metadata: they are ``not_replayable``, never a pass. The
evaluate orchestration over real frozen reviews is covered by
``tests/test_method_evolution.py``.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import update

from investment_agent.agent.execution import ExecutionGrant
from investment_agent.agent.method_receipt import TaskMethodReceipt
from investment_agent.agent.method_validation import (
    MethodValidationContext,
    replay_context,
    replay_requirements,
    replay_sample_state,
)
from investment_agent.agent.research import ResearchHypothesis, ResearchProgress
from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import DIMENSIONS, DimensionAssessment
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.method_packages import (
    LEGACY_BASELINE_DEFINITION,
    CalculationRequirement,
    CounterRefsRequirement,
    DimensionOutputRequirement,
    HypothesesRequirement,
    MethodPackage,
    MethodStep,
    PeerContextRequirement,
    PlanRequirement,
    SnapshotFieldRequirement,
    strictly_stronger_requirements,
)
from investment_agent.investor.methods import MethodBinding, MethodService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.support.methods import samples
from tests.support.publication import fixture
from tests.support.research import full_progress


def dimension_gap_step(**overrides: Any) -> MethodStep:
    """One blocking expectations-gap step; a strict §8 enhancement of the baseline."""
    fields: dict[str, Any] = dict(id="expectations_gap", title="Expectations gap", instruction_kind="expectations_gap",
        blocking=True, depends_on=("dimension_assessment",),
        required_outputs=(DimensionOutputRequirement(type="dimension", dimension="price_expectations", minimum_status="qualitative"),))
    fields.update(overrides)
    return MethodStep(**fields)


def candidate_with(step: MethodStep) -> Any:
    return LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (*LEGACY_BASELINE_DEFINITION.steps, step)})


def seven_dimensions(**statuses: str) -> tuple[DimensionAssessment, ...]:
    return tuple(DimensionAssessment(dimension=name, status=statuses.get(name, "missing"), applicable=True) for name in DIMENSIONS)


def offline_context(package: Any, committed_actions: tuple[dict[str, Any], ...] = ()) -> MethodValidationContext:
    """A context built without storage; only the pure requirement judging runs on it."""
    binding = MethodBinding(schema_version=2, method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
        config=package.discipline, package_ref=ObjectRef(kind="package", id=uuid4(), revision=1), package_hash="hash")
    receipt = TaskMethodReceipt(binding=binding, rendered_method="rendered", rendered_method_hash="0" * 64,
        renderer_version="2", validator_version="1", tool_family_members={})
    grant = ExecutionGrant(mode="interactive", source="authenticated_message", prompt="Research the company",
        tool_names=("get_snapshot", "capture_snapshot"), research_kind="company_research")
    return MethodValidationContext(task_id=uuid4(), binding=binding, package=package, receipt=receipt,
        grant=grant, session=None, first_plan=None, committed_actions=committed_actions, artifact_refs=())


async def replayed_for(package: Any, progress: Any, committed_actions: tuple[dict[str, Any], ...] = ()) -> tuple[dict[str, str], list[str]]:
    return await replay_requirements(offline_context(package, committed_actions), progress)


def capture_action(field: str) -> dict[str, Any]:
    return {"operation_id": uuid4(), "tool": "capture_snapshot",
        "parameters": {"tool": "capture_snapshot", "business": {"field": field}}, "result": {}, "committed_sequence": 1}


# --------------------------------------------------------------------------- #
# Not-replayable structural requirements (§7.1)
# --------------------------------------------------------------------------- #


async def test_blocking_coverage_results_actions_order_and_peer_are_not_replayable() -> None:
    package = candidate_with(dimension_gap_step())
    # The progress actually reaches the candidate's dimension minimum, so every
    # replayable output passes and the significant ids are exactly the
    # not-replayable structural set.
    progress = full_progress(seven_dimensions(price_expectations="qualitative"))
    replayed, significant = await replayed_for(package, progress)
    for requirement_id in ("blocking:expectations_gap", "results:expectations_gap", "actions:expectations_gap",
                           "depends_on:expectations_gap:dimension_assessment"):
        assert replayed[requirement_id] == "not_replayable"
        assert requirement_id in significant
    assert replayed["outputs:expectations_gap:dimension:price_expectations"] == "pass"
    # Peer binding consistency needs frozen step-result metadata: not replayable.
    peer = candidate_with(dimension_gap_step(required_outputs=(
        *dimension_gap_step().required_outputs, PeerContextRequirement(type="peer_context"))))
    replayed, _ = await replayed_for(peer, progress)
    assert replayed["outputs:expectations_gap:peer_context"] == "not_replayable"
    # The new step's own strengthening ids line up with the §8 partial order.
    assert set(significant) & set(strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, package))
    assert replay_sample_state(replayed) == "not_replayable"


async def test_non_blocking_steps_are_never_judged() -> None:
    optional = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (
        LEGACY_BASELINE_DEFINITION.steps[0],
        LEGACY_BASELINE_DEFINITION.steps[1].model_copy(update={"required_outputs": (
            HypothesesRequirement(type="hypotheses", min_count=99),)}),
        LEGACY_BASELINE_DEFINITION.steps[2])})
    replayed, _ = await replayed_for(optional, full_progress(seven_dimensions()))
    assert not any("counter_analysis" in requirement_id for requirement_id in replayed)


async def test_sample_state_precedence() -> None:
    assert replay_sample_state({"a": "pass", "b": "block", "c": "not_replayable"}) == "block"
    assert replay_sample_state({"a": "pass", "c": "not_replayable"}) == "not_replayable"
    assert replay_sample_state({"a": "pass"}) == "pass"


# --------------------------------------------------------------------------- #
# Replayable requirements recomputed from progress fields
# --------------------------------------------------------------------------- #


async def test_plan_hypotheses_and_dimension_replay_from_progress_fields() -> None:
    dimensions = seven_dimensions()
    raise_type = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": tuple(
        step.model_copy(update={"required_outputs": tuple(
            requirement.model_copy(update={"minimum_status": "evidenced"})
            if isinstance(requirement, DimensionOutputRequirement) and requirement.dimension == "price_expectations"
            else requirement for requirement in step.required_outputs)})
        if step.id == "dimension_assessment" else step for step in LEGACY_BASELINE_DEFINITION.steps)})
    complete = full_progress(dimensions)
    replayed, significant = await replayed_for(raise_type, complete)
    assert replayed["outputs:falsifiable_plan:plan"] == "pass"
    assert replayed["outputs:falsifiable_plan:hypotheses"] == "pass"
    assert replayed["outputs:dimension_assessment:dimension:price_expectations"] == "block"
    assert set(significant) == {"outputs:dimension_assessment:dimension:price_expectations", "dimension_min:price_expectations"}
    assert replay_sample_state(replayed) == "block"
    # Missing stop conditions fail the plan requirement with the param-level id.
    plan_only = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (
        LEGACY_BASELINE_DEFINITION.steps[0].model_copy(update={"required_outputs": (
            PlanRequirement(type="plan", require_completion_criteria=True, require_stop_conditions=True),)}),)})
    replayed, significant = await replayed_for(plan_only, complete.model_copy(update={"stop_conditions": ()}))
    assert replayed["outputs:falsifiable_plan:plan"] == "block"
    assert significant == ["outputs:falsifiable_plan:plan:require_stop_conditions"]
    # Distinct hypotheses are counted on the normalized alternative text.
    twin = complete.hypotheses[0].model_copy(update={"alternative": "  " + complete.hypotheses[0].alternative.upper() + "  "})
    doubled = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (
        LEGACY_BASELINE_DEFINITION.steps[0].model_copy(update={"required_outputs": (
            PlanRequirement(type="plan", require_completion_criteria=True, require_stop_conditions=True),
            HypothesesRequirement(type="hypotheses", min_count=2),)}),)})
    replayed, _ = await replayed_for(doubled, complete.model_copy(update={"hypotheses": (*complete.hypotheses, twin)}))
    assert replayed["outputs:falsifiable_plan:hypotheses"] == "block"
    distinct = complete.model_copy(update={"hypotheses": (*complete.hypotheses,
        ResearchHypothesis(question="q", explanation="e", alternative="Cash durability may fail",
            falsification_condition="f", evidence_priority="p"))})
    replayed, _ = await replayed_for(doubled, distinct)
    assert replayed["outputs:falsifiable_plan:hypotheses"] == "pass"


async def test_snapshot_field_replays_from_committed_capture_parameters() -> None:
    field_step = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (
        LEGACY_BASELINE_DEFINITION.steps[0].model_copy(update={"required_outputs": (
            SnapshotFieldRequirement(type="snapshot_field", field="close"),)}),)})
    progress = full_progress(seven_dimensions())
    replayed, significant = await replayed_for(field_step, progress, ())
    assert replayed["outputs:falsifiable_plan:snapshot_field:close"] == "block"
    assert significant == ["outputs:falsifiable_plan:snapshot_field:close"]
    replayed, _ = await replayed_for(field_step, progress, (capture_action("close"),))
    assert replayed["outputs:falsifiable_plan:snapshot_field:close"] == "pass"
    # A capture of another field does not satisfy the requirement.
    replayed, _ = await replayed_for(field_step, progress, (capture_action("roe"),))
    assert replayed["outputs:falsifiable_plan:snapshot_field:close"] == "block"


# --------------------------------------------------------------------------- #
# replay_context over a real frozen review; saved-output requirements
# --------------------------------------------------------------------------- #


async def _saved_calculation(method: str, evidence: ObjectRef) -> ObjectRef:
    as_of = datetime.now(UTC) - timedelta(days=1)
    record = CalculationRecord(id=uuid4(), schema_version=1, revision=1, actor="fixture", origin_ref=None,
        created_at=as_of, method=method, method_version="fixture", fact_refs=(evidence,), assumption_refs=(),
        input_hash="fixture", as_of=as_of, applicable=True, status="ok",
        results={"percentile": Quantity(value="0.25", unit="fraction")})
    async with session_scope() as session:
        session.add(SavedCalculation(id=str(record.id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
    return ObjectRef(kind="calculation", id=record.id, revision=1)


async def test_replay_context_builds_candidate_binding_from_frozen_review(conversation_db) -> None:
    subject, _, _, failure, _, _, _ = await samples()
    candidate = candidate_with(dimension_gap_step(required_outputs=(
        DimensionOutputRequirement(type="dimension", dimension="price_expectations", minimum_status="qualitative"),
        CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"))))
    async with session_scope() as session:
        stored = await MethodService(session).save_package(candidate, source="replay-tests")
        review = await MethodService(session).artifact(UUID(failure["id"]), "process_review")
    async with session_scope() as session:
        service = MethodService(session)
        package = MethodPackage.model_validate(await service.package_by_ref(ObjectRef(kind="package", id=UUID(stored["artifact_id"]), revision=stored["version"])))
        context = await replay_context(session, review, package, renderer_version="2", validator_version="1")
        assert context.binding.schema_version == 2
        assert context.binding.package_ref == ObjectRef(kind="package", id=package.artifact_id, revision=package.version)
        assert context.binding.package_hash == stored["content_hash"] == package.content_hash
        assert context.binding.config == candidate.discipline
        assert context.binding.listing_ref == subject
        assert context.receipt.renderer_version == "2" and context.receipt.validator_version == "1"
        assert context.receipt.binding == context.binding
        # v1 historical actions stay unsequenced: replay preserves the NULLs.
        assert context.committed_actions and all(action["committed_sequence"] is None for action in context.committed_actions)
        assert context.grant.research_kind == "company_research"
        assert context.first_plan is not None
        replayed, significant = await replay_requirements(context, ResearchProgress.model_validate(review["progress"]))
        # The failure history misses both candidate outputs: replayable block.
        assert replayed["outputs:expectations_gap:calculation:reverse_dcf_fcfe"] == "block"
        assert replayed["outputs:expectations_gap:dimension:price_expectations"] == "block"
        assert set(significant) == {"outputs:expectations_gap:calculation:reverse_dcf_fcfe",
            "outputs:expectations_gap:dimension:price_expectations", "dimension_min:price_expectations"}
        assert replay_sample_state(replayed) == "block"
        assert set(significant) & set(strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, candidate))


async def test_replay_calculation_matches_exact_resolvable_method(conversation_db) -> None:
    from tests.support.conversations import message, new_conversation, submit

    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    dcf = await _saved_calculation("reverse_dcf_fcfe", evidence)
    percentile = await _saved_calculation("historical_valuation_percentile", evidence)
    dcf_step = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (
        LEGACY_BASELINE_DEFINITION.steps[0].model_copy(update={"required_outputs": (
            CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"),)}),)})
    base = full_progress(dimensions)
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        context = offline_context(dcf_step)
        # The resolvable check anchors on the task's observation cutoff: a real
        # admitted task provides it (the replay path always freezes one).
        object.__setattr__(context, "task_id", receipt.task.id)
        object.__setattr__(context, "session", session)
        replayed, _ = await replay_requirements(context, base.model_copy(update={"fact_refs": (dcf,)}))
        assert replayed["outputs:falsifiable_plan:calculation:reverse_dcf_fcfe"] == "pass"
        # An unrelated calculation of another method cannot stand in (§6.2 item 4).
        replayed, _ = await replay_requirements(context, base.model_copy(update={"fact_refs": (percentile,)}))
        assert replayed["outputs:falsifiable_plan:calculation:reverse_dcf_fcfe"] == "block"
        replayed, _ = await replay_requirements(context, base)
        assert replayed["outputs:falsifiable_plan:calculation:reverse_dcf_fcfe"] == "block"


async def test_replay_verified_counter_requires_verified_saved_evidence(conversation_db) -> None:
    from tests.support.conversations import message, new_conversation, submit

    candidate, evidence, subject = await fixture()
    dimensions = candidate.thesis.assessed_dimensions
    counter_step = LEGACY_BASELINE_DEFINITION.model_copy(update={"steps": (
        LEGACY_BASELINE_DEFINITION.steps[0].model_copy(update={"required_outputs": (
            CounterRefsRequirement(type="counter_refs", min_count=1, verified_only=True),)}),)})
    base = full_progress(dimensions)
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        context = offline_context(counter_step)
        object.__setattr__(context, "task_id", receipt.task.id)
        object.__setattr__(context, "session", session)
        replayed, _ = await replay_requirements(context, base)
        assert replayed["outputs:falsifiable_plan:counter_refs"] == "block"
        # The fixture evidence is verified without unresolved conflicts: passes.
        replayed, _ = await replay_requirements(context, base.model_copy(update={"counter_refs": (evidence,)}))
        assert replayed["outputs:falsifiable_plan:counter_refs"] == "pass"
        row = await session.get(SavedEvidence, (str(evidence.id), evidence.revision))
        await session.execute(update(SavedEvidence).where(SavedEvidence.id == str(evidence.id))
            .values(payload={**row.payload, "verification": "candidate"}))
        replayed, significant = await replay_requirements(context, base.model_copy(update={"counter_refs": (evidence,)}))
        assert replayed["outputs:falsifiable_plan:counter_refs"] == "block"
        assert significant == ["outputs:falsifiable_plan:counter_refs:verified_only"]


async def test_replay_context_rejects_drifted_candidate_hash(conversation_db) -> None:
    _, _, _, failure, _, _, _ = await samples()
    candidate = candidate_with(dimension_gap_step())
    async with session_scope() as session:
        review = await MethodService(session).artifact(UUID(failure["id"]), "process_review")
        drifted = MethodPackage.model_validate({**candidate.model_dump(), "artifact_id": uuid4(), "version": 1,
            "content_hash": "not-the-canonical-hash", "created_at": datetime.now(UTC), "source": "replay-tests"})
        try:
            await replay_context(session, review, drifted, renderer_version="2", validator_version="1")
        except InvestorError:
            pass
        else:
            raise AssertionError("drifted candidate content must fail closed")


# --------------------------------------------------------------------------- #
# Card 17: workbench display (§9) — structured diff, replay, shadow coverage
# --------------------------------------------------------------------------- #


async def test_workbench_detail_blocks_cover_candidate_diff_replay_and_shadows(conversation_db) -> None:
    """Card 17: the package-proposal detail carries the four workbench blocks
    with correct fields, the list summary surfaces the package key, and the
    bounded response leaks neither rendered instructions nor raw artifacts."""
    import json

    import httpx

    from investment_agent.agent.method_receipt import render_method
    from investment_agent.api.app import create_app
    from investment_agent.api.auth import get_bootstrap_settings
    from tests.support.conversations import base
    from tests.test_method_evolution import dimension_gap_candidate, register_candidate_shadow
    from tests.unit.test_candidate_shadow import shadow_proposal, shadow_ready_mandate

    _, _, subject, weak, proposal = await shadow_proposal()
    await shadow_ready_mandate(subject)
    registered = await register_candidate_shadow(proposal, weak, expected="pass")
    assert registered["candidate_shadow"]["matched"]
    app = create_app()
    app.dependency_overrides[get_bootstrap_settings] = lambda: base()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"X-API-Key": "fixture-app"}) as client:
        page = (await client.get("/investor/methods")).json()
        summary = next(item["proposal"] for item in page["proposals"] if item["proposal"]["id"] == proposal["id"])
        assert summary["package_key"] == "company-research"
        response = await client.get("/investor/methods/proposals/" + proposal["id"])
        assert response.status_code == 200
        detail = response.json()
    # Block 1: the frozen candidate identity (stored artifact, hash excerpted).
    assert detail["candidate"] == {"package_key": "company-research", "version": 1,
        "content_hash": detail["proposal"]["request"]["content_hash"][:8],
        "discipline": dimension_gap_candidate().discipline.model_dump(mode="json")}
    # Block 2: the structure diff is generated from the card-11 strengthening ids.
    diff = detail["structure_diff"]
    assert diff["old_package_kind"] == "legacy"
    assert diff["new_blocking_steps"]["items"] == ["blocking:expectations_gap"]
    assert diff["outputs"]["items"] == ["outputs:expectations_gap:dimension:price_expectations"]
    assert diff["dimension_minimums"]["items"] == ["dimension_min:price_expectations"]
    assert diff["depends_on"]["items"] == ["depends_on:expectations_gap:dimension_assessment"]
    assert diff["discipline"]["items"] == [] and diff["gap_tightenings"]["items"] == []
    assert diff["enhancement_ids"]["truncated"] is False
    assert len(diff["text"]) <= 2000 and "blocking:expectations_gap" in diff["text"]
    # Block 3: per-sample three states plus the cumulative shadow set.
    replay = detail["replay"]
    assert replay["requires_candidate_shadow"] == sorted(registered["proposal"]["requires_candidate_shadow"])
    failure_sample = next(sample for sample in replay["samples"] if sample["role"] == "failure")
    in_scope = next(sample for sample in replay["samples"] if sample["role"] == "counterexample" and sample["applicable"])
    outside = next(sample for sample in replay["samples"] if sample["role"] == "counterexample" and not sample["applicable"])
    assert failure_sample["state"] == "block"
    assert "blocking:expectations_gap" in failure_sample["not_replayable"]
    assert in_scope["state"] == "not_replayable"
    assert {"blocking:expectations_gap", "results:expectations_gap"} <= set(in_scope["not_replayable"])
    assert outside["state"] is None and outside["applicable"] is False and outside["not_replayable"] == []
    # Block 4: the registered card-15 shadow with its covered requirement ids.
    shadows = detail["candidate_shadows"]
    assert shadows["total"] == 1 and not shadows["truncated"]
    entry = shadows["items"][0]
    assert entry["task_id"] == registered["candidate_shadow"]["task_id"]
    assert entry["expected"] == "pass" and entry["outcome"] == "passed"
    assert set(entry["requirement_ids"]) == set(replay["requires_candidate_shadow"])
    # §9 size discipline: no rendered instructions and no raw artifact copies.
    dump = json.dumps(detail, ensure_ascii=False)
    assert render_method(dimension_gap_candidate()) not in dump and "renderer 2" not in dump
    assert "offline_FunctionModel_engineering_only" not in dump
    assert "prospective_shadow_engineering_evidence_only_no_publication" not in dump


async def test_workbench_config_only_detail_shape_and_list_summary_stay_unchanged(conversation_db) -> None:
    """Card 17 regression: config-only proposals keep the exact detail shape
    (none of the four workbench blocks, no candidate binding) and their list
    summary carries no package key."""
    import httpx

    from investment_agent.api.app import create_app
    from investment_agent.api.auth import get_bootstrap_settings
    from tests.support.conversations import base
    from tests.test_method_evolution import proposal_for

    subject, _, _, review, _, _, _ = await samples()
    proposal = await proposal_for(subject, review)
    app = create_app()
    app.dependency_overrides[get_bootstrap_settings] = lambda: base()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers={"X-API-Key": "fixture-app"}) as client:
        page = (await client.get("/investor/methods")).json()
        assert page["proposals"] and all("package_key" not in item["proposal"] for item in page["proposals"])
        response = await client.get("/investor/methods/proposals/" + proposal["id"])
        assert response.status_code == 200
        detail = response.json()
    assert set(detail) == {"proposal", "history_counts", "detail_loaded", "evaluations", "shadows", "prospective", "artifact_hashes", "head_revision", "current_binding", "history_note", "evidence_hash", "approval_summary"}
    assert detail["detail_loaded"] and detail["evidence_hash"] and detail["approval_summary"]

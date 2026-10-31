"""v2 task method receipts (card 04): fixed renderer, build/parse, admission freezing.

Covers design §4.3/§6.1: the renderer is deterministic fixed-template text that
never emits ``title`` / ``description``; ``build_receipt`` hashes the rendered
text and freezes tool-family members; a real v1 admission keeps its byte-exact
payload while a v2 default head freezes a receipt whose hash, config, kind,
aggregate-validation and capability checks all fail closed.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant, admit
from investment_agent.agent.method_receipt import (
    RENDERER_VERSION,
    VALIDATOR_RENDER_ONLY,
    VALIDATOR_V1,
    TaskMethodReceipt,
    build_receipt,
    parse_task_method_payload,
    render_method,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.method_packages import (
    LEGACY_BASELINE_DEFINITION,
    CalculationRequirement,
    CounterRefsRequirement,
    DimensionOutputRequirement,
    HypothesesRequirement,
    MethodPackage,
    MethodPackageDefinition,
    MethodStep,
    PeerContextRequirement,
    PlanRequirement,
    SnapshotFieldRequirement,
    canonical_hash,
)
from investment_agent.investor.methods import (
    ActivateGlobal,
    MethodBinding,
    MethodConfig,
    MethodService,
    task_method,
    task_method_receipt,
)
from investment_agent.investor.publication import FIXED_METHOD
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.investor import (
    MethodArtifact,
    MethodDefaultHead,
    TaskMethodBinding,
)
from tests.support.conversations import (
    base,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.support.publication import fixture

BLOCKING_DISCIPLINE = "Blocking steps must be planned first and satisfied or completed-with-gap"


def _marker_package() -> MethodPackageDefinition:
    """One package exercising every requirement kind with review-only markers."""
    return MethodPackageDefinition(
        package_key="marker-package",
        applicable_kinds=("company_research",),
        description="DESCRIPTION-ONLY-REVIEW-TEXT",
        steps=(
            MethodStep(
                id="first_step",
                title="TITLE-ONLY-REVIEW-TEXT",
                instruction_kind="falsifiable_plan",
                blocking=True,
                required_outputs=(
                    PlanRequirement(type="plan", require_completion_criteria=True, require_stop_conditions=False),
                    HypothesesRequirement(type="hypotheses", min_count=1),
                ),
            ),
            MethodStep(
                id="second_step",
                title="ANOTHER-TITLE-MARKER",
                instruction_kind="counter_analysis",
                blocking=False,
                depends_on=("first_step",),
                required_capabilities=("valuation", "peer"),
                allow_missing=True,
                required_outputs=(
                    CounterRefsRequirement(type="counter_refs", min_count=2, verified_only=True),
                    PeerContextRequirement(type="peer_context", require_calculation=True),
                    CalculationRequirement(type="calculation", method="historical_valuation_percentile"),
                    SnapshotFieldRequirement(type="snapshot_field", field="close"),
                ),
            ),
            MethodStep(
                id="gap_step",
                title="GAP-STEP-TITLE-MARKER",
                instruction_kind="expectations_gap",
                blocking=True,
                depends_on=("first_step", "second_step"),
                allow_missing=True,
                required_outputs=(
                    CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"),
                    DimensionOutputRequirement(
                        type="dimension", dimension="price_expectations", minimum_status="qualitative"
                    ),
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Fixed renderer (design §6.1 item 4)
# --------------------------------------------------------------------------- #


def test_render_is_deterministic_and_fixed_template() -> None:
    package = _marker_package()
    first = render_method(package)
    rebuilt = MethodPackageDefinition.model_validate(package.model_dump(mode="json"))
    assert render_method(rebuilt) == first  # no hidden state between renders
    assert render_method(package) == first
    assert first.startswith("Method package marker-package (renderer 2):")
    assert BLOCKING_DISCIPLINE in first
    assert "Step first_step (falsifiable_plan):" in first
    assert "Step second_step (counter_analysis):" in first
    assert "Step gap_step (expectations_gap):" in first
    assert "This step is blocking." in first
    assert "This step is non-blocking." in first
    assert "Depends on first_step: each listed step must complete before this step." in first


def test_render_never_concatenates_title_or_description() -> None:
    rendered = render_method(_marker_package())
    for marker in ("DESCRIPTION-ONLY-REVIEW-TEXT", "TITLE-ONLY-REVIEW-TEXT", "ANOTHER-TITLE-MARKER", "GAP-STEP-TITLE-MARKER"):
        assert marker not in rendered


def test_render_states_every_typed_output_requirement() -> None:
    rendered = render_method(_marker_package())
    assert "plan output (completion criteria required)" in rendered
    assert "hypotheses output: at least 1 distinct hypothesis entries" in rendered
    assert (
        "counter_refs output: at least 2 references included in progress.counter_refs,"
        " each verified without unresolved conflicts" in rendered
    )
    assert (
        "peer_context output: a get_industry_peer_context action reference with a"
        " calculation bound to the same frozen peer context" in rendered
    )
    assert (
        "calculation output: a historical_valuation_percentile calculation referenced"
        " by committed actions" in rendered
    )
    assert "snapshot_field output: a committed capture_snapshot action for field close" in rendered
    assert "allow_missing: true" in rendered and "allow_missing: false" in rendered


def test_render_expectations_gap_states_compare_and_downgrade_rule() -> None:
    rendered = render_method(_marker_package())
    assert "compare what the current price implies" in rendered
    assert "state the gap and which evidence would narrow it" in rendered
    assert "keep the price view balanced or undetermined" in rendered
    assert "calculation output: a reverse_dcf_fcfe calculation referenced by committed actions" in rendered
    assert "dimension output: price_expectations must reach at least qualitative status" in rendered


def test_render_of_legacy_baseline_lists_step_ids_and_dimensions() -> None:
    rendered = render_method(LEGACY_BASELINE_DEFINITION)
    assert rendered.startswith("Method package company-research (renderer 2):")
    for step_id in ("falsifiable_plan", "counter_analysis", "dimension_assessment"):
        assert f"Step {step_id} (" in rendered
    assert "dimension output: business_demand must reach at least missing status" in rendered
    assert "plan output (completion criteria required; stop conditions required)" in rendered
    assert "Depends on: none." in rendered and "each listed step must complete" not in rendered


# --------------------------------------------------------------------------- #
# build_receipt: hash + frozen tool-family snapshot (design §4.3)
# --------------------------------------------------------------------------- #


def test_build_receipt_hashes_rendered_text_and_freezes_families() -> None:
    package = _marker_package()
    binding = _receipt_binding(package)
    receipt = build_receipt(binding, package, validator_version=VALIDATOR_RENDER_ONLY)
    assert receipt.binding == binding
    assert receipt.renderer_version == RENDERER_VERSION == "2"
    assert receipt.validator_version == VALIDATOR_RENDER_ONLY == "render-only"
    assert receipt.rendered_method == render_method(package)
    assert receipt.rendered_method_hash == hashlib.sha256(receipt.rendered_method.encode("utf-8")).hexdigest()
    assert receipt.tool_family_members == {
        "valuation": ("historical_valuation_percentile", "reverse_dcf_fcfe"),
        "peer": ("get_industry_peer_context",),
    }
    assert build_receipt(binding, package, validator_version=VALIDATOR_RENDER_ONLY) == receipt
    assert VALIDATOR_V1 == "1"


def test_receipt_round_trips_through_json_and_rejects_unknown_fields() -> None:
    package = _marker_package()
    receipt = build_receipt(_receipt_binding(package), package, validator_version=VALIDATOR_RENDER_ONLY)
    data = receipt.model_dump(mode="json")
    assert isinstance(data["tool_family_members"]["valuation"], list)
    assert TaskMethodReceipt.model_validate(data) == receipt
    data["unknown_field"] = 1
    with pytest.raises(ValidationError):
        TaskMethodReceipt.model_validate(data)


def _receipt_binding(package: MethodPackageDefinition, **overrides: Any) -> MethodBinding:
    values: dict[str, Any] = {
        "schema_version": 2,
        "method_ref": ObjectRef(kind="method", id=uuid4(), revision=1),
        "config": package.discipline,
        "package_ref": ObjectRef(kind="package", id=uuid4(), revision=1),
        "package_hash": canonical_hash(package),
    }
    values.update(overrides)
    return MethodBinding(**values)


# --------------------------------------------------------------------------- #
# parse_task_method_payload: v1 compatibility (design §4.3)
# --------------------------------------------------------------------------- #


def test_parse_keeps_legacy_payloads_plain_bindings() -> None:
    scoped = MethodBinding(listing_ref=ObjectRef(kind="listing", id=7, revision=1))
    binding, receipt = parse_task_method_payload(scoped.model_dump(mode="json"))
    assert binding == scoped and receipt is None
    binding, receipt = parse_task_method_payload(MethodBinding().model_dump(mode="json"))
    assert binding == MethodBinding() and receipt is None


def test_parse_reads_v2_receipt_payloads() -> None:
    package = _marker_package()
    receipt = build_receipt(_receipt_binding(package), package, validator_version=VALIDATOR_RENDER_ONLY)
    payload = {"schema_version": 2, "receipt": receipt.model_dump(mode="json")}
    binding, parsed = parse_task_method_payload(payload)
    assert binding == receipt.binding and parsed == receipt


# --------------------------------------------------------------------------- #
# Admission freezing through the real admit()/submit() path
# --------------------------------------------------------------------------- #


async def _saved_baseline(**overrides: Any) -> dict[str, Any]:
    async with session_scope() as session:
        return await MethodService(session).save_package(
            LEGACY_BASELINE_DEFINITION.model_copy(update=overrides), source="receipt-tests"
        )


async def _activate(saved: dict[str, Any]) -> None:
    async with session_scope() as session:
        await MethodService(session).activate_global(
            ActivateGlobal(
                mutation_id=uuid4(),
                package_ref=ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]),
                expected_revision=0,
                reason="Enable the reviewed baseline package",
            ),
            actor="authenticated_user",
        )


async def _point_default_head(binding: MethodBinding) -> None:
    async with session_scope() as session:
        session.add(
            MethodDefaultHead(owner_id="local_owner", channel="company-research", revision=1, payload=binding.model_dump(mode="json"))
        )


def _stored_v2_binding(saved: dict[str, Any], **overrides: Any) -> MethodBinding:
    values: dict[str, Any] = {
        "schema_version": 2,
        "method_ref": ObjectRef(kind="method", id=uuid4(), revision=1),
        "config": MethodConfig.model_validate(saved["discipline"]),
        "package_ref": ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]),
        "package_hash": saved["content_hash"],
    }
    values.update(overrides)
    return MethodBinding(**values)


def _direct_grant(subject: ObjectRef, tool_names: tuple[str, ...]) -> ExecutionGrant:
    return ExecutionGrant(
        mode="interactive",
        source="authenticated_message",
        prompt="深入研究",
        tool_names=tool_names,
        scope_refs=(subject,),
        research_kind="company_research",
    )


async def test_v1_admission_payload_stays_byte_identical(conversation_db) -> None:
    subject = (await fixture())[2]
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        row = await session.get(TaskMethodBinding, str(receipt.task.id))
        assert row is not None
        # The exact current v1 shape: baseline binding plus the single listing.
        assert row.payload == {
            "schema_version": 1,
            "method_ref": {"kind": "method", "id": str(FIXED_METHOD.id), "revision": 1, "field_path": None},
            "config": {
                "schema_version": 1,
                "require_two_counter_hypotheses": False,
                "require_verified_counter_evidence": False,
                "require_explicit_stop_decision": False,
            },
            "listing_ref": {"kind": "listing", "id": subject.id, "revision": 1, "field_path": None},
        }
        assert await task_method(session, receipt.task.id) == MethodBinding(listing_ref=subject)
        assert await task_method_receipt(session, receipt.task.id) is None


async def test_v1_non_company_admission_keeps_plain_baseline(conversation_db) -> None:
    receipt = await submit(await new_conversation(), message(text="解释经营研究方法", research_kind="explanation"))
    async with session_scope() as session:
        row = await session.get(TaskMethodBinding, str(receipt.task.id))
        assert row is not None
        assert row.payload == MethodBinding().model_dump(mode="json")
        assert await task_method_receipt(session, receipt.task.id) is None


async def test_v2_default_head_freezes_receipt_on_admission(conversation_db) -> None:
    subject = (await fixture())[2]
    saved = await _saved_baseline()
    await _activate(saved)
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    async with session_scope() as session:
        row = await session.get(TaskMethodBinding, str(receipt.task.id))
        assert row is not None
        assert set(row.payload) == {"schema_version", "receipt"} and row.payload["schema_version"] == 2
        frozen = TaskMethodReceipt.model_validate(row.payload["receipt"])
        # Card 07: new v2 tasks freeze as enforcing (validator v1).
        assert frozen.validator_version == VALIDATOR_V1
        assert frozen.renderer_version == RENDERER_VERSION
        assert frozen.rendered_method_hash == hashlib.sha256(frozen.rendered_method.encode("utf-8")).hexdigest()
        assert frozen.binding.schema_version == 2
        assert frozen.binding.package_ref == ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"])
        assert frozen.binding.package_hash == saved["content_hash"]
        # The baseline package adds no capability requirement: an empty snapshot.
        assert frozen.tool_family_members == {}
        for step_id in ("falsifiable_plan", "counter_analysis", "dimension_assessment"):
            assert f"Step {step_id} (" in frozen.rendered_method
        # Readers agree: task_method returns the frozen binding, receipt helper the receipt.
        assert await task_method(session, receipt.task.id) == frozen.binding
        assert await task_method_receipt(session, receipt.task.id) == frozen


async def test_tampered_package_hash_fails_closed(conversation_db) -> None:
    subject = (await fixture())[2]
    saved = await _saved_baseline()
    binding = _stored_v2_binding(saved, package_hash="0" * 64)
    await _point_default_head(binding)
    with pytest.raises(InvestorError) as info:
        await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    assert info.value.detail.code == "method_package_corrupt"


async def test_config_mismatch_fails_closed(conversation_db) -> None:
    subject = (await fixture())[2]
    saved = await _saved_baseline()
    await _point_default_head(_stored_v2_binding(saved, config=MethodConfig(require_two_counter_hypotheses=True)))
    with pytest.raises(InvestorError) as info:
        await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    assert info.value.detail.code == "method_package_corrupt"


async def test_corrupted_package_content_fails_aggregate_revalidation(conversation_db) -> None:
    subject = (await fixture())[2]
    # save_package refuses invalid definitions, so the corruption must enter
    # storage directly: the same package with no blocking step left.
    broken = LEGACY_BASELINE_DEFINITION.model_copy(
        update={
            "package_key": "corrupt-baseline",
            "steps": tuple(step.model_copy(update={"blocking": False}) for step in LEGACY_BASELINE_DEFINITION.steps),
        }
    )
    artifact_id = uuid4()
    package = MethodPackage(
        **broken.model_dump(mode="json"),
        artifact_id=artifact_id,
        version=1,
        content_hash=canonical_hash(broken),
        created_at=datetime.now(UTC),
        source="unit-test",
    )
    async with session_scope() as session:
        session.add(
            MethodArtifact(
                id=str(artifact_id),
                owner_id="local_owner",
                kind="package",
                payload=package.model_dump(mode="json"),
            )
        )
    await _point_default_head(
        MethodBinding(
            schema_version=2,
            method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
            config=package.discipline,
            package_ref=ObjectRef(kind="package", id=artifact_id, revision=1),
            package_hash=canonical_hash(package),
        )
    )
    with pytest.raises(InvestorError) as info:
        await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    assert info.value.detail.code == "method_package_corrupt"


async def test_applicable_kind_mismatch_denies_admission(conversation_db) -> None:
    subject = (await fixture())[2]
    saved = await _saved_baseline(package_key="review-only", applicable_kinds=("review",))
    await _point_default_head(_stored_v2_binding(saved))
    with pytest.raises(InvestorError) as info:
        await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    assert info.value.detail.code == "method_package_not_applicable"


async def test_capability_gap_denies_admission_without_downgrade(conversation_db) -> None:
    subject = (await fixture())[2]
    valuation_step = MethodStep(
        id="valuation_step",
        title="Independent valuation",
        instruction_kind="calculation",
        blocking=False,
        required_capabilities=("valuation",),
        required_outputs=(CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"),),
    )
    async with session_scope() as session:
        saved = await MethodService(session).save_package(
            LEGACY_BASELINE_DEFINITION.model_copy(
                update={"package_key": "capability-baseline", "steps": (*LEGACY_BASELINE_DEFINITION.steps, valuation_step)}
            ),
            source="receipt-tests",
        )
    await _activate(saved)
    binding_ref = ContextBinding(context_refs=(subject,), base_refs=(subject,), as_of=datetime.now(UTC))
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="method_capability_unavailable"):
            await admit(
                session,
                base=base(),
                input_key=f"receipt:{uuid4()}",
                binding=binding_ref,
                grant=_direct_grant(subject, ("get_snapshot", "get_evidence")),
            )
    # Fail closed: the denied admission wrote no receipt and no downgrade to v1.
    async with session_scope() as session:
        assert (await session.scalars(select(TaskMethodBinding))).all() == []
    full = _direct_grant(subject, ("get_snapshot", "get_evidence", "historical_valuation_percentile", "reverse_dcf_fcfe"))
    async with session_scope() as session:
        task = await admit(session, base=base(), input_key=f"receipt:{uuid4()}", binding=binding_ref, grant=full)
        task_id = task.id
    async with session_scope() as session:
        receipt = await task_method_receipt(session, task_id)
    assert receipt is not None
    assert receipt.tool_family_members == {"valuation": ("historical_valuation_percentile", "reverse_dcf_fcfe")}

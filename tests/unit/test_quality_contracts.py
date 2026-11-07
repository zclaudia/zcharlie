"""QT-001 pure closed contracts and existing financial-consumer rejection."""

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import NumericNode
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.publication_numbers import NumericResolver
from investment_agent.research_quality import contracts, refs
from investment_agent.research_quality.contracts import (
    AssessmentInput,
    Denominators,
    ExperimentCreated,
    ExperimentStarted,
    MutationReceipt,
    QualityCaseInput,
    RubricScore,
    SafeModelSnapshot,
    UsageProjection,
)

ROOT = Path(__file__).resolve().parents[1] / "fixtures/next_iterations/contracts"
FIXTURE = json.loads((ROOT / "quality.json").read_text())
AT = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize("kind", sorted(refs.PUBLIC_ARTIFACT_KINDS))
def test_public_quality_namespace_never_becomes_financial_objectref(kind):
    value = {"kind": kind, "id": str(UUID(int=1)), "revision": 1}
    quality = refs.parse_public_ref(value)
    assert isinstance(quality, refs.QualityArtifactRef)
    with pytest.raises(ValidationError):
        ObjectRef.model_validate(quality.model_dump())
    with pytest.raises(ValidationError):
        refs.QualityArtifactRef.model_validate(
            ObjectRef(kind="evidence", id=UUID(int=1), revision=1).model_dump()
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "quality_blind_map"},
        {"kind": "evidence"},
        {"field_path": "text"},
        {"revision": True},
        {"revision": "1"},
        {"revision": 0},
        {"id": 1},
        {"id": True},
        {"kind": "quality_unknown"},
    ],
)
def test_ref_whitelist_is_closed_and_strict(changes):
    with pytest.raises(ValidationError):
        refs.parse_public_ref(
            {"kind": "quality_case", "id": str(UUID(int=1)), "revision": 1, **changes}
        )


def test_blind_map_has_a_distinct_private_type_and_no_public_export():
    private = refs._BlindMapRef(id=UUID(int=1), revision=1)
    assert "quality_blind_map" not in refs.PUBLIC_ARTIFACT_KINDS
    assert "_BlindMapRef" not in refs.__all__
    with pytest.raises(ValidationError):
        refs.parse_public_ref(private.model_dump())
    value = copy.deepcopy(FIXTURE["mutation_lookup"]["response"])
    value["result_refs"] = [private.model_dump()]
    with pytest.raises(ValidationError):
        MutationReceipt.model_validate(value)


def test_frozen_api_examples_and_original_receipt_response_roundtrip():
    request = contracts.CreateExperiment.model_validate(FIXTURE["create"]["request"])
    response = ExperimentCreated.model_validate(FIXTURE["create"]["response"])
    receipt = MutationReceipt.model_validate(FIXTURE["mutation_lookup"]["response"])
    assert receipt.response == response
    assert receipt.mutation_id == request.mutation_id and receipt.committed_at == AT
    assert receipt.response.model_dump(mode="json") == FIXTURE["create"]["response"]
    assert contracts.SealExperiment.model_validate(FIXTURE["seal"])
    assert contracts.StartExperiment.model_validate(FIXTURE["start"])
    assert contracts.CancelExperiment.model_validate(FIXTURE["cancel"])
    assert FIXTURE["mutation_not_observed"]["status"] == 404
    assert "不证明无在途" in contracts.MUTATION_NOT_OBSERVED_NOTICE


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": "local_owner"},
        {"method_override": {}},
        {"model": {}},
        {"grant": {}},
        {"repeat_count": True},
        {"repeat_count": 3},
        {"expected_proposal_revision": "4"},
        {"schema_version": True},
        {"schema_version": 1.0},
    ],
)
def test_create_input_cannot_inject_authority_or_unbounded_repeats(changes):
    with pytest.raises(ValidationError):
        contracts.CreateExperiment.model_validate({**FIXTURE["create"]["request"], **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "other"},
        {"response": {"prompt": "private"}},
        {"experiment_id": str(UUID(int=999))},
        {"response_status": True},
        {"result_refs": [{"kind": "quality_result", "id": str(UUID(int=111)), "revision": 1}]},
    ],
)
def test_receipt_cannot_expose_open_payloads_or_change_original_references(changes):
    raw = copy.deepcopy(FIXTURE["mutation_lookup"]["response"])
    raw.update(changes)
    with pytest.raises(ValidationError):
        MutationReceipt.model_validate(raw)


def test_case_creation_uses_only_exact_financial_sources_and_aware_cutoff():
    value = {
        "mutation_id": str(UUID(int=1)),
        "listing_ref": {"kind": "listing", "id": 18, "revision": 1},
        "question": "核对现金来源",
        "cutoff": "2026-11-07T17:30:00+08:00",
        "material_refs": [],
    }
    assert QualityCaseInput.model_validate(value).cutoff == AT
    for changes in (
        {"cutoff": "2026-11-07T09:30:00"},
        {"question": "  "},
        {"material_refs": [FIXTURE["create"]["request"]["case_ref"]]},
        {"material_refs": [{"kind": "method", "id": str(UUID(int=1)), "revision": 1}]},
    ):
        with pytest.raises(ValidationError):
            QualityCaseInput.model_validate({**value, **changes})


def test_safe_model_snapshot_has_no_endpoint_key_or_private_values_fields():
    value = {
        "ref": "fixture/main",
        "provider_id": "fixture",
        "provider_kind": "openai",
        "model_id": "main",
        "model": "synthetic",
        "profile": "openai",
        "output_mode": "prompted",
        "thinking": "auto",
        "capabilities": ["tools"],
        "selection": {"provider_id": "fixture", "model_id": "main"},
        "invocation_hash": "a" * 64,
    }
    assert SafeModelSnapshot.model_validate(value)
    for changes in (
        {"base_url": "https://private.invalid"},
        {"api_key": "secret"},
        {"values": {}},
        {"private_prompt": "secret"},
        {"model_id": "other"},
    ):
        with pytest.raises(ValidationError):
            SafeModelSnapshot.model_validate({**value, **changes})


def test_trial_start_requires_atomic_pairs_with_unique_tasks():
    trial = {
        "trial_id": str(UUID(int=1)),
        "arm": "baseline",
        "repeat_index": 1,
        "task_ref": {"kind": "task", "id": str(UUID(int=101)), "revision": 1},
    }
    other = {
        **trial,
        "trial_id": str(UUID(int=2)),
        "arm": "candidate",
        "task_ref": {**trial["task_ref"], "id": str(UUID(int=102))},
    }
    value = {
        "experiment_id": str(UUID(int=10)),
        "revision": 3,
        "spec_hash": "a" * 64,
        "trials": [trial, other],
    }
    assert ExperimentStarted.model_validate(value)
    for trials in (
        [trial],
        [trial, trial],
        [trial, {**other, "repeat_index": 2}],
        [trial, {**other, "task_ref": trial["task_ref"]}],
    ):
        with pytest.raises(ValidationError):
            ExperimentStarted.model_validate({**value, "trials": trials})


def test_rubric_requires_five_justified_strict_scores_without_reviewer_override():
    score = {"score": 2, "reason": "关键论点可定位"}
    fields = (
        "fact_relationship",
        "research_depth",
        "counter_evidence",
        "independent_revision",
        "clarity_next_steps",
    )
    value = {
        "mutation_id": str(UUID(int=1)),
        "expected_assessment_revision": 0,
        "output_hash": "a" * 64,
        "scores": {field: score for field in fields},
    }
    assert AssessmentInput.model_validate(value)
    assert RubricScore(score=None, reason="证据不足样本", not_applicable_reason="没有方向性结论")
    for bad in (True, 1.0, "2", 3):
        with pytest.raises(ValidationError):
            RubricScore.model_validate({**score, "score": bad})
    with pytest.raises(ValidationError):
        RubricScore(score=None, reason="N/A")
    with pytest.raises(ValidationError):
        AssessmentInput.model_validate({**value, "reviewer_id": "judge"})


def test_unknown_usage_and_seven_denominators_keep_failure_and_unknown_values():
    usage = {
        "state": "unknown",
        "requests": 1,
        "tokens": None,
        "cost": None,
        "cost_status": "unknown",
    }
    assert UsageProjection.model_validate(usage).tokens is None
    for changes in (
        {"tokens": 0},
        {"cost": "0", "cost_status": "known"},
        {"cost": True},
        {"cost": 0.0},
        {"cost": "NaN"},
    ):
        with pytest.raises(ValidationError):
            UsageProjection.model_validate({**usage, **changes})
    assert UsageProjection(
        state="settled", requests=1, tokens=10, cost=Decimal("0.010"), cost_status="known"
    )
    counts = {
        "planned": 2,
        "admitted": 2,
        "sent": 1,
        "completed": 1,
        "usable_output": 1,
        "paired": 0,
        "scored": 0,
    }
    assert Denominators.model_validate(counts).planned == 2
    for changes in ({"planned": 1}, {"paired": 1}, {"scored": 2}, {"completed": True}):
        with pytest.raises(ValidationError):
            Denominators.model_validate({**counts, **changes})
    assert "unknown" not in str(contracts.TrialState.__value__)
    assert not contracts.EXPERIMENT_TRANSITIONS["cancelled"]


async def test_existing_financial_consumers_refuse_quality_namespace_without_database():
    quality = refs.QualityArtifactRef(kind="quality_result", id=UUID(int=1), revision=1)
    with pytest.raises(InvestorError) as error:
        await EvidenceService(None).get_evidence("local_owner", quality, AT)
    assert error.value.detail.code == "invalid_reference"
    # An adversarial internal caller cannot bypass NumericNode parsing by constructing a node.
    node = NumericNode.model_construct(
        type="numeric_claim", ref=quality, role="historical_fact", display_format="decimal"
    )
    resolver = NumericResolver(
        EvidenceService(None), ObjectRef(kind="listing", id=18, revision=1), AT
    )
    with pytest.raises(InvestorError) as error:
        await resolver.resolve(node)
    assert error.value.detail.code == "invalid_reference"

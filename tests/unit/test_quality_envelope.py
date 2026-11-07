"""QT-014 canonical manifests from the actual frozen synthetic snapshot recipe."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.evidence.queries import SecurityIdentity
from investment_agent.evidence.snapshots import materialize
from investment_agent.research_quality.contracts import QualityCaseSaved
from investment_agent.research_quality.envelope import (
    FrozenBlob,
    FrozenMaterial,
    FrozenReadEnvelope,
    build_envelope,
    evidence_material,
)
from investment_agent.research_quality.refs import QualityArtifactRef
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from tests.support.quality_dataset import load_dataset


def inputs():
    recipe = load_dataset()[0]
    materials = tuple(
        evidence_material(
            item.evidence.source_ref,
            materialize(
                FinancialSnapshot(**item.snapshot.model_dump()),
                item.evidence.locator.removeprefix("fields."),
            ),
            category="snapshot",
            source_algorithm="snapshot-materialize.v1",
        )
        for item in recipe.materials
    )
    case = QualityCaseSaved(
        case_ref=QualityArtifactRef(kind="quality_case", id=uuid4(), revision=1),
        listing_ref=recipe.listing_ref,
        question=recipe.question,
        cutoff=recipe.cutoff,
        material_mode="observed_materials",
        material_refs=tuple(item.ref for item in materials),
    )
    identity = SecurityIdentity(
        listing_ref=case.listing_ref,
        ticker="600001.SH",
        market="SSE",
        company_name="Synthetic",
        currency="CNY",
    )
    return case, identity, materials


def test_same_inputs_reproduce_bytes_hash_and_exact_units_versions():
    case, identity, materials = inputs()
    first = build_envelope(case, identity, materials, gaps=("b", "a", "a"))
    second = build_envelope(case, identity, tuple(reversed(materials)), gaps=("a", "b"))
    assert first == second and first.content_hash == second.content_hash
    assert first.gaps == ("a", "b")
    assert all(item.unit == "CNY" for item in first.materials)
    assert all(item.verification == item.payload["verification"] for item in first.materials)
    assert first.versions.tool_profile == "quality.frozen-read.v1"
    assert first.versions.renderer and first.versions.original_parser
    assert first.versions.resolver and first.versions.method_renderer
    assert FrozenReadEnvelope.model_validate_json(first.model_dump_json()) == first


@pytest.mark.parametrize("field", ["published_at", "acquired_at", "available_from"])
def test_each_time_boundary_is_required_even_for_an_old_report_period(field):
    case, identity, materials = inputs()
    changed = materials[0].model_copy(update={field: case.cutoff + timedelta(seconds=1)})
    with pytest.raises(ValidationError):
        build_envelope(case, identity, (changed, *materials[1:]))


def test_observed_and_reconstructed_separate_hashes_and_neither_bypasses_times():
    case, identity, materials = inputs()
    observed = build_envelope(case, identity, materials)
    reconstructed = build_envelope(
        case.model_copy(update={"material_mode": "reconstructed_materials"}), identity, materials
    )
    assert observed.content_hash != reconstructed.content_hash
    with pytest.raises(ValueError):
        build_envelope(case, identity, materials[:-1])
    with pytest.raises(ValidationError):
        FrozenReadEnvelope.model_validate(
            {**observed.model_dump(), "allowed_tools": ["capture_snapshot"]}
        )


def test_payload_hash_limits_and_blob_reference_do_not_copy_original_bytes():
    case, _, materials = inputs()
    data = materials[0].model_dump()
    data["payload"]["text"] = "tampered"
    with pytest.raises(ValidationError):
        FrozenMaterial.model_validate(data)
    from investment_agent.investor.common import ObjectRef

    blob = FrozenBlob(
        original_ref=ObjectRef(kind="disclosure_original", id=uuid4(), revision=1),
        content_hash="a" * 64,
    )
    assert "body" not in blob.model_dump() and blob.content_hash == "a" * 64
    with pytest.raises(ValidationError):
        FrozenBlob.model_validate({**blob.model_dump(), "body": b"original"})

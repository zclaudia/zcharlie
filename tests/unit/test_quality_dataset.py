"""QT-002 synthetic matrix and original candidate-import seam, not QT-003."""

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import NumericNode
from investment_agent.investor.method_packages import (
    canonical_definition,
    canonical_hash,
    load_package_file,
    resolve_family_members,
    strengthening_violations,
    strictly_stronger_requirements,
)
from investment_agent.investor.methods import CreateProposal, MethodBinding, MethodService
from investment_agent.investor.publication_numbers import NumericResolver
from investment_agent.research_events.canonical import payload_hash
from investment_agent.storage.models import Base
from investment_agent.storage.models.agent import AgentTask, BudgetEntry
from investment_agent.storage.models.investor import MethodDefaultHead, MethodHead, MethodProposal
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.support.quality_dataset import (
    CATEGORIES,
    COMPANIES,
    DEVELOPMENT,
    REPO,
    ROOT,
    DatasetCase,
    SnapshotRecipe,
    build_dataset,
    load_dataset,
)


def test_twelve_case_matrix_exact_refs_modes_expectations_and_frozen_split():
    cases = load_dataset()
    assert cases == build_dataset()
    assert len(cases) == 12
    assert {case.id for case in cases if case.split == "development"} == DEVELOPMENT
    assert sum(case.split == "holdout" for case in cases) == 8
    for identifier, company, _, _ in COMPANIES:
        selected = [case for case in cases if case.company_key == company]
        assert {case.category for case in selected} == set(CATEGORIES)
        assert {case.listing_ref.id for case in selected} == {identifier}
    for case in cases:
        assert case.synthetic is True and case.execution_status == "not_run"
        assert case.reference_status == "fixture_only_not_persisted"
        assert case.repeat_count == 1 and case.material_mode == "observed_materials"
        assert case.expected.preserves_failure_denominator
        if case.category == "insufficient":
            assert not case.materials and not case.expected.required_counter_refs
            assert (
                case.expected.behavior == "insufficient_evidence"
                and "material_missing" in case.expected.expected_gaps
            )
        else:
            assert case.expected.required_counter_refs
            for ref in case.expected.required_counter_refs:
                material = next(item for item in case.materials if item.evidence_ref == ref)
                assert material.evidence.verification == "verified"
                assert (
                    material.evidence.value is not None
                    and material.evidence.value.value is not None
                )
                assert (
                    max(
                        material.evidence.published_at,
                        material.evidence.acquired_at,
                        material.evidence.available_from,
                    )
                    <= case.cutoff
                )
        if case.category == "prior_failure":
            assert case.prior_failure.kind == "failure_recipe_only"
            assert case.prior_failure.task_ref is None  # never pretend a real old Task exists


def test_expected_cases_are_not_claimed_research_outputs_or_quality_success():
    for case in load_dataset():
        with pytest.raises(ValidationError):
            DatasetCase.model_validate({**case.model_dump(), "execution_status": "succeeded"})
        with pytest.raises(ValidationError):
            DatasetCase.model_validate({**case.model_dump(), "result": "candidate won"})
        for changes in ({"synthetic": 1}, {"synthetic": False}, {"schema_version": True}):
            with pytest.raises(ValidationError):
                DatasetCase.model_validate({**case.model_dump(), **changes})
    index = json.loads((ROOT / "dataset/index.json").read_text())
    assert index["status"] == "synthetic_offline_not_evaluated"
    assert index["holdout_use_count"] == 0


def test_loader_rejects_changed_files_and_split_hashes(tmp_path):
    copied = tmp_path / "quality"
    shutil.copytree(ROOT, copied)
    index = json.loads((copied / "dataset/index.json").read_text())
    first = index["cases"][0]
    path = copied / "dataset" / first["path"]
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="file hash"):
        load_dataset(copied)
    # Even when the bytes are untouched, a changed split cannot reuse the old index hashes.
    path.write_bytes((ROOT / "dataset" / first["path"]).read_bytes())
    index["cases"][0]["split"] = "holdout"
    (copied / "dataset/index.json").write_text(json.dumps(index))
    with pytest.raises(ValueError, match="split mismatch"):
        load_dataset(copied)
    index["cases"][0]["path"] = "../methods/baseline.json"
    (copied / "dataset/index.json").write_text(json.dumps(index))
    with pytest.raises(ValueError, match="path"):
        load_dataset(copied)


def test_recipe_material_hash_subject_and_time_are_not_self_asserted():
    first = load_dataset()[0]
    with pytest.raises(ValidationError):
        DatasetCase.model_validate(
            {**first.model_dump(), "cutoff": first.cutoff - timedelta(days=10)}
        )
    raw = first.model_dump(mode="json")
    raw["materials"][0]["evidence"]["verification"] = "candidate"
    with pytest.raises(ValidationError, match="original snapshot materializer"):
        DatasetCase.model_validate(raw)
    index = json.loads((ROOT / "dataset/index.json").read_text())
    assert payload_hash(index["cases"]) == index["dataset_hash"]
    assert len({entry["content_hash"] for entry in index["cases"]}) == 12


@pytest.mark.parametrize("bad", [True, False, 100.0, "100"])
def test_snapshot_recipe_requires_native_strict_integers_not_bool_float_or_string(bad):
    recipe = load_dataset()[0].materials[0].snapshot.model_dump()
    recipe["fields"]["revenue_main"] = bad
    with pytest.raises(ValidationError):
        SnapshotRecipe.model_validate(recipe)


async def test_all_non_insufficient_materials_save_read_ok_and_resolve_expected_numbers(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'quality-dataset-values.sqlite'}"
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(
                Market(code="SSE", name="Synthetic", timezone="Asia/Shanghai", currency="CNY")
            )
            for listing_id, _, name, ticker in COMPANIES:
                session.add(Company(id=listing_id, canonical_name=name))
                session.add(
                    Listing(
                        id=listing_id,
                        company_id=listing_id,
                        market_code="SSE",
                        ticker=ticker.split(".")[0],
                    )
                )
            seen = set()
            checked = 0
            negative_counters = 0
            for case in load_dataset():
                for material in case.materials:
                    if material.snapshot.id not in seen:
                        session.add(FinancialSnapshot(**material.snapshot.model_dump()))
                        seen.add(material.snapshot.id)
                    await session.flush()
                    ref = await EvidenceService(session).save_evidence(
                        "local_owner", material.evidence, UUID(int=100000 + checked)
                    )
                    at = max(case.cutoff, datetime.now(UTC))
                    result = await EvidenceService(session).get_evidence(
                        "local_owner", ref, at, subject_ref=case.listing_ref
                    )
                    assert result.status == "ok" and result.data.value.value is not None
                    field = material.evidence.locator.removeprefix("fields.")
                    expected = Decimal(material.snapshot.fields[field])
                    resolved = await NumericResolver(
                        EvidenceService(session), case.listing_ref, at
                    ).resolve(
                        NumericNode(
                            type="numeric_claim",
                            ref=ref.model_copy(update={"field_path": "value"}),
                            role="historical_fact",
                            display_format="decimal",
                        )
                    )
                    assert Decimal(resolved.value) == result.data.value.value == expected
                    if (
                        case.category == "counterexample"
                        and material.snapshot.report_period.year == 2025
                        and field == "operating_cash_flow"
                    ):
                        assert expected == -100
                        negative_counters += 1
                    checked += 1
            assert checked == 54 and negative_counters == 3
    finally:
        await engine.dispose()


def test_c1_preserves_three_steps_and_only_strengthens_counter_analysis():
    baseline = load_package_file(REPO / "config/methods/company-research.v1.yaml")
    candidate = load_package_file(REPO / "config/methods/company-research.c1-counter-analysis.yaml")
    assert (
        [step.id for step in candidate.steps]
        == [step.id for step in baseline.steps]
        == ["falsifiable_plan", "counter_analysis", "dimension_assessment"]
    )
    assert candidate.steps[0] == baseline.steps[0] and candidate.steps[2] == baseline.steps[2]
    counter = candidate.steps[1]
    assert counter.blocking
    requirements = {requirement.type: requirement for requirement in counter.required_outputs}
    assert requirements["hypotheses"].min_count == 2
    assert (
        requirements["counter_refs"].min_count == 1 and requirements["counter_refs"].verified_only
    )
    assert candidate.discipline.require_two_counter_hypotheses
    assert candidate.discipline.require_verified_counter_evidence
    assert (
        candidate.discipline.require_explicit_stop_decision
        == baseline.discipline.require_explicit_stop_decision
    )
    assert not strengthening_violations(baseline, candidate)
    assert strictly_stronger_requirements(baseline, candidate)
    for step in candidate.steps:
        assert resolve_family_members(step.required_capabilities) == ()
        assert all(
            requirement.type not in {"calculation", "snapshot_field", "peer_context"}
            for requirement in step.required_outputs
        )


def test_method_fixtures_match_original_canonical_authoring_hashes():
    manifest = json.loads((ROOT / "methods/manifest.json").read_text())
    assert manifest["execution_compatibility"] == "pending_QT003"
    assert manifest["default_head_changed"] is False
    for entry in manifest["packages"]:
        definition = load_package_file(REPO / entry["authoring_path"])
        path = ROOT / "methods" / entry["definition_path"]
        assert json.loads(path.read_text()) == canonical_definition(definition)
        assert canonical_hash(definition) == entry["content_hash"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]


async def test_c1_passes_original_candidate_package_import_seam_without_activation(tmp_path):
    """Only the real package-freezing seam; no fabricated process review/proposal.

    Full proposal eligibility, structure evaluation, original admit and method
    settlement remain the separate QT-003 integration gate.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quality-c1.sqlite'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            candidate = load_package_file(
                REPO / "config/methods/company-research.c1-counter-analysis.yaml"
            )
            old = MethodBinding()
            request = CreateProposal(
                mutation_id=UUID(int=1),
                review_id=UUID(int=2),
                listing_ref=ObjectRef(kind="listing", id=180001, revision=1),
                old_method=old.method_ref,
                error_pattern="synthetic fixture only; no real review claimed",
                summary="C1 import seam",
                config=candidate.discipline,
                package=candidate,
            )
            svc = MethodService(session)
            frozen = await svc._freeze_package_candidate(request, old, str(UUID(int=3)))
            loaded = await svc.package_by_ref(ObjectRef.model_validate(frozen["package_ref"]))
            assert frozen["content_hash"] == loaded["content_hash"] == canonical_hash(candidate)
            await session.commit()
            for model in (MethodHead, MethodDefaultHead, MethodProposal, AgentTask, BudgetEntry):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        await engine.dispose()

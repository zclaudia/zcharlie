"""Method packages (card 01): canonical hash, aggregate validation, registries, legacy baseline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from investment_agent.agent.builtin_tools import SnapshotParameters
from investment_agent.agent.research import registrations
from investment_agent.investor.contracts import DIMENSIONS
from investment_agent.investor.method_packages import (
    LEGACY_BASELINE_DEFINITION,
    SNAPSHOT_FIELDS,
    TOOL_FAMILY_MEMBERS,
    CalculationRequirement,
    CounterRefsRequirement,
    DimensionOutputRequirement,
    DimensionRequirement,
    HypothesesRequirement,
    MethodPackage,
    MethodPackageDefinition,
    MethodPackageError,
    MethodStep,
    PeerContextRequirement,
    PlanRequirement,
    SnapshotFieldRequirement,
    ToolFamily,
    canonical_definition,
    canonical_hash,
    canonical_json,
    default_dimensions,
    legacy_package_for,
    load_package_file,
    load_packages,
    parse_definition,
    resolve_family_members,
    strengthening_violations,
    strictly_stronger_requirements,
    validate_definition,
)
from investment_agent.investor.methods import MethodConfig


def _baseline_data() -> dict[str, Any]:
    return LEGACY_BASELINE_DEFINITION.model_dump(mode="json")


def _step_data(**overrides: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "only_step",
        "title": "Only step",
        "instruction_kind": "dimension_assessment",
        "blocking": True,
        "depends_on": [],
        "required_capabilities": [],
        "required_outputs": [],
        "allow_missing": False,
    }
    step.update(overrides)
    return step


def _with_steps(steps: list[dict[str, Any]]) -> dict[str, Any]:
    data = _baseline_data()
    data["steps"] = steps
    return data


def _parse_error(data: dict[str, Any]) -> MethodPackageError:
    with pytest.raises(MethodPackageError) as info:
        parse_definition(data)
    return info.value


# --------------------------------------------------------------------------- #
# Canonical hash
# --------------------------------------------------------------------------- #


def test_same_content_hashes_equal_after_round_trip() -> None:
    baseline = LEGACY_BASELINE_DEFINITION
    rebuilt = parse_definition(yaml.safe_load(yaml.safe_dump(baseline.model_dump(mode="json"))))
    assert canonical_hash(rebuilt) == canonical_hash(baseline)
    assert canonical_json(rebuilt) == canonical_json(baseline)


def test_description_is_part_of_the_content_hash() -> None:
    baseline = LEGACY_BASELINE_DEFINITION
    retitled = baseline.model_copy(update={"description": "已审阅版本说明"})
    assert canonical_hash(retitled) != canonical_hash(baseline)


def test_lifecycle_fields_do_not_change_the_hash() -> None:
    baseline = LEGACY_BASELINE_DEFINITION
    artifact = MethodPackage(
        **baseline.model_dump(),
        artifact_id=uuid4(),
        version=3,
        content_hash=canonical_hash(baseline),
        created_at=datetime(2026, 10, 30, tzinfo=UTC),
        source="unit-test",
    )
    assert canonical_hash(artifact) == canonical_hash(baseline)
    moved = artifact.model_copy(update={"version": 4, "source": "re-import", "evidence_hash": "e"})
    assert canonical_hash(moved) == canonical_hash(baseline)


def test_default_dimensions_expand_to_the_explicit_seven() -> None:
    baseline = LEGACY_BASELINE_DEFINITION
    explicit = baseline.model_copy(update={"dimensions": default_dimensions()})
    assert len(explicit.dimensions) == len(DIMENSIONS)  # type: ignore[arg-type]
    assert canonical_json(baseline) == canonical_json(explicit)
    canonical = canonical_definition(baseline)
    assert canonical["dimensions"] == canonical_definition(explicit)["dimensions"]
    assert [entry["dimension"] for entry in canonical["dimensions"]] == list(DIMENSIONS)
    assert canonical["dimensions"] == [
        {
            "schema_version": 1,
            "dimension": dimension,
            "minimum_status": "missing",
            "allow_not_applicable": True,
            "allow_missing": False,
        }
        for dimension in DIMENSIONS
    ]


def test_mapping_input_is_normalized_before_hashing() -> None:
    data = _baseline_data()
    del data["discipline"], data["description"]
    data["dimensions"] = None
    canonical = canonical_definition(yaml.safe_load(yaml.safe_dump(data)))
    assert canonical["description"] == ""
    assert canonical["discipline"] == LEGACY_BASELINE_DEFINITION.discipline.model_dump(mode="json")
    assert canonical == canonical_definition(LEGACY_BASELINE_DEFINITION)


# --------------------------------------------------------------------------- #
# Aggregate validation
# --------------------------------------------------------------------------- #


def test_legacy_baseline_is_valid() -> None:
    assert validate_definition(LEGACY_BASELINE_DEFINITION) == []
    assert parse_definition(_baseline_data()) == LEGACY_BASELINE_DEFINITION


def test_baseline_restates_current_research_requirements_exactly() -> None:
    # 与 ResearchProgress.depth(agent/research.py)对 company_research/review 的现行要求
    # 一一对应,不多不少:阻断的计划步骤覆盖 hypotheses/evidence_steps/completion_criteria/
    # stop_conditions;七维各一次由阻断的维度步骤表达;反证加强仅由 discipline 开关表达,
    # 默认全关;dimensions=None 即现状默认,不新增最低状态要求。
    baseline = LEGACY_BASELINE_DEFINITION
    assert baseline.applicable_kinds == ("company_research", "review")
    assert [step.id for step in baseline.steps] == [
        "falsifiable_plan",
        "counter_analysis",
        "dimension_assessment",
    ]
    assert [step.blocking for step in baseline.steps] == [True, False, True]
    plan_outputs = baseline.steps[0].required_outputs
    assert isinstance(plan_outputs[0], PlanRequirement)
    assert plan_outputs[0].require_completion_criteria is True
    assert plan_outputs[0].require_stop_conditions is True
    assert isinstance(plan_outputs[1], HypothesesRequirement)
    assert plan_outputs[1].min_count == 1
    assert baseline.steps[1].required_outputs == ()
    dimension_outputs = baseline.steps[2].required_outputs
    assert [output.dimension for output in dimension_outputs] == list(DIMENSIONS)
    assert all(output.minimum_status == "missing" for output in dimension_outputs)
    assert baseline.dimensions is None
    assert baseline.discipline == baseline.discipline.__class__()


def test_dependency_cycles_are_rejected() -> None:
    data = _with_steps([
        _step_data(id="first_step", instruction_kind="falsifiable_plan", depends_on=["second_step"]),
        _step_data(id="second_step", instruction_kind="counter_analysis", blocking=False, depends_on=["first_step"]),
    ])
    error = _parse_error(data)
    assert any("cycle" in message and "first_step" in message for message in error.errors)


def test_self_dependency_is_rejected_with_field_path() -> None:
    data = _with_steps([_step_data(id="self_step", depends_on=["self_step"])])
    error = _parse_error(data)
    assert any(
        message.startswith("steps[0].depends_on[0]") and "depends on itself" in message
        for message in error.errors
    )


def test_unknown_depends_on_is_rejected_with_field_path() -> None:
    data = _with_steps([_step_data(id="plan_step", depends_on=["ghost_step"])])
    error = _parse_error(data)
    assert any(
        message.startswith("steps[0].depends_on[0]") and "ghost_step" in message
        for message in error.errors
    )


def test_missing_blocking_step_is_rejected() -> None:
    data = _with_steps([
        _step_data(id="first_step", blocking=False),
        _step_data(id="second_step", instruction_kind="counter_analysis", blocking=False),
    ])
    assert any("blocking" in message for message in _parse_error(data).errors)


def test_empty_steps_are_rejected() -> None:
    error = _parse_error(_with_steps([]))
    assert any(message.startswith("steps") for message in error.errors)


def test_applicable_kinds_must_be_non_empty_and_distinct() -> None:
    empty = _baseline_data()
    empty["applicable_kinds"] = []
    assert any(
        message.startswith("applicable_kinds") for message in _parse_error(empty).errors
    )
    duplicated = _baseline_data()
    duplicated["applicable_kinds"] = ["company_research", "company_research"]
    assert any(
        message.startswith("applicable_kinds") and "duplicate" in message
        for message in _parse_error(duplicated).errors
    )


def test_dimensions_must_cover_each_dimension_exactly_once() -> None:
    missing = _baseline_data()
    missing["dimensions"] = [{"dimension": name} for name in DIMENSIONS if name != "financial_cash"]
    assert any(
        message.startswith("dimensions") and "financial_cash" in message
        for message in _parse_error(missing).errors
    )
    repeated = _baseline_data()
    names = [name for name in DIMENSIONS if name != "financial_cash"]
    repeated["dimensions"] = [{"dimension": name} for name in names] + [
        {"dimension": "price_expectations"},
    ]
    error = _parse_error(repeated)
    assert any(message.startswith("dimensions") and "financial_cash" in message for message in error.errors)
    assert any(message.startswith("dimensions") and "price_expectations" in message for message in error.errors)


def test_multiple_errors_are_reported_at_once() -> None:
    data = _baseline_data()
    data["applicable_kinds"] = []
    data["steps"] = [
        _step_data(id="same_step", instruction_kind="falsifiable_plan"),
        _step_data(id="same_step", instruction_kind="counter_analysis", blocking=False, depends_on=["ghost"]),
    ]
    error = _parse_error(data)
    assert len(error.errors) >= 3
    joined = "\n".join(error.errors)
    assert "applicable_kinds" in joined
    assert "duplicate step id" in joined
    assert "steps[1].depends_on[0]" in joined


def test_schema_errors_carry_field_paths() -> None:
    # 判别联合的报错 loc 会在路径中插入变体名(如 ...required_outputs.0.hypotheses.min_count),
    # 因此按路径片段断言而不是整段前缀。
    cases: list[tuple[dict[str, Any], tuple[str, ...]]] = [
        (_baseline_data() | {"package_key": "Company"}, ("package_key",)),
        (_with_steps([_step_data(id="Bad-Id")]), ("steps.0.id",)),
        (_with_steps([_step_data(id="x" * 33)]), ("steps.0.id",)),
        (_with_steps([_step_data(instruction_kind="free_form")]), ("steps.0.instruction_kind",)),
        (_with_steps([_step_data(required_capabilities=["teleport"])]), ("steps.0.required_capabilities.0",)),
        (
            _with_steps([_step_data(required_outputs=[{"type": "hypotheses", "min_count": 0}])]),
            ("steps.0.required_outputs.0", "min_count"),
        ),
        (
            _with_steps([_step_data(required_outputs=[{"type": "mystery"}])]),
            ("steps.0.required_outputs.0",),
        ),
        (
            _with_steps([_step_data(required_outputs=[{"type": "calculation"}])]),
            ("steps.0.required_outputs.0", "method"),
        ),
        (
            _with_steps([_step_data(required_outputs=[{"type": "snapshot_field"}])]),
            ("steps.0.required_outputs.0", "field"),
        ),
        (
            _with_steps([_step_data(required_outputs=[{"type": "plan"}])]),
            ("steps.0.required_outputs.0", "require_completion_criteria"),
        ),
    ]
    for data, fragments in cases:
        error = _parse_error(data)
        assert any(all(fragment in message for fragment in fragments) for message in error.errors), (
            fragments,
            error.errors,
        )


def test_calculation_requirement_keeps_registry_methods() -> None:
    parsed = parse_definition(
        _with_steps([
            _step_data(
                id="valuation_step",
                instruction_kind="calculation",
                required_outputs=[{"type": "calculation", "method": "reverse_dcf_fcfe"}],
            )
        ])
    )
    output = parsed.steps[0].required_outputs[0]
    assert isinstance(output, CalculationRequirement)
    assert output.method == "reverse_dcf_fcfe"


# --------------------------------------------------------------------------- #
# Code-owned registries
# --------------------------------------------------------------------------- #


def test_tool_family_members_are_registered_tools() -> None:
    names = {registration.name for registration in registrations()}
    for family, members in TOOL_FAMILY_MEMBERS.items():
        assert members, family
        for member in members:
            assert member in names, f"{family}: {member}"


def test_resolve_family_members_preserves_order_and_deduplicates() -> None:
    families: list[ToolFamily] = ["valuation", "snapshot_read", "valuation"]
    assert resolve_family_members(families) == (
        "historical_valuation_percentile",
        "reverse_dcf_fcfe",
        "get_snapshot",
        "capture_snapshot",
    )
    assert resolve_family_members([]) == ()


def test_snapshot_fields_are_accepted_by_the_snapshot_tool() -> None:
    # SnapshotParameters 的 field 枚举由 default_field_sources(financial/market 表)
    # 动态生成,是本注册表的超集;每个 SnapshotField 都必须是该工具的有效字段。
    extra = SnapshotParameters.model_fields["field"].json_schema_extra
    assert isinstance(extra, dict)
    registered = extra["enum"]
    assert isinstance(registered, list)
    assert set(SNAPSHOT_FIELDS) <= set(registered)


def test_contracts_are_frozen_and_forbid_extra_fields() -> None:
    baseline = LEGACY_BASELINE_DEFINITION
    with pytest.raises(ValidationError):
        baseline.package_key = "other-package"
    data = _baseline_data()
    data["unknown_field"] = 1
    with pytest.raises(MethodPackageError):
        parse_definition(data)


# --------------------------------------------------------------------------- #
# Baseline package file (config/methods/company-research.v1.yaml)
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PACKAGE_FILE = REPO_ROOT / "config" / "methods" / "company-research.v1.yaml"
EXPECTATIONS_PACKAGE_FILE = REPO_ROOT / "config" / "methods" / "company-research.v2.expectations.yaml"


def test_load_package_file_reads_the_repo_baseline_package() -> None:
    loaded = load_package_file(BASELINE_PACKAGE_FILE)
    assert loaded.package_key == "company-research"
    assert loaded.schema_version == 1
    # description 只供人审阅,允许 YAML 与代码内基线措辞不同;置同值后其余
    # 内容(含 canonical hash)必须完全一致,两边不允许悄悄分叉。
    aligned = loaded.model_copy(update={"description": LEGACY_BASELINE_DEFINITION.description})
    assert canonical_hash(aligned) == canonical_hash(LEGACY_BASELINE_DEFINITION)
    assert canonical_definition(aligned) == canonical_definition(LEGACY_BASELINE_DEFINITION)


def test_baseline_package_adds_nothing_beyond_current_depth_requirements() -> None:
    loaded = load_package_file(BASELINE_PACKAGE_FILE)
    assert [step.id for step in loaded.steps if step.blocking] == [
        "falsifiable_plan",
        "dimension_assessment",
    ]
    counter = next(step for step in loaded.steps if step.id == "counter_analysis")
    assert counter.blocking is False
    assert counter.required_outputs == ()
    assert loaded.discipline.require_two_counter_hypotheses is False
    assert loaded.discipline.require_verified_counter_evidence is False
    assert loaded.discipline.require_explicit_stop_decision is False


def test_expectations_package_extends_baseline_with_a_strengthening_step() -> None:
    baseline = load_package_file(BASELINE_PACKAGE_FILE)
    loaded = load_package_file(EXPECTATIONS_PACKAGE_FILE)
    assert loaded.package_key == baseline.package_key
    assert canonical_hash(loaded) != canonical_hash(baseline)
    baseline_steps = {step.id: step for step in baseline.steps}
    expectations_steps = {step.id: step for step in loaded.steps}
    # §8 strengthening: every baseline step preserved verbatim, exactly one new step.
    assert set(baseline_steps) < set(expectations_steps)
    for step_id, step in baseline_steps.items():
        assert expectations_steps[step_id] == step
    gap = expectations_steps["expectations_gap"]
    assert gap.instruction_kind == "expectations_gap"
    assert gap.blocking is True and gap.allow_missing is True
    assert gap.required_capabilities == ("valuation",)
    # 维度步骤不是 action-backed,依赖边会在结算时永久违规,因此不声明 depends_on。
    assert gap.depends_on == ()
    assert [type(requirement) for requirement in gap.required_outputs] == [
        CalculationRequirement,
        DimensionOutputRequirement,
    ]
    assert gap.required_outputs[0].method == "reverse_dcf_fcfe"
    assert gap.required_outputs[1].dimension == "price_expectations"
    assert gap.required_outputs[1].minimum_status == "qualitative"


def test_load_packages_reads_yaml_files_in_name_order(tmp_path: Path) -> None:
    (tmp_path / "b-company.yaml").write_text(
        yaml.safe_dump(_baseline_data(), allow_unicode=True), encoding="utf-8"
    )
    other = _baseline_data() | {"package_key": "aaa-other"}
    (tmp_path / "a-other.yaml").write_text(
        yaml.safe_dump(other, allow_unicode=True), encoding="utf-8"
    )
    # 卡片约定目录加载只读 *.yaml,.yml 不在范围内。
    (tmp_path / "c-skipped.yml").write_text(
        yaml.safe_dump(_baseline_data(), allow_unicode=True), encoding="utf-8"
    )
    packages = load_packages(tmp_path)
    assert [package.package_key for package in packages] == ["aaa-other", "company-research"]


def test_load_package_file_aggregates_problems_with_the_file_as_source(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("schema_version: 1\n", encoding="utf-8")  # 缺 package_key 与 steps
    with pytest.raises(MethodPackageError) as info:
        load_package_file(broken)
    assert info.value.source == str(broken)
    assert len(info.value.errors) >= 2

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(MethodPackageError) as info:
        load_package_file(empty)
    assert any("mapping" in message for message in info.value.errors)


# --------------------------------------------------------------------------- #
# Strengthening partial order (design §8): violations vs strict enhancements
# --------------------------------------------------------------------------- #


def _baseline_step(step_id: str) -> MethodStep:
    return next(step for step in LEGACY_BASELINE_DEFINITION.steps if step.id == step_id)


def _candidate(**overrides: Any) -> MethodPackageDefinition:
    """The legacy baseline with the given definition fields replaced."""

    return LEGACY_BASELINE_DEFINITION.model_copy(update=overrides)


def _steps(*revised: MethodStep) -> tuple[MethodStep, ...]:
    """All baseline steps, with the given ones replacing their same-id original."""

    replacements = {step.id: step for step in revised}
    return tuple(replacements.get(step.id, step) for step in LEGACY_BASELINE_DEFINITION.steps)


def _dimensions_with(dimension: str, **overrides: Any) -> tuple[DimensionRequirement, ...]:
    """The seven default dimension requirements with one entry overridden."""

    return tuple(
        requirement.model_copy(update=overrides)
        if requirement.dimension == dimension
        else requirement
        for requirement in default_dimensions()
    )


def _dimension_step_with(*extra_outputs: Any) -> MethodStep:
    """The baseline dimension assessment step with additional output demands."""

    return _baseline_step("dimension_assessment").model_copy(
        update={
            "required_outputs": _baseline_step("dimension_assessment").required_outputs + extra_outputs
        }
    )


def test_identical_packages_produce_no_output() -> None:
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, LEGACY_BASELINE_DEFINITION) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, LEGACY_BASELINE_DEFINITION) == ()


def test_presentation_only_changes_are_neither_violations_nor_enhancements() -> None:
    # 卡 11:仅改 description/title → violations 空且 strictly_stronger 空,
    # 上层(卡 12)据此以「无实质增强」拒绝。
    retitled = _candidate(
        description="只有展示文案变化",
        steps=tuple(
            step.model_copy(update={"title": "Retitled " + step.id})
            for step in LEGACY_BASELINE_DEFINITION.steps
        ),
    )
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, retitled) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, retitled) == ()


def test_applicable_kinds_shrink_violates_and_growth_strengthens() -> None:
    narrowed = _candidate(applicable_kinds=("company_research",))
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, narrowed) == [
        "applicable_kinds: removed kind(s) review"
    ]
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, narrowed) == ()

    widened = _candidate(applicable_kinds=("company_research",))
    assert strengthening_violations(widened, LEGACY_BASELINE_DEFINITION) == []
    assert strictly_stronger_requirements(widened, LEGACY_BASELINE_DEFINITION) == ("kinds:review",)


def test_discipline_relaxation_violates_and_tightening_strengthens() -> None:
    tightened = _candidate(discipline=MethodConfig(require_verified_counter_evidence=True))
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, tightened) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, tightened) == (
        "discipline:require_verified_counter_evidence",
    )
    assert strengthening_violations(tightened, LEGACY_BASELINE_DEFINITION) == [
        "discipline.require_verified_counter_evidence: switch cannot be turned off (was true)"
    ]
    assert strictly_stronger_requirements(tightened, LEGACY_BASELINE_DEFINITION) == ()


def test_v1_comparison_uses_the_virtual_legacy_baseline() -> None:
    # 卡 11:v1 旧方法的比较基准是 legacy_package_for(旧 config);v1 的三个
    # 开关是仅有的可变研究要求,因此 v2 元数据(包哈希等)天然不算研究增强。
    stronger_v1 = legacy_package_for(MethodConfig(require_two_counter_hypotheses=True))
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, stronger_v1) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, stronger_v1) == (
        "discipline:require_two_counter_hypotheses",
    )
    weakened_v1 = legacy_package_for(MethodConfig())
    assert strictly_stronger_requirements(stronger_v1, weakened_v1) == ()
    assert strengthening_violations(stronger_v1, weakened_v1) == [
        "discipline.require_two_counter_hypotheses: switch cannot be turned off (was true)"
    ]


def test_legacy_package_for_swaps_only_the_discipline() -> None:
    config = MethodConfig(require_explicit_stop_decision=True)
    legacy = legacy_package_for(config)
    assert legacy.discipline == config
    assert legacy.steps == LEGACY_BASELINE_DEFINITION.steps
    assert LEGACY_BASELINE_DEFINITION.discipline == MethodConfig()  # 共享基线未被改动
    realigned = legacy.model_copy(update={"discipline": MethodConfig()})
    assert canonical_hash(realigned) == canonical_hash(LEGACY_BASELINE_DEFINITION)


def test_removed_step_violates_and_renaming_counts_as_removal() -> None:
    removed = _candidate(
        steps=tuple(step for step in LEGACY_BASELINE_DEFINITION.steps if step.id != "counter_analysis")
    )
    assert "steps.counter_analysis: step removed or renamed" in strengthening_violations(
        LEGACY_BASELINE_DEFINITION, removed
    )

    renamed = _candidate(steps=(
        _baseline_step("falsifiable_plan"),
        _baseline_step("counter_analysis").model_copy(update={"id": "counter_review"}),
        _baseline_step("dimension_assessment"),
    ))
    violations = strengthening_violations(LEGACY_BASELINE_DEFINITION, renamed)
    assert "steps.counter_analysis: step removed or renamed" in violations
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, renamed) == ()


def test_instruction_kind_change_violates() -> None:
    swapped = _candidate(steps=_steps(
        _baseline_step("falsifiable_plan").model_copy(update={"instruction_kind": "counter_analysis"}),
    ))
    violations = strengthening_violations(LEGACY_BASELINE_DEFINITION, swapped)
    assert any(
        message.startswith("steps.falsifiable_plan.instruction_kind") and "'counter_analysis'" in message
        for message in violations
    )


def test_blocking_demotion_violates_and_promotion_strengthens() -> None:
    demoted = _candidate(steps=_steps(
        _baseline_step("dimension_assessment").model_copy(update={"blocking": False}),
    ))
    assert (
        "steps.dimension_assessment.blocking: blocking step cannot become optional"
        in strengthening_violations(LEGACY_BASELINE_DEFINITION, demoted)
    )

    promoted = _candidate(steps=_steps(
        _baseline_step("counter_analysis").model_copy(update={"blocking": True}),
    ))
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, promoted) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, promoted) == ("blocking:counter_analysis",)


def test_step_gap_allowance_relaxed_violates_and_tightened_strengthens() -> None:
    with_gap = _candidate(steps=_steps(
        _baseline_step("dimension_assessment").model_copy(update={"allow_missing": True}),
    ))
    assert (
        "steps.dimension_assessment.allow_missing: gap allowance cannot be introduced (was false)"
        in strengthening_violations(LEGACY_BASELINE_DEFINITION, with_gap)
    )
    assert strengthening_violations(with_gap, LEGACY_BASELINE_DEFINITION) == []
    assert strictly_stronger_requirements(with_gap, LEGACY_BASELINE_DEFINITION) == (
        "allow_missing:dimension_assessment",
    )


def test_depends_on_shrink_violates_and_growth_strengthens() -> None:
    dependent = _candidate(steps=_steps(
        _baseline_step("counter_analysis").model_copy(update={"depends_on": ("falsifiable_plan",)}),
    ))
    assert (
        "steps.counter_analysis.depends_on: removed dependency falsifiable_plan"
        in strengthening_violations(dependent, LEGACY_BASELINE_DEFINITION)
    )
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, dependent) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, dependent) == (
        "depends_on:counter_analysis:falsifiable_plan",
    )


def test_dependency_cycle_in_the_new_graph_violates() -> None:
    cycled = _candidate(steps=(
        _baseline_step("falsifiable_plan").model_copy(update={"depends_on": ("dimension_assessment",)}),
        _baseline_step("counter_analysis"),
        _baseline_step("dimension_assessment").model_copy(update={"depends_on": ("falsifiable_plan",)}),
    ))
    violations = strengthening_violations(LEGACY_BASELINE_DEFINITION, cycled)
    assert any("cycle" in message and "falsifiable_plan" in message for message in violations)


def test_capability_precondition_change_violates_in_both_directions() -> None:
    # §8:既有步骤的 required_capabilities 是适用性约束,必须不变,增减都算违反。
    preconditioned = _candidate(steps=_steps(
        _baseline_step("counter_analysis").model_copy(update={"required_capabilities": ("evidence",)}),
    ))
    for old, new in (
        (preconditioned, LEGACY_BASELINE_DEFINITION),  # 减少
        (LEGACY_BASELINE_DEFINITION, preconditioned),  # 增加
    ):
        violations = strengthening_violations(old, new)
        assert any(
            message.startswith("steps.counter_analysis.required_capabilities") for message in violations
        ), (old, new, violations)
        assert strictly_stronger_requirements(old, new) == ()


def test_output_requirement_removal_violates() -> None:
    dropped_plan = _candidate(steps=_steps(
        _baseline_step("falsifiable_plan").model_copy(update={"required_outputs": (
            HypothesesRequirement(type="hypotheses", min_count=1),
        )}),
    ))
    assert "steps.falsifiable_plan.required_outputs.plan: required output removed" in strengthening_violations(
        LEGACY_BASELINE_DEFINITION, dropped_plan
    )
    dropped_dimension = _candidate(steps=_steps(
        _baseline_step("dimension_assessment").model_copy(update={"required_outputs": tuple(
            output
            for output in _baseline_step("dimension_assessment").required_outputs
            if output.dimension != "financial_cash"
        )}),
    ))
    assert (
        "steps.dimension_assessment.required_outputs.dimension.financial_cash: required output removed"
        in strengthening_violations(LEGACY_BASELINE_DEFINITION, dropped_dimension)
    )


def test_min_count_lowered_violates_and_raised_strengthens() -> None:
    two_hypotheses = _candidate(steps=_steps(
        _baseline_step("falsifiable_plan").model_copy(update={"required_outputs": (
            PlanRequirement(type="plan", require_completion_criteria=True, require_stop_conditions=True),
            HypothesesRequirement(type="hypotheses", min_count=2),
        )}),
    ))
    assert (
        "steps.falsifiable_plan.required_outputs.hypotheses.min_count: lowered from 2 to 1"
        in strengthening_violations(two_hypotheses, LEGACY_BASELINE_DEFINITION)
    )
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, two_hypotheses) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, two_hypotheses) == (
        "outputs:falsifiable_plan:hypotheses:min_count",
    )


def test_output_flag_tightening_strengthens_and_relaxing_violates() -> None:
    open_plan = _candidate(steps=_steps(
        _baseline_step("falsifiable_plan").model_copy(update={"required_outputs": (
            PlanRequirement(type="plan", require_completion_criteria=True, require_stop_conditions=False),
            HypothesesRequirement(type="hypotheses", min_count=1),
        )}),
    ))
    assert strictly_stronger_requirements(open_plan, LEGACY_BASELINE_DEFINITION) == (
        "outputs:falsifiable_plan:plan:require_stop_conditions",
    )
    assert strengthening_violations(open_plan, LEGACY_BASELINE_DEFINITION) == []
    assert (
        "steps.falsifiable_plan.required_outputs.plan.require_stop_conditions: relaxed from true to false"
        in strengthening_violations(LEGACY_BASELINE_DEFINITION, open_plan)
    )

    unverified = _candidate(steps=_steps(
        _baseline_step("counter_analysis").model_copy(update={"required_outputs": (
            CounterRefsRequirement(type="counter_refs", min_count=1, verified_only=False),
        )}),
    ))
    verified = _candidate(steps=_steps(
        _baseline_step("counter_analysis").model_copy(update={"required_outputs": (
            CounterRefsRequirement(type="counter_refs", min_count=1, verified_only=True),
        )}),
    ))
    assert strictly_stronger_requirements(unverified, verified) == (
        "outputs:counter_analysis:counter_refs:verified_only",
    )
    assert strengthening_violations(unverified, verified) == []
    assert (
        "steps.counter_analysis.required_outputs.counter_refs.verified_only: relaxed from true to false"
        in strengthening_violations(verified, unverified)
    )

    plain_peer = _candidate(steps=_steps(
        _baseline_step("counter_analysis").model_copy(update={"required_outputs": (
            PeerContextRequirement(type="peer_context", require_calculation=False),
        )}),
    ))
    calculating_peer = _candidate(steps=_steps(
        _baseline_step("counter_analysis").model_copy(update={"required_outputs": (
            PeerContextRequirement(type="peer_context", require_calculation=True),
        )}),
    ))
    assert strictly_stronger_requirements(plain_peer, calculating_peer) == (
        "outputs:counter_analysis:peer_context:require_calculation",
    )
    assert strengthening_violations(plain_peer, calculating_peer) == []
    assert (
        "steps.counter_analysis.required_outputs.peer_context.require_calculation: relaxed from true to false"
        in strengthening_violations(calculating_peer, plain_peer)
    )


def test_calculation_method_set_shrinking_violates_and_growth_strengthens() -> None:
    with_reverse = _candidate(steps=_steps(_dimension_step_with(
        CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"),
    )))
    with_both = _candidate(steps=_steps(_dimension_step_with(
        CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"),
        CalculationRequirement(type="calculation", method="historical_valuation_percentile"),
    )))
    assert (
        "steps.dimension_assessment.required_outputs.calculation.historical_valuation_percentile: required output removed"
        in strengthening_violations(with_both, with_reverse)
    )
    assert strengthening_violations(with_reverse, with_both) == []
    assert strictly_stronger_requirements(with_reverse, with_both) == (
        "outputs:dimension_assessment:calculation:historical_valuation_percentile",
    )


def test_snapshot_field_set_shrinking_violates_and_growth_strengthens() -> None:
    with_close = _candidate(steps=_steps(_dimension_step_with(
        SnapshotFieldRequirement(type="snapshot_field", field="close"),
    )))
    with_close_and_roe = _candidate(steps=_steps(_dimension_step_with(
        SnapshotFieldRequirement(type="snapshot_field", field="close"),
        SnapshotFieldRequirement(type="snapshot_field", field="roe"),
    )))
    assert (
        "steps.dimension_assessment.required_outputs.snapshot_field.roe: required output removed"
        in strengthening_violations(with_close_and_roe, with_close)
    )
    assert strengthening_violations(with_close, with_close_and_roe) == []
    assert strictly_stronger_requirements(with_close, with_close_and_roe) == (
        "outputs:dimension_assessment:snapshot_field:roe",
    )


def test_dimension_minimum_status_raise_strengthens_and_lower_violates() -> None:
    evidenced = _baseline_step("dimension_assessment").model_copy(update={"required_outputs": (
        DimensionOutputRequirement(type="dimension", dimension="financial_cash", minimum_status="evidenced"),
        *(
            output
            for output in _baseline_step("dimension_assessment").required_outputs
            if output.dimension != "financial_cash"
        ),
    )})
    raised = _candidate(steps=_steps(evidenced))
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, raised) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, raised) == ("dimension_min:financial_cash",)
    assert (
        "steps.dimension_assessment.required_outputs.dimension.financial_cash.minimum_status:"
        " lowered from 'evidenced' to 'missing'"
        in strengthening_violations(raised, LEGACY_BASELINE_DEFINITION)
    )


def test_package_dimension_requirements_follow_the_same_order() -> None:
    qualitative = _candidate(dimensions=_dimensions_with("price_expectations", minimum_status="qualitative"))
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, qualitative) == []
    assert "dimension_min:price_expectations" in strictly_stronger_requirements(
        LEGACY_BASELINE_DEFINITION, qualitative
    )
    assert strictly_stronger_requirements(qualitative, LEGACY_BASELINE_DEFINITION) == ()
    assert (
        "dimensions.price_expectations.minimum_status: lowered from 'qualitative' to 'missing'"
        in strengthening_violations(qualitative, LEGACY_BASELINE_DEFINITION)
    )

    gapped = _candidate(dimensions=_dimensions_with("financial_cash", allow_missing=True))
    assert (
        "dimensions.financial_cash.allow_missing: relaxed from false to true"
        in strengthening_violations(LEGACY_BASELINE_DEFINITION, gapped)
    )
    assert strictly_stronger_requirements(gapped, LEGACY_BASELINE_DEFINITION) == (
        "dimension_allow_missing:financial_cash",
    )

    strict_na = _candidate(dimensions=_dimensions_with("financial_cash", allow_not_applicable=False))
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, strict_na) == (
        "dimension_allow_not_applicable:financial_cash",
    )
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, strict_na) == []
    assert (
        "dimensions.financial_cash.allow_not_applicable: relaxed from false to true"
        in strengthening_violations(strict_na, LEGACY_BASELINE_DEFINITION)
    )


def test_new_blocking_step_with_outputs_is_a_strict_enhancement() -> None:
    gap_step = MethodStep(
        id="expectations_gap",
        title="Expectations gap",
        instruction_kind="expectations_gap",
        blocking=True,
        required_outputs=(
            CalculationRequirement(type="calculation", method="reverse_dcf_fcfe"),
            DimensionOutputRequirement(type="dimension", dimension="price_expectations", minimum_status="qualitative"),
        ),
    )
    candidate = _candidate(steps=(*LEGACY_BASELINE_DEFINITION.steps, gap_step))
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, candidate) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, candidate) == (
        "blocking:expectations_gap",
        "dimension_min:price_expectations",
        "outputs:expectations_gap:calculation:reverse_dcf_fcfe",
        "outputs:expectations_gap:dimension:price_expectations",
    )


def test_new_optional_step_without_outputs_adds_no_research_requirement() -> None:
    advisory = MethodStep(
        id="optional_note",
        title="Optional note",
        instruction_kind="counter_analysis",
        blocking=False,
    )
    candidate = _candidate(steps=(*LEGACY_BASELINE_DEFINITION.steps, advisory))
    assert strengthening_violations(LEGACY_BASELINE_DEFINITION, candidate) == []
    assert strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, candidate) == ()


def test_strict_enhancement_ids_are_sorted_and_deduplicated() -> None:
    # 同一维度的最低状态在步骤输出与包级 requirements 同时提高 → 只报一个 id。
    raised_step = _baseline_step("dimension_assessment").model_copy(update={"required_outputs": (
        DimensionOutputRequirement(type="dimension", dimension="financial_cash", minimum_status="evidenced"),
        *(
            output
            for output in _baseline_step("dimension_assessment").required_outputs
            if output.dimension != "financial_cash"
        ),
    )})
    raised = _candidate(
        steps=_steps(raised_step),
        dimensions=_dimensions_with("financial_cash", minimum_status="qualitative"),
    )
    stronger = strictly_stronger_requirements(LEGACY_BASELINE_DEFINITION, raised)
    assert stronger.count("dimension_min:financial_cash") == 1
    assert stronger == tuple(sorted(set(stronger)))


def test_expectations_package_is_a_pure_strengthening_of_the_baseline() -> None:
    baseline = load_package_file(BASELINE_PACKAGE_FILE)
    expectations = load_package_file(EXPECTATIONS_PACKAGE_FILE)
    assert strengthening_violations(baseline, expectations) == []
    assert strictly_stronger_requirements(baseline, expectations) == (
        "blocking:expectations_gap",
        "dimension_min:price_expectations",
        "outputs:expectations_gap:calculation:reverse_dcf_fcfe",
        "outputs:expectations_gap:dimension:price_expectations",
    )
    # 能力前置是适用性约束,不进入质量偏序(§8):新步骤的 valuation 能力不算增强项。
    assert "outputs:expectations_gap:required_capabilities:valuation" not in strictly_stronger_requirements(
        baseline, expectations
    )


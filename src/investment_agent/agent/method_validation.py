"""Package-level method validation: first plan baseline and final settlement.

Card 07, design §6.2. This module proves the *structural* relation between the
frozen method package, the first committed plan, the final step results and the
task's actually committed tool actions - it never claims semantic research
quality. It runs only for tasks whose frozen receipt says
``validator_version == VALIDATOR_V1`` (enforcing); v1 tasks without a receipt
and render-only receipts never reach it, so their behavior is unchanged.

Every check re-reads only frozen admission data (receipt binding, package
artifact behind the frozen ref, frozen grant) plus this task's committed
actions - never a live head, never a live package version, never the global
tool-family mapping (§6.2 item 7). Errors use the existing
:class:`ResearchValidationError` code/field/message triple; the repairable set
lives on the error class and the bounded ``agent_retries`` feedback mechanism
is unchanged.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import ExecutionGrant, ResearchValidationError
from investment_agent.agent.method_receipt import TaskMethodReceipt, render_method
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.method_packages import (
    CalculationRequirement,
    CounterRefsRequirement,
    DimensionOutputRequirement,
    HypothesesRequirement,
    MethodPackage,
    MethodPackageDefinition,
    MethodStep,
    OutputRequirement,
    PeerContextRequirement,
    PlanRequirement,
    SnapshotFieldRequirement,
    ToolFamily,
    canonical_hash,
    resolve_family_members,
)
from investment_agent.investor.methods import MethodBinding
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence

if TYPE_CHECKING:
    from investment_agent.agent.research import MethodPlannedProgress

_log = logging.getLogger(__name__)

STATUS_ORDER = {"missing": 0, "qualitative": 1, "evidenced": 2}
"""§4.2 dimension status partial order: missing < qualitative < evidenced."""

ACTION_BACKED_REQUIREMENTS = (CalculationRequirement, SnapshotFieldRequirement, PeerContextRequirement)
"""Output requirement kinds whose evidence must come from committed tool actions."""


@dataclass(frozen=True)
class MethodValidationContext:
    """The frozen §6.2 inputs of one v2 settlement check.

    ``committed_actions`` holds every committed ToolAction of the task ordered
    by ``committed_sequence`` ascending (NULLs last - historical actions carry
    no order information). ``first_plan`` is the earliest committed research
    progress by task artifact append order, or None for tasks that never
    committed one (historical in-flight tasks are skipped, not failed).
    ``session`` is the request-scoped session the checks resolve saved
    evidence/calculation rows through; it is transport, never frozen state.
    """

    task_id: UUID
    binding: MethodBinding
    package: MethodPackageDefinition
    receipt: TaskMethodReceipt
    grant: ExecutionGrant
    session: AsyncSession
    first_plan: MethodPlannedProgress | None
    committed_actions: tuple[dict[str, Any], ...]
    artifact_refs: tuple[ObjectRef, ...]


async def build_context(
    session: AsyncSession, *, task_id: UUID, receipt: TaskMethodReceipt, grant: ExecutionGrant
) -> MethodValidationContext:
    """Load the frozen context: package artifact, baseline plan, committed actions."""
    from investment_agent.agent.tasks import TaskService
    from investment_agent.investor.foundations import InvestorError
    from investment_agent.investor.methods import MethodService

    binding = receipt.binding
    assert binding.package_ref is not None and binding.package_hash is not None
    package = MethodPackage.model_validate(
        await MethodService(session, grant.owner).package_by_ref(binding.package_ref)
    )
    # §4.3: hash and config are re-verified together; a mismatch is storage
    # corruption and fails closed instead of settling against drifted content.
    if canonical_hash(package) != binding.package_hash or binding.config != package.discipline:
        raise InvestorError("method_package_corrupt", "方法包工件与绑定不一致，结算校验拒绝")
    task = await TaskService(session).get_task(task_id)
    committed_actions = await _committed_actions(session, task_id)
    return MethodValidationContext(
        task_id=task_id,
        binding=binding,
        package=package,
        receipt=receipt,
        grant=grant,
        session=session,
        first_plan=await _first_committed_plan(session, task),
        committed_actions=committed_actions,
        artifact_refs=tuple(task.artifact_refs),
    )


async def _committed_actions(session: AsyncSession, task_id: UUID) -> tuple[dict[str, Any], ...]:
    """Every committed ToolAction of the task, ``committed_sequence`` ascending.

    Historical actions carry no order information: their NULL sequence is
    preserved as ``None`` and sorts last, never fabricated into an order.
    """
    rows = (
        await session.scalars(
            select(ToolAction).where(ToolAction.task_id == str(task_id), ToolAction.state == "committed")
        )
    ).all()
    ordered = sorted(rows, key=lambda row: (row.committed_sequence is None, row.committed_sequence or 0))
    return tuple(
        {
            "operation_id": UUID(row.operation_id),
            "tool": str(row.parameters.get("tool", "")),
            "parameters": row.parameters,
            "result": row.result or {},
            "committed_sequence": row.committed_sequence,
        }
        for row in ordered
    )


async def _first_committed_plan(session: AsyncSession, task: Any) -> Any:
    """Earliest committed research progress by artifact append order (§6.2).

    Shares the traversal with ``agent/progress.py``: artifact refs are read in
    task append order (the projection reads the same list from the end for the
    latest), never in SQL order. Rows that no longer parse are skipped, never
    guessed.
    """
    from investment_agent.agent.progress import committed_progress
    from investment_agent.agent.research import MethodPlannedProgress

    found = await committed_progress(session, task, MethodPlannedProgress)
    return found[0][1] if found else None


# --------------------------------------------------------------------------- #
# First committed plan (§6.2 首次计划)
# --------------------------------------------------------------------------- #


def validate_first_plan(context: MethodValidationContext, progress: Any) -> None:
    """The first committed save must be a plan: mappings valid, no results yet."""
    steps = {step.id for step in context.package.steps}
    seen: set[tuple[int, str]] = set()
    for index, binding in enumerate(progress.plan_bindings):
        field = f"progress.plan_bindings.{index}"
        if binding.evidence_step_index >= len(progress.evidence_steps):
            raise ResearchValidationError("method_plan_binding_invalid", field,
                "计划映射的 evidence_step_index 必须指向已声明的 evidence step。")
        if binding.method_step_id not in steps:
            raise ResearchValidationError("method_plan_binding_invalid", field,
                "计划映射的 method_step_id 必须存在于冻结方法包中。")
        pair = (binding.evidence_step_index, binding.method_step_id)
        if pair in seen:
            raise ResearchValidationError("method_plan_binding_invalid", field,
                "同一 (evidence_step_index, method_step_id) 映射不得重复。")
        seen.add(pair)
    covered = {method_step_id for _, method_step_id in seen}
    for step in context.package.steps:
        if step.blocking and step.id not in covered:
            raise ResearchValidationError("method_step_required", "progress.plan_bindings",
                f"blocking 步骤 {step.id} 必须至少被一个 evidence step 覆盖。")
    if progress.step_results:
        raise ResearchValidationError("method_step_result_premature", "progress.step_results",
            "首次计划只能声明 evidence_steps 与 plan_bindings；步骤结果在最终结算时提交。")


# --------------------------------------------------------------------------- #
# Final settlement (§6.2 最终结算)
# --------------------------------------------------------------------------- #


async def validate_final(session: AsyncSession, context: MethodValidationContext, progress: Any) -> None:
    """Settle every declared step result against the frozen package and actions."""
    first = context.first_plan
    if first is None:
        # Historical in-flight tasks without a first committed plan are not
        # judged here; the skip is explicit, never a silent pass.
        _log.info("method_settlement_skipped_no_first_plan committed_actions=%d", len(context.committed_actions))
        return
    if progress.evidence_steps != first.evidence_steps or progress.plan_bindings != first.plan_bindings:
        raise ResearchValidationError("method_plan_changed", "progress.evidence_steps",
            "最终提交必须保持首次 committed 计划的 evidence_steps 与 plan_bindings 不变；补充结果只能写入 step_results。")
    steps = {step.id: step for step in context.package.steps}
    results = _results_by_step(steps, progress)
    _settle_within_first_plan(first, results)
    for step in context.package.steps:
        if step.blocking and len(results.get(step.id, ())) != 1:
            raise ResearchValidationError("method_step_required", f"progress.step_results.{step.id}",
                f"blocking 步骤 {step.id} 必须有且仅有一个最终 step result。")
    _verify_action_authenticity(context, results)
    for step in context.package.steps:
        for result in results.get(step.id, ()):
            if result.status == "completed_with_gap":
                _validate_gap_close(step, result, progress)
            else:
                for position, requirement in enumerate(step.required_outputs):
                    await _validate_requirement(session, context, step, result, progress, requirement,
                        field=f"progress.step_results.{step.id}.required_outputs.{position}")
    _verify_dependency_order(context, results)
    _reverify_capabilities(context, results)


def _results_by_step(steps: dict[str, MethodStep], progress: Any) -> dict[str, list[Any]]:
    """Group step results; a result naming an unknown step id is an invalid mapping."""
    grouped: dict[str, list[Any]] = {}
    for index, result in enumerate(progress.step_results):
        if result.method_step_id not in steps:
            raise ResearchValidationError("method_plan_binding_invalid",
                f"progress.step_results.{index}.method_step_id",
                "step result 的 method_step_id 必须存在于冻结方法包中。")
        grouped.setdefault(result.method_step_id, []).append(result)
    return grouped


def _settle_within_first_plan(first: Any, results: dict[str, list[Any]]) -> None:
    """Each result's evidence_step_indices stay within the first-plan bindings."""
    bound: dict[str, set[int]] = {}
    for binding in first.plan_bindings:
        bound.setdefault(binding.method_step_id, set()).add(binding.evidence_step_index)
    for step_id, entries in results.items():
        for result in entries:
            if not set(result.evidence_step_indices) <= bound.get(step_id, set()):
                raise ResearchValidationError("method_plan_changed",
                    f"progress.step_results.{step_id}.evidence_step_indices",
                    f"步骤 {step_id} 的结果只能引用首次计划绑定的 evidence step。")


def _verify_action_authenticity(context: MethodValidationContext, results: dict[str, list[Any]]) -> None:
    """Every claimed action is this task's committed action inside the frozen grant."""
    by_operation = {action["operation_id"]: action for action in context.committed_actions}
    for step_id, entries in results.items():
        for result in entries:
            for action_id in result.action_ids:
                action = by_operation.get(action_id)
                if action is None:
                    raise ResearchValidationError("method_action_invalid",
                        f"progress.step_results.{step_id}.action_ids",
                        "action_ids 必须全部属于本任务且已提交，不能引用其他任务或未提交动作。")
                if action["tool"] not in context.grant.tool_names:
                    raise ResearchValidationError("method_action_invalid",
                        f"progress.step_results.{step_id}.action_ids",
                        f"动作工具 {action['tool']} 不在本任务冻结授权范围内。")


async def _validate_requirement(
    session: AsyncSession,
    context: MethodValidationContext,
    step: MethodStep,
    result: Any,
    progress: Any,
    requirement: OutputRequirement,
    *,
    field: str,
) -> None:
    """One typed OutputRequirement of one satisfied step result (§4.2, §6.2 item 4)."""
    if isinstance(requirement, PlanRequirement):
        if requirement.require_completion_criteria and not progress.completion_criteria:
            raise _output_required(field, "该步骤要求 completion_criteria 非空。")
        if requirement.require_stop_conditions and not progress.stop_conditions:
            raise _output_required(field, "该步骤要求 stop_conditions 非空。")
    elif isinstance(requirement, HypothesesRequirement):
        indices = result.hypothesis_indices
        if (len(set(indices)) != len(indices) or len(indices) < requirement.min_count
                or any(index < 0 or index >= len(progress.hypotheses) for index in indices)):
            raise _output_required(field,
                f"hypothesis_indices 必须指向 {requirement.min_count} 个互不相同的已声明假设。")
    elif isinstance(requirement, CounterRefsRequirement):
        evidence_refs = [ref for ref in result.output_refs if ref.kind == "evidence"]
        if len(evidence_refs) < requirement.min_count:
            raise _output_required(field,
                f"output_refs 必须包含至少 {requirement.min_count} 条 evidence 反证引用。")
        if not set(evidence_refs) <= set(progress.counter_refs):
            raise _output_required(field, "反证引用必须全部出现在 progress.counter_refs 中。")
        if requirement.verified_only:
            await _require_verified_counter(session, context, evidence_refs, field)
    elif isinstance(requirement, DimensionOutputRequirement):
        assessment = next((item for item in progress.dimensions if item.dimension == requirement.dimension), None)
        if assessment is None or STATUS_ORDER[assessment.status] < STATUS_ORDER[requirement.minimum_status]:
            raise _output_required(field,
                f"维度 {requirement.dimension} 必须达到 {requirement.minimum_status} 状态。")
    elif isinstance(requirement, CalculationRequirement):
        await _validate_calculation(session, context, result, progress, requirement, field)
    elif isinstance(requirement, SnapshotFieldRequirement):
        _validate_snapshot_field(context, result, requirement, field)
    else:
        assert isinstance(requirement, PeerContextRequirement)
        await _validate_peer_context(session, context, result, requirement, field)


def _output_required(field: str, message: str) -> ResearchValidationError:
    return ResearchValidationError("method_step_output_required", field, message)


def _action_invalid(field: str, message: str) -> ResearchValidationError:
    return ResearchValidationError("method_action_invalid", field, message)


async def _validate_calculation(
    session: AsyncSession,
    context: MethodValidationContext,
    result: Any,
    progress: Any,
    requirement: CalculationRequirement,
    field: str,
) -> None:
    """The cited calculation must be the exact method, in the progress ref sets, action-generated."""
    from investment_agent.industry_peers.resolver import peer_method_payload

    matched: ObjectRef | None = None
    for ref in result.output_refs:
        if ref.kind != "calculation":
            continue
        row = await session.get(SavedCalculation, (str(ref.id), ref.revision))
        if row is None or row.owner_id != context.grant.owner:
            continue
        if await peer_method_payload(session, context.grant.owner, ref) is not None:
            continue  # peer-method calculations belong to the peer requirement
        if row.payload.get("method") == requirement.method:
            matched = ref
            break
    if matched is None:
        raise _output_required(field,
            f"output_refs 必须包含 method={requirement.method} 的已保存 calculation；其他方法的计算不能冒充。")
    if matched not in _progress_ref_set(progress):
        raise _output_required(field, "calculation 引用必须同时进入 progress 的相应引用集合。")
    for action in context.committed_actions:
        if action["tool"] == "save_research_progress":
            continue  # progress saves never generate calculations
        if _result_contains_ref(action["result"], matched):
            if action["operation_id"] not in result.action_ids:
                raise _action_invalid(f"{field}.action_ids",
                    "action_ids 必须包含生成该 calculation 的已提交动作。")
            return
    raise _action_invalid(f"{field}.action_ids", "calculation 引用必须由本任务已提交动作生成或读取。")


async def _require_verified_counter(
    session: AsyncSession, context: MethodValidationContext, refs: list[ObjectRef], field: str
) -> None:
    """Same per-ref check as ``enforce_steps`` verified counter evidence."""
    from investment_agent.agent.observation import effective_cutoff
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.investor.publication_numbers import NumericResolver

    cutoff = await effective_cutoff(session, context.task_id)
    for ref in refs:
        row = await session.get(SavedEvidence, (str(ref.id), ref.revision))
        if row is None:
            raise _output_required(field, "反证必须为已验证、无未解冲突的原始证据。")
        subject = ObjectRef.model_validate(row.payload["subject_ref"])
        fact = await NumericResolver(EvidenceService(session), subject, cutoff).fact(ref, require_fresh=False)
        if fact.verification != "verified" or fact.conflict == "unresolved":
            raise _output_required(field, "反证必须为已验证、无未解冲突的原始证据。")


def _validate_snapshot_field(
    context: MethodValidationContext, result: Any, requirement: SnapshotFieldRequirement, field: str
) -> None:
    captures = [
        action
        for action in context.committed_actions
        if action["tool"] == "capture_snapshot"
        and isinstance(action["parameters"].get("business"), dict)
        and action["parameters"]["business"].get("field") == requirement.field
    ]
    for ref in (ref for ref in result.output_refs if ref.kind == "evidence"):
        for action in captures:
            if _result_contains_ref(action["result"], ref):
                if action["operation_id"] not in result.action_ids:
                    raise _action_invalid(f"{field}.action_ids",
                        f"action_ids 必须包含保存字段 {requirement.field} 的 committed capture_snapshot 动作。")
                return
    raise _output_required(field,
        f"必须引用由 committed capture_snapshot 保存的字段 {requirement.field} 的已保存 evidence。")


async def _validate_peer_context(
    session: AsyncSession, context: MethodValidationContext, result: Any, requirement: PeerContextRequirement, field: str
) -> None:
    from investment_agent.industry_peers.metric_packs import BAIJIU_PACK
    from investment_agent.industry_peers.resolver import peer_method_payload

    peer_actions = [action for action in context.committed_actions if action["tool"] == "get_industry_peer_context"]
    if not any(action["operation_id"] in result.action_ids for action in peer_actions):
        raise _output_required(field, "action_ids 必须包含本任务已提交的 get_industry_peer_context 动作。")
    if not requirement.require_calculation:
        return
    binding = context.grant.peer_context
    if binding is None:
        raise _output_required(field, "任务未冻结同业上下文，无法绑定同业计算。")
    for ref in (ref for ref in result.output_refs if ref.kind == "calculation"):
        # peer_method_payload returns the payload only for this method's own
        # calculation, so the routing helper is also the method identity check.
        payload = await peer_method_payload(session, context.grant.owner, ref)
        if payload is None:
            continue
        details = payload.get("details") or {}
        cohort = details.get("cohort") or {}
        try:
            cohort_ref = ObjectRef.model_validate(cohort.get("ref"))
        except ValidationError:
            continue
        if cohort_ref == binding.cohort_ref and details.get("metric_pack", BAIJIU_PACK) == binding.metric_pack:
            return
    raise _output_required(field, "output_refs 必须包含绑定任务冻结同业上下文的 calculation。")


def _validate_gap_close(step: MethodStep, result: Any, progress: Any) -> None:
    """§6.2 item 6: a gap closes a step only under the package's gap policy."""
    field = f"progress.step_results.{step.id}"
    if not step.allow_missing:
        raise ResearchValidationError("method_step_gap_invalid", field,
            f"步骤 {step.id} 不允许 completed_with_gap；satisfied 需要真实的输出要求落地。")
    reason = (result.gap_reason or "").strip()
    if not reason:
        raise ResearchValidationError("method_step_gap_invalid", field, "带缺口完成必须提供 gap_reason。")
    explanations = tuple(text.strip() for text in (*progress.gaps, *progress.limitations) if text.strip())
    if not any(reason in text or text in reason for text in explanations):
        raise ResearchValidationError("method_step_gap_invalid", field,
            "gaps 或 limitations 中必须保留与 gap_reason 对应的说明。")
    if any(isinstance(requirement, ACTION_BACKED_REQUIREMENTS) for requirement in step.required_outputs) and not result.action_ids:
        raise ResearchValidationError("method_step_gap_invalid", field,
            "action-backed 步骤的缺口必须保留至少一次真实尝试动作。")


def _verify_dependency_order(context: MethodValidationContext, results: dict[str, list[Any]]) -> None:
    """§6.2 item 5: A→B requires max(A sequences) < min(B sequences).

    Edges are judged between steps that both declared a result; a missing
    non-blocking result is a coverage gap handled by the blocking rules, not an
    order claim. Any side without committed action sequences (empty or NULL)
    is ``not_replayable``: it fails as a violation instead of passing.
    """
    sequences = _result_sequences(context, results)
    for step in context.package.steps:
        for dependency in step.depends_on:
            prerequisite, follower = sequences.get(dependency), sequences.get(step.id)
            if prerequisite is None or follower is None:
                continue
            field = f"progress.step_results.{step.id}"
            earlier, latest = _sequence_bounds(prerequisite), _sequence_bounds(follower)
            if earlier is None or latest is None:
                raise ResearchValidationError("method_step_order_violation", field,
                    f"依赖 {dependency} → {step.id} 缺少已提交动作顺序（历史动作无 committed_sequence），不能当作满足顺序。")
            if earlier[0] >= latest[1]:
                raise ResearchValidationError("method_step_order_violation", field,
                    f"依赖顺序违反：{dependency} 的动作 sequence 必须全部早于 {step.id} 的最早动作。")


def _sequence_bounds(values: list[int | None]) -> tuple[int, int] | None:
    """(max, min) of known sequences; None when empty or any sequence is NULL."""
    known = [value for value in values if value is not None]
    if not known or len(known) != len(values):
        return None
    return max(known), min(known)


def _result_sequences(context: MethodValidationContext, results: dict[str, list[Any]]) -> dict[str, list[int | None]]:
    """Per step: the committed sequences of every claimed action, or [] when absent."""
    by_operation = {action["operation_id"]: action for action in context.committed_actions}
    sequences: dict[str, list[int | None]] = {}
    for step_id, entries in results.items():
        collected: list[int | None] = []
        for result in entries:
            collected.extend(
                by_operation[operation_id]["committed_sequence"]
                for operation_id in result.action_ids
                if operation_id in by_operation
            )
        sequences[step_id] = collected
    return sequences


def _reverify_capabilities(context: MethodValidationContext, results: dict[str, list[Any]]) -> None:
    """§6.2 item 7: frozen family snapshot only, never live heads or mappings."""
    for step in context.package.steps:
        if step.id not in results:
            continue
        required: set[str] = set()
        for family in step.required_capabilities:
            required.update(context.receipt.tool_family_members.get(family, ()))
        if missing := sorted(required - set(context.grant.tool_names)):
            raise ResearchValidationError("method_capability_unavailable",
                f"progress.step_results.{step.id}",
                f"步骤能力缺失：{', '.join(missing)} 不在本任务冻结授权范围内。")


# --------------------------------------------------------------------------- #
# Historical three-state replay (design §7.1, card 13)
# --------------------------------------------------------------------------- #


ReplayState = Literal["pass", "block", "not_replayable"]
"""Per-requirement replay verdict: recomputed pass, recomputed failure, or
missing historical metadata (never treated as pass, §7.1/§10)."""


async def replay_context(
    session: AsyncSession,
    review: dict[str, Any],
    candidate_package: MethodPackage,
    *,
    renderer_version: str,
    validator_version: str,
) -> MethodValidationContext:
    """The frozen replay context of one review sample against a candidate package.

    Everything is read from the review's frozen data plus this task's immutable
    committed-action ledger - never a live head, never a live package version,
    never the review's old task binding with a swapped method config (§7). The
    binding carries the proposal-frozen package artifact and hash with the
    candidate discipline; ``method_ref`` is a synthetic identity (the review id)
    because a candidate has no method version before approval and replay never
    resolves one. The candidate must be the stored artifact payload: its
    ``artifact_id``/``version`` freeze ``package_ref`` and ``content_hash``
    must match the canonical hash, otherwise the stored evidence drifted and
    the replay fails closed.
    """
    from investment_agent.agent.contracts import Task
    from investment_agent.investor.foundations import InvestorError

    if canonical_hash(candidate_package) != candidate_package.content_hash:
        raise InvestorError("method_package_corrupt", "候选方法包工件与内容哈希不一致，回放拒绝")
    admission = review["inputs"].get("admission")
    if admission is None:
        raise InvestorError("invalid_reference", "历史回放需要复盘冻结的任务授权")
    task_id = UUID(review["task_id"])
    listings = [ObjectRef.model_validate(ref) for ref in review["inputs"]["task"]["binding"]["base_refs"] if ref["kind"] == "listing"]
    if len(listings) != 1:
        raise InvestorError("invalid_reference", "历史回放需要恰好一个明确证券范围")
    binding = MethodBinding(
        schema_version=2,
        method_ref=ObjectRef(kind="method", id=UUID(review["id"]), revision=1),
        config=candidate_package.discipline,
        listing_ref=listings[0],
        package_ref=ObjectRef(kind="package", id=candidate_package.artifact_id, revision=candidate_package.version),
        package_hash=candidate_package.content_hash,
    )
    rendered = render_method(candidate_package)
    members: dict[ToolFamily, tuple[str, ...]] = {}
    for step in candidate_package.steps:
        for family in step.required_capabilities:
            if family not in members:
                members[family] = resolve_family_members((family,))
    receipt = TaskMethodReceipt(
        binding=binding,
        rendered_method=rendered,
        rendered_method_hash=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        renderer_version=renderer_version,
        validator_version=validator_version,
        tool_family_members=members,
    )
    # The frozen task payload is the exact admission-time dump of the same
    # schema, so it revalidates as the contract; replay reads only its identity
    # and append-ordered artifact refs from it.
    frozen_task = Task.model_validate(review["inputs"]["task"])
    return MethodValidationContext(
        task_id=task_id,
        binding=binding,
        package=candidate_package,
        receipt=receipt,
        grant=ExecutionGrant.model_validate(admission),
        session=session,
        first_plan=await _first_committed_plan(session, frozen_task),
        committed_actions=await _committed_actions(session, task_id),
        artifact_refs=tuple(frozen_task.artifact_refs),
    )


async def replay_requirements(
    context: MethodValidationContext, progress: Any
) -> tuple[dict[str, ReplayState], list[str]]:
    """Per-requirement three-state replay of every blocking step (§7.1).

    Requirements that depend only on saved progress fields, saved outputs and
    committed action parameters are recomputed against the candidate package.
    Requirements whose evidence is plan-binding/step-result metadata that
    historical v1 outputs never carried (blocking coverage, exactly-one result,
    action_ids attribution, peer binding consistency, dependency order on
    NULL-sequence actions) are ``not_replayable`` - never a pass. The returned
    list holds the significant ids of the aggregate outcome: the finer-grained
    blocked requirement ids (comparable to
    :func:`investor.method_packages.strictly_stronger_requirements`) when any
    requirement blocks, otherwise every not-replayable id, otherwise empty.
    """
    replayed: dict[str, ReplayState] = {}
    blocked: list[str] = []
    not_replayable: list[str] = []
    for step in context.package.steps:
        if not step.blocking:
            continue
        for requirement_id in _structural_requirement_ids(step):
            replayed[requirement_id] = "not_replayable"
            not_replayable.append(requirement_id)
        for requirement in step.required_outputs:
            state, failure_ids = await _replay_requirement(context, step, progress, requirement)
            primary = _requirement_id(step, requirement)
            replayed[primary] = state
            if state == "block":
                blocked.extend(failure_ids)
            elif state == "not_replayable":
                not_replayable.append(primary)
    significant = _unique(blocked) if blocked else _unique(not_replayable)
    return replayed, significant


def replay_sample_state(replayed: dict[str, ReplayState]) -> ReplayState:
    """Sample-level aggregation (§7.1): any block wins, then not_replayable."""
    if "block" in replayed.values():
        return "block"
    if "not_replayable" in replayed.values():
        return "not_replayable"
    return "pass"


def _structural_requirement_ids(step: MethodStep) -> tuple[str, ...]:
    """Plan-binding coverage, result existence, action attribution and order.

    ``blocking:<id>`` matches the strengthening-order id of a newly blocking
    step; ``depends_on:<id>:<dependency>`` matches a newly required edge.
    Historical actions carry no ``committed_sequence``, so order can never be
    replayed (§7.1).
    """
    ids = [f"blocking:{step.id}", f"results:{step.id}", f"actions:{step.id}"]
    ids.extend(f"depends_on:{step.id}:{dependency}" for dependency in dict.fromkeys(step.depends_on))
    return tuple(ids)


def _requirement_id(step: MethodStep, requirement: OutputRequirement) -> str:
    """Primary replay id of one required output; keyed types carry their key."""
    if isinstance(requirement, DimensionOutputRequirement):
        return f"outputs:{step.id}:dimension:{requirement.dimension}"
    if isinstance(requirement, CalculationRequirement):
        return f"outputs:{step.id}:calculation:{requirement.method}"
    if isinstance(requirement, SnapshotFieldRequirement):
        return f"outputs:{step.id}:snapshot_field:{requirement.field}"
    return f"outputs:{step.id}:{requirement.type}"


async def _replay_requirement(
    context: MethodValidationContext, step: MethodStep, progress: Any, requirement: OutputRequirement
) -> tuple[ReplayState, list[str]]:
    """Recompute one required output from progress fields and saved outputs."""
    step_id = step.id
    if isinstance(requirement, PlanRequirement):
        failures = [
            f"outputs:{step_id}:plan:{name}"
            for flag, name in (
                (requirement.require_completion_criteria and not progress.completion_criteria, "require_completion_criteria"),
                (requirement.require_stop_conditions and not progress.stop_conditions, "require_stop_conditions"),
            )
            if flag
        ]
        return ("block" if failures else "pass"), failures
    if isinstance(requirement, HypothesesRequirement):
        distinct = {" ".join(hypothesis.alternative.split()).casefold() for hypothesis in progress.hypotheses}
        if len(distinct) < requirement.min_count:
            return "block", [f"outputs:{step_id}:hypotheses", f"outputs:{step_id}:hypotheses:min_count"]
        return "pass", []
    if isinstance(requirement, CounterRefsRequirement):
        counters = [ref for ref in progress.counter_refs if ref.kind == "evidence"]
        if len(counters) < requirement.min_count:
            return "block", [f"outputs:{step_id}:counter_refs", f"outputs:{step_id}:counter_refs:min_count"]
        if requirement.verified_only and not await _replay_verified_counters(context, counters):
            return "block", [f"outputs:{step_id}:counter_refs:verified_only"]
        return "pass", []
    if isinstance(requirement, DimensionOutputRequirement):
        assessment = next((item for item in progress.dimensions if item.dimension == requirement.dimension), None)
        if assessment is None or STATUS_ORDER[assessment.status] < STATUS_ORDER[requirement.minimum_status]:
            failures = [f"outputs:{step_id}:dimension:{requirement.dimension}"]
            if STATUS_ORDER[requirement.minimum_status] > 0:
                failures.append(f"dimension_min:{requirement.dimension}")
            return "block", failures
        return "pass", []
    if isinstance(requirement, CalculationRequirement):
        if await _replay_calculation(context, progress, requirement):
            return "pass", []
        return "block", [f"outputs:{step_id}:calculation:{requirement.method}"]
    if isinstance(requirement, SnapshotFieldRequirement):
        for action in context.committed_actions:
            if action["tool"] == "capture_snapshot" and isinstance(action["parameters"].get("business"), dict) and action["parameters"]["business"].get("field") == requirement.field:
                return "pass", []
        return "block", [f"outputs:{step_id}:snapshot_field:{requirement.field}"]
    # Peer binding consistency compares a frozen peer binding with step-result
    # calculation refs; historical outputs have no step results to compare (§7.1).
    return "not_replayable", []


async def _replay_verified_counters(context: MethodValidationContext, refs: list[ObjectRef]) -> bool:
    """Same per-ref verified/no-unresolved-conflict bar as ``enforce_steps``."""
    from investment_agent.agent.observation import effective_cutoff
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.investor.foundations import InvestorError
    from investment_agent.investor.publication_numbers import NumericResolver

    session = context.session
    cutoff = await effective_cutoff(session, context.task_id)
    for ref in refs:
        row = await session.get(SavedEvidence, (str(ref.id), ref.revision))
        if row is None:
            return False
        try:
            subject = ObjectRef.model_validate(row.payload["subject_ref"])
            fact = await NumericResolver(EvidenceService(session), subject, cutoff).fact(ref, require_fresh=False)
        except (InvestorError, ValidationError):
            return False
        if fact.verification != "verified" or fact.conflict == "unresolved":
            return False
    return True


async def _replay_calculation(context: MethodValidationContext, progress: Any, requirement: CalculationRequirement) -> bool:
    """A matching-method saved calculation inside the progress ref sets, resolvable.

    The action-backed attribution stays ``not_replayable``; this recomputes only
    the citable part: an owned, non-peer calculation of the exact method that
    still resolves against its bound facts (an unrelated calculation cannot
    stand in, mirroring ``_validate_calculation``).
    """
    from investment_agent.agent.observation import effective_cutoff
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.industry_peers.resolver import peer_method_payload
    from investment_agent.investor.foundations import InvestorError
    from investment_agent.investor.publication_numbers import NumericResolver

    session = context.session
    cutoff = await effective_cutoff(session, context.task_id)
    refs = [*progress.fact_refs, *progress.counter_refs, *(ref for assessment in progress.dimensions for ref in assessment.refs)]
    for ref in refs:
        if ref.kind != "calculation":
            continue
        row = await session.get(SavedCalculation, (str(ref.id), ref.revision))
        if row is None or row.owner_id != context.grant.owner:
            continue
        if await peer_method_payload(session, context.grant.owner, ref) is not None:
            continue  # peer-method calculations belong to the peer requirement
        if row.payload.get("method") != requirement.method:
            continue
        try:
            fact_rows = [await session.get(SavedEvidence, (str(f["id"]), f["revision"])) for f in row.payload.get("fact_refs", [])]
            subjects = {ObjectRef.model_validate(f.payload["subject_ref"]) for f in fact_rows if f is not None}
            if len(subjects) != 1:
                continue
            await NumericResolver(EvidenceService(session), next(iter(subjects)), cutoff).calculation(ref, require_fresh=False)
            return True
        except (InvestorError, ValidationError):
            continue
    return False


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _progress_ref_set(progress: Any) -> set[ObjectRef]:
    """Every reference collection the generic validator type-checks."""
    from investment_agent.investor.contracts import EntityNode, NumericNode

    nodes = {node.ref for node in progress.content if isinstance(node, EntityNode | NumericNode)}
    dimensions = {ref for assessment in progress.dimensions for ref in assessment.refs}
    return {*progress.fact_refs, *progress.counter_refs, *dimensions, *nodes}


def _result_contains_ref(value: Any, ref: ObjectRef) -> bool:
    """Recursive id/revision/kind match of an ObjectRef inside a tool result."""
    if isinstance(value, dict):
        if value.get("id") == str(ref.id) and value.get("revision") == ref.revision and value.get("kind") == ref.kind:
            return True
        return any(_result_contains_ref(child, ref) for child in value.values())
    if isinstance(value, list):
        return any(_result_contains_ref(child, ref) for child in value)
    return False

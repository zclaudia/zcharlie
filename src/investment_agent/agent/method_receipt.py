"""v2 task method receipts (design §4.3) and the fixed method renderer (§6.1).

A v2 task never re-renders its method and never re-reads a live package or
head: ``freeze_task`` renders the package once, hashes the text and freezes
tool-family members into a :class:`TaskMethodReceipt` at admission. The
renderer is fixed server-owned text per ``instruction_kind`` and output
requirement type; package ``description`` and step ``title`` stay review-only
and are never concatenated into model instructions (§6.1 item 4).

``validator_version`` selects the settlement implementation. New v2 tasks
freeze ``validator_version = VALIDATOR_V1`` (enforcing, card 07); receipts
frozen by card 04 keep ``render-only`` and never upgrade in flight - the two
versions never mix (§4.3, §10).
"""

import hashlib
from typing import Any, Literal, cast

from pydantic import SerializerFunctionWrapHandler, model_serializer

from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.method_packages import (
    CalculationRequirement,
    CounterRefsRequirement,
    DimensionOutputRequirement,
    HypothesesRequirement,
    MethodPackageDefinition,
    OutputRequirement,
    PlanRequirement,
    SnapshotFieldRequirement,
    ToolFamily,
    resolve_family_members,
)
from investment_agent.investor.methods import MethodBinding

RENDERER_VERSION: Literal["1", "2"] = "2"
VALIDATOR_RENDER_ONLY = "render-only"
VALIDATOR_V1 = "1"


class TaskMethodReceipt(Contract):
    """Immutable admission snapshot of one v2 task's method (design §4.3)."""

    schema_version: Literal[1] = 1
    binding: MethodBinding
    rendered_method: str
    rendered_method_hash: str
    renderer_version: str
    validator_version: str
    tool_family_members: dict[ToolFamily, tuple[str, ...]]
    # §7.3 candidate-package prospective shadow only: the task executes the
    # proposal-frozen candidate instead of an approved method version, and the
    # receipt records the shadow marker plus the proposal's old-method ref
    # (checked equal at admission/result time). Both default to None and every
    # normal v1/v2 task keeps the exact pre-card-15 payload shape.
    shadow: Literal["candidate_package"] | None = None
    candidate_base_method_ref: ObjectRef | None = None

    @model_serializer(mode="wrap")
    def preserve_receipt_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Omit the two shadow fields while unset, keeping every earlier
        serialized receipt byte-identical (§7.3: normal v1/v2 tasks unchanged)."""
        data = cast(dict[str, Any], handler(self))
        if self.shadow is None:
            data.pop("shadow", None)
        if self.candidate_base_method_ref is None:
            data.pop("candidate_base_method_ref", None)
        return data


INSTRUCTION_KIND_TEXT: dict[str, str] = {
    "falsifiable_plan": (
        "Produce a falsifiable research plan: state explicit hypotheses, how each can"
        " be falsified, and the completion criteria and stop conditions required below."
    ),
    "counter_analysis": (
        "Seek and record counter-evidence that could overturn the working conclusion,"
        " reporting counter references exactly as required below."
    ),
    "dimension_assessment": (
        "Assess every listed research dimension explicitly, backed by saved checkable"
        " references at the status required below."
    ),
    "snapshot_capture": (
        "Capture the exact snapshot fields listed below and reference the committed"
        " capture actions that produced them."
    ),
    "calculation": (
        "Run the exact calculation methods listed below and reference the committed"
        " calculation outputs."
    ),
    "peer_context": (
        "Read the industry peer context and bind any peer calculation to the task's"
        " frozen peer cohort."
    ),
    "expectations_gap": (
        "Before any directional price judgment, compare what the current price"
        " implies (reverse-DCF implied growth or historical valuation percentile)"
        " with your own evidenced expectations for the same drivers; state the gap"
        " and which evidence would narrow it. If no gap can be articulated, keep"
        " the price view balanced or undetermined and record the gap instead."
    ),
}


def _requirement_text(requirement: OutputRequirement) -> str:
    """Deterministic one-line description of one typed output requirement."""
    if isinstance(requirement, PlanRequirement):
        parts = [
            text
            for flag, text in (
                (requirement.require_completion_criteria, "completion criteria required"),
                (requirement.require_stop_conditions, "stop conditions required"),
            )
            if flag
        ]
        return "plan output" + (f" ({'; '.join(parts)})" if parts else "")
    if isinstance(requirement, HypothesesRequirement):
        return f"hypotheses output: at least {requirement.min_count} distinct hypothesis entries"
    if isinstance(requirement, CounterRefsRequirement):
        text = (
            f"counter_refs output: at least {requirement.min_count} references included"
            " in progress.counter_refs"
        )
        if requirement.verified_only:
            text += ", each verified without unresolved conflicts"
        return text
    if isinstance(requirement, DimensionOutputRequirement):
        return (
            f"dimension output: {requirement.dimension} must reach at least"
            f" {requirement.minimum_status} status"
        )
    if isinstance(requirement, CalculationRequirement):
        return f"calculation output: a {requirement.method} calculation referenced by committed actions"
    if isinstance(requirement, SnapshotFieldRequirement):
        return f"snapshot_field output: a committed capture_snapshot action for field {requirement.field}"
    text = "peer_context output: a get_industry_peer_context action reference"
    if requirement.require_calculation:
        text += " with a calculation bound to the same frozen peer context"
    return text


def render_method(package: MethodPackageDefinition) -> str:
    """Fixed-template text of one package; the same input always renders the same text.

    Steps are output in package order. Every step carries the step id, the fixed
    instruction-kind text, dependencies ("must complete before this step"),
    blocking, the deterministic required-output descriptions and the gap policy.
    ``description`` and ``title`` are review-only and never appear here.
    """
    lines = [
        f"Method package {package.package_key} (renderer {RENDERER_VERSION}):",
        "Blocking steps must be planned first and satisfied or completed-with-gap"
        " before the final answer.",
    ]
    for step in package.steps:
        lines.append(f"Step {step.id} ({step.instruction_kind}): {INSTRUCTION_KIND_TEXT[step.instruction_kind]}")
        lines.append(f"- This step is {'blocking' if step.blocking else 'non-blocking'}.")
        if step.depends_on:
            lines.append(
                f"- Depends on {', '.join(step.depends_on)}: each listed step must"
                " complete before this step."
            )
        else:
            lines.append("- Depends on: none.")
        if step.required_outputs:
            lines.append("- Required outputs:")
            lines.extend(f"  - {_requirement_text(requirement)}" for requirement in step.required_outputs)
        else:
            lines.append("- Required outputs: none beyond the step instruction.")
        if step.allow_missing:
            lines.append(
                "- allow_missing: true - this step may close as completed_with_gap with"
                " an explicit gap reason."
            )
        else:
            lines.append("- allow_missing: false - this step cannot close as completed_with_gap.")
    return "\n".join(lines)


def build_receipt(
    binding: MethodBinding,
    package_definition: MethodPackageDefinition,
    *,
    validator_version: str,
) -> TaskMethodReceipt:
    """Render once, hash the text and freeze the tool-family snapshot (§4.3)."""
    rendered = render_method(package_definition)
    members: dict[ToolFamily, tuple[str, ...]] = {}
    for step in package_definition.steps:
        for family in step.required_capabilities:
            if family not in members:
                members[family] = resolve_family_members((family,))
    return TaskMethodReceipt(
        binding=binding,
        rendered_method=rendered,
        rendered_method_hash=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        renderer_version=RENDERER_VERSION,
        validator_version=validator_version,
        tool_family_members=members,
    )


def parse_task_method_payload(payload: dict[str, Any]) -> tuple[MethodBinding, TaskMethodReceipt | None]:
    """Read one frozen ``TaskMethodBinding`` payload.

    A payload carrying a ``"receipt"`` key is the v2 shape and yields the
    receipt; every earlier payload keeps parsing as a plain binding, so
    historical rows validate exactly as before.
    """
    if "receipt" in payload:
        receipt = TaskMethodReceipt.model_validate(payload["receipt"])
        return receipt.binding, receipt
    return MethodBinding.model_validate(payload), None

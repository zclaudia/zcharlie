"""Offline and prospective shadow replay for research-method changes.

MethodService stays in methods.py. That module re-exports these names.
This module must not import methods.py at import time: run_offline_shadow
loads MethodService only when it runs.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.tasks import fingerprint
from investment_agent.investor.common import Contract, Text
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.investor import MethodArtifact

if TYPE_CHECKING:
    from investment_agent.investor.method_packages import MethodPackage


class ShadowRequest(Contract):
    mutation_id: UUID
    proposal_id: UUID
    expected_revision: int = Field(ge=1)
    evaluation_id: UUID


CANDIDATE_OUTPUT_CONTRACT = (
    "Candidate output contract (fixed server template): the replayed progress keeps every"
    " frozen field unchanged and may add only plan_bindings and step_results. plan_bindings:"
    " list of {evidence_step_index: 0-based index into evidence_steps, method_step_id: an id"
    " from the candidate rendered method}; cover every blocking step. step_results: list of"
    " {method_step_id, evidence_step_indices: indices bound for that step in plan_bindings,"
    " status: satisfied | completed_with_gap, action_ids: only ids listed in frozen_actions,"
    " output_refs: only {kind, id, revision} refs that appear in frozen_progress,"
    " hypothesis_indices: indices into the frozen hypotheses, gap_reason: required for"
    " completed_with_gap}; every blocking step carries exactly one final result. Any other"
    " change to the frozen progress is rejected."
)
"""The deterministic §4.4 schema description sent with every candidate shadow prompt (§7.2)."""


def _ref_identity(ref: Any) -> tuple[str, str, int, str | None]:
    """Canonical identity of one ObjectRef model or JSON mapping, for frozen-ref matching."""
    if callable(getattr(ref, "get", None)):
        kind, identifier, revision, field_path = ref.get("kind"), ref.get("id"), ref.get("revision", 0), ref.get("field_path")
    else:
        kind, identifier, revision, field_path = ref.kind, ref.id, ref.revision, ref.field_path
    try:
        identifier = str(UUID(str(identifier)))
    except (ValueError, TypeError):
        identifier = str(identifier)
    return (str(kind), identifier, int(revision), field_path)


def _frozen_ref_identities(value: Any) -> set[tuple[str, str, int, str | None]]:
    """Every ObjectRef-shaped identity appearing anywhere in the frozen progress JSON."""
    if isinstance(value, dict):
        found = {_ref_identity(value)} if {"kind", "id", "revision"} <= set(value) else set()
        return found | {identity for child in value.values() for identity in _frozen_ref_identities(child)}
    if isinstance(value, (list, tuple)):
        return {identity for child in value for identity in _frozen_ref_identities(child)}
    return set()


def _candidate_shadow_replay(inner: Any, frozen: dict[str, Any]) -> Any:
    """Enforcing candidate replay boundary (§7.2).

    Whatever the inner model function attempts, the returned progress JSON is
    rebuilt as the frozen sample content plus only the two new metadata fields
    (plan_bindings/step_results) - the model can never alter a frozen field.
    Every action id / output ref the metadata cites must exist in the frozen
    sample; a hallucinated identity raises InvestorError right here, so
    deterministic code intercepts instead of trusting model self-discipline.
    """
    import json

    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from investment_agent.agent.research import MethodPlanBinding, MethodStepResult

    def sanitize(response: Any, info: Any) -> ModelResponse:
        part = next(
            (
                candidate_part
                for candidate_part in getattr(response, "parts", ())
                if getattr(candidate_part, "part_kind", None) == "tool-call"
                and getattr(candidate_part, "tool_name", None) == info.output_tools[0].name
            ),
            None,
        )
        args = getattr(part, "args", None)
        try:
            attempted = json.loads(args) if isinstance(args, str) else args
        except ValueError as exc:
            raise InvestorError("invalid_reference", "候选影子输出不是合法 JSON，重放拒绝") from exc
        if not isinstance(attempted, dict):
            raise InvestorError("invalid_reference", "候选影子重放缺少输出工具调用参数，重放拒绝")
        try:
            bindings = [MethodPlanBinding.model_validate(item) for item in attempted.get("plan_bindings") or ()]
            results = [MethodStepResult.model_validate(item) for item in attempted.get("step_results") or ()]
        except (ValidationError, TypeError, ValueError) as exc:
            raise InvestorError("invalid_reference", "候选影子新元数据不符合输出 schema，重放拒绝") from exc
        for result in results:
            if unknown := [str(action_id) for action_id in result.action_ids if str(action_id) not in frozen["action_ids"]]:
                raise InvestorError("invalid_reference", f"候选影子引用了冻结样本中不存在的动作 id：{unknown[0]}")
            if foreign := [ref for ref in result.output_refs if _ref_identity(ref) not in frozen["refs"]]:
                raise InvestorError("invalid_reference", f"候选影子引用了冻结 progress 中不存在的 output ref：{foreign[0].kind}:{foreign[0].id}")
        payload = {
            **frozen["progress"],
            "plan_bindings": [binding.model_dump(mode="json") for binding in bindings],
            "step_results": [result.model_dump(mode="json") for result in results],
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    if inspect.iscoroutinefunction(inner):
        async def replay(messages: Any, info: Any) -> Any:
            return sanitize(await inner(messages, info), info)
    else:
        def replay(messages: Any, info: Any) -> Any:  # type: ignore[misc]
            return sanitize(inner(messages, info), info)
    return replay


async def _candidate_shadow_verdict(
    session: AsyncSession, review: dict[str, Any], candidate: MethodPackage, output: dict[str, Any]
) -> tuple[str, list[str]]:
    """Package-level ``validate_final`` over one replayed candidate progress (§7.2).

    The offline run's only committed plan is the replayed candidate progress
    itself, so its plan part serves as ``first_plan``: the historical first
    committed save predates the candidate schema and carries no plan bindings,
    so binding it would render every sample ``method_plan_changed`` and make
    the whole shadow vacuous. Dependency order over historical actions without
    ``committed_sequence`` cannot be satisfied by any replayed metadata - that
    single failure kind is the offline not-replayable item (§7.1) and does not
    count against an in-scope counterexample.
    """
    from dataclasses import replace

    from investment_agent.agent.execution import ResearchValidationError
    from investment_agent.agent.method_receipt import RENDERER_VERSION, VALIDATOR_V1
    from investment_agent.agent.method_validation import replay_context, validate_final
    from investment_agent.agent.research import MethodPlannedProgress

    progress = MethodPlannedProgress.model_validate(output)
    context = await replay_context(session, review, candidate, renderer_version=RENDERER_VERSION, validator_version=VALIDATOR_V1)
    try:
        await validate_final(session, replace(context, first_plan=progress), progress)
    except ResearchValidationError as exc:
        details = [f"{error.field}: {error.message}" for error in exc.field_errors]
        not_replayable = exc.code == "method_step_order_violation" and any("缺少已提交动作顺序" in error.message for error in exc.field_errors)
        return ("not_replayable_only" if not_replayable else "block"), [f"{exc.code}: {item}" for item in details]
    except (InvestorError, ValidationError, ValueError, LookupError) as exc:
        return "error", [type(exc).__name__ + ":" + str(exc)[:200]]
    return "pass", []


async def run_offline_shadow(factory: Any, request: ShadowRequest, model: Any) -> dict[str, Any]:
    """One finite native FunctionModel run per frozen sample, no tools or effects.

    This is an offline model-boundary replay, never a paid/production trial. No
    transaction spans a model call. Reject/change/rollback fences stale results.
    Stable mutation replay never repeats an uncertain model call. Package
    proposals replay the candidate package (§7.2): the prompt carries the
    rendered candidate method, the fixed output contract and the frozen action
    summary; the enforcing replay boundary rebuilds frozen content plus only
    the new plan_bindings/step_results metadata and intercepts hallucinated
    identities; every applicable sample is judged by the package-level
    ``validate_final``. Config-only proposals keep the exact existing path.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import UsageLimits

    from investment_agent.agent.research import ResearchProgress
    from investment_agent.investor.methods import MethodBinding, MethodConfig, MethodService
    if not isinstance(model, FunctionModel):
        raise ValueError("offline shadows only accept FunctionModel; production calls require existing executor budgets")
    async with factory() as session:
        service = MethodService(session)
        if result := await service.replay(request.mutation_id, request):
            if result.get("claim_id"):
                results = (await session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == service.owner, MethodArtifact.kind == "shadow_result"))).all()
                saved = next((r.payload["shadow"] for r in results if r.payload["claim_id"] == result["claim_id"]), None)
                if saved:
                    return cast(dict[str, Any], saved)
            return result
        row = await service.proposal(request.proposal_id)
        evaluation = await service.artifact(request.evaluation_id, "evaluation")
        if row.revision != request.expected_revision or row.payload["status"] != "shadow" or not evaluation["passed"] or row.payload["evaluation_ids"][-1] != evaluation["id"] or evaluation["proposal_id"] != row.id:
            raise InvestorError("stale_revision", "影子运行需要当前完整通过的评估")
        config = MethodConfig.model_validate(row.payload["request"]["config"])
        candidate = await service._frozen_candidate(row)
        frozen: list[dict[str, Any]] = []
        if candidate is not None:
            # §7.2: the same committed-action ledger feeds the prompt summary,
            # the replay-boundary interception and the judge-side context.
            from investment_agent.agent.method_validation import _committed_actions
            for sample in evaluation["cases"]:
                actions = await _committed_actions(session, UUID(sample["frozen_review"]["task_id"]))
                frozen.append({
                    "progress": sample["frozen_review"]["progress"],
                    "actions": [{"id": str(action["operation_id"]), "tool": action["tool"], "committed_sequence": action["committed_sequence"]} for action in actions],
                    "action_ids": {str(action["operation_id"]) for action in actions},
                    "refs": _frozen_ref_identities(sample["frozen_review"]["progress"]),
                })
        claim = service.save("shadow_claim", {"proposal_id": row.id, "evaluation_id": evaluation["id"], "evaluation_hash": fingerprint(evaluation), "request": request.model_dump(mode="json")})
        await service.revise(row, row.revision, {"shadow_claim_id": claim["id"]})
        await service.receipt(request.mutation_id, request, {"status": "running", "claim_id": claim["id"], "retry_policy": "same mutation returns receipt; explicit new trial required after interrupted offline run"})
        await session.commit()
    outputs = []
    for index, sample in enumerate(evaluation["cases"]):
        try:
            import json
            if candidate is None:
                agent = Agent(model, output_type=ResearchProgress, retries=0, instructions="Offline method replay. Return the frozen research progress; no tools, publication or external effects.")
                native_result = await agent.run(json.dumps({"frozen_progress": sample["frozen_review"]["progress"], "method_config": config.model_dump(mode="json")}), usage_limits=UsageLimits(request_limit=1))
            else:
                from investment_agent.agent.method_receipt import render_method
                from investment_agent.agent.research import MethodPlannedProgress
                agent = Agent(FunctionModel(_candidate_shadow_replay(model.function, frozen[index])), output_type=MethodPlannedProgress, retries=0, instructions="Offline candidate-package replay. Return the frozen research progress adding only plan_bindings and step_results per the candidate output contract; cite only ids and refs that exist in the frozen sample; no tools, publication or external effects.")
                native_result = await agent.run(json.dumps({"frozen_progress": sample["frozen_review"]["progress"], "method_config": config.model_dump(mode="json"), "candidate_rendered_method": render_method(candidate), "candidate_output_contract": CANDIDATE_OUTPUT_CONTRACT, "frozen_actions": frozen[index]["actions"]}), usage_limits=UsageLimits(request_limit=1))
            outputs.append({"review_id": sample["review_id"], "output": native_result.output.model_dump(mode="json"), "usage": {"requests": native_result.usage.requests, "input_tokens": native_result.usage.input_tokens, "output_tokens": native_result.usage.output_tokens}})
        except Exception as exc:
            record: dict[str, Any] = {"review_id": sample["review_id"], "error": type(exc).__name__}
            if candidate is not None and isinstance(exc, InvestorError):
                record["detail"] = f"{exc.detail.code}: {exc.detail.message}"
            outputs.append(record)
    async with factory() as session:
        service = MethodService(session)
        await service.reserve()
        row = await service.proposal(request.proposal_id)
        if row.revision != request.expected_revision + 1 or row.payload["status"] != "shadow" or row.payload.get("shadow_claim_id") != claim["id"]:
            value = service.save("shadow", {"proposal_id": row.id, "evaluation_id": evaluation["id"], "outputs": outputs, "passed": False, "fenced": True, "quality": "offline_FunctionModel_engineering_only"})
        else:
            cases = []
            for sample, output in zip(evaluation["cases"], outputs, strict=True):
                if candidate is None:
                    sample_config = config if sample["applicable"] else MethodBinding.model_validate(sample["frozen_review"]["old_method"]).config
                    checked = await service.check_sample(sample["frozen_review"], sample_config, output["output"]) if "output" in output else {"result": "model_error", "errors": [output["error"]]}
                    cases.append({"review_id": sample["review_id"], "role": sample["role"], "expected": sample["expected"], "checked": checked, "output": output})
                else:
                    review = sample["frozen_review"]
                    conclusion: str
                    errors: list[str]
                    if "output" not in output:
                        conclusion, errors = "model_error", [str(output.get("detail", output["error"]))]
                    elif not sample["applicable"]:
                        # Out-of-scope samples keep their own method and the
                        # existing baseline semantics (§7.1): the candidate
                        # never touches them, so only the frozen content is
                        # checked, with the replay-added metadata stripped.
                        checked = await service.check_sample(review, MethodBinding.model_validate(review["old_method"]).config, {key: value for key, value in output["output"].items() if key not in {"plan_bindings", "step_results"}})
                        conclusion, errors = str(checked["result"]), [str(error) for error in checked.get("errors", ())]
                    else:
                        conclusion, errors = await _candidate_shadow_verdict(session, review, candidate, output["output"])
                    cases.append({
                        "review_id": sample["review_id"],
                        "role": sample["role"],
                        "expected": sample["expected"],
                        "applicable": sample["applicable"],
                        "candidate_conclusion": conclusion,
                        "errors": errors,
                        "matched": conclusion in ({"block"} if sample["expected"] == "block" else {"pass", "not_replayable_only"}),
                        "output": output,
                    })
            if candidate is None:
                value = service.save("shadow", {"proposal_id": row.id, "evaluation_id": evaluation["id"], "evaluation_hash": fingerprint(evaluation), "cases": cases, "passed": all(c["checked"]["result"] == c["expected"] for c in cases), "quality": "offline_FunctionModel_engineering_only"})
            else:
                value = service.save("shadow", {
                    "proposal_id": row.id,
                    "evaluation_id": evaluation["id"],
                    "evaluation_hash": fingerprint(evaluation),
                    "candidate_shadow": True,
                    "rendered_method_hash": row.payload["request"]["rendered_method_hash"],
                    "cases": cases,
                    "passed": all(case["matched"] for case in cases),
                    "quality": "offline_FunctionModel_engineering_only",
                })
            await service.revise(row, row.revision, {"shadow_ids": [*row.payload["shadow_ids"], value["id"]]})
        # Immutable original mutation receipt stays a claim. Results are found
        # through that exact claim, allowing lost acknowledgements to recover.
        service.save("shadow_result", {"claim_id": claim["id"], "proposal_id": row.id, "shadow": value})
        await session.commit()
        return value


class ProspectiveRequest(Contract):
    mutation_id: UUID
    proposal_id: UUID
    expected_revision: int = Field(ge=1)
    evaluation_id: UUID
    task_ids: tuple[UUID, ...] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def unique_tasks(self) -> ProspectiveRequest:
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("distinct actual fresh tasks required")
        return self


CANDIDATE_SHADOW_FORBIDDEN_TOOLS = frozenset({
    "prepare_company_view",  # no formal view preparation / publication
    "get_portfolio_context", "calculate_portfolio_options", "prepare_portfolio_plan",  # no portfolio ledger family
})
"""§7.3 shadow boundary: stripped from the shadow grant's tool whitelist, so
the model can never call them (no publication, no notification/ledger path)."""


class CandidateShadowRequest(Contract):
    """Body of POST /investor/methods/proposals/{proposal_id}/candidate-shadow;
    the proposal id arrives on the path and is bound into this contract."""

    mutation_id: UUID
    proposal_id: UUID
    prompt: Text = Field(max_length=2000)
    expected: Literal["pass", "block"]


class CandidateShadowResultRequest(Contract):
    mutation_id: UUID
    proposal_id: UUID
    task_id: UUID
    expected_revision: int = Field(ge=1)


class CandidateShadowItem(Contract):
    """One batch shadow item: its own prompt and expectation (card 20 §1)."""

    prompt: Text = Field(max_length=2000)
    expected: Literal["pass", "block"]


class CandidateShadowBatchRequest(Contract):
    """Body of POST /investor/methods/proposals/{proposal_id}/candidate-shadow-batch;
    the proposal id arrives on the path and is bound into this contract. The
    whole batch shares one mutation id (one idempotent replay) and its prompts
    must be distinct."""

    mutation_id: UUID
    proposal_id: UUID
    items: tuple[CandidateShadowItem, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def distinct_prompts(self) -> CandidateShadowBatchRequest:
        if len({item.prompt for item in self.items}) != len(self.items):
            raise ValueError("distinct prompts required in one batch")
        return self

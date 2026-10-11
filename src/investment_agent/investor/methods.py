"""Finite research-step changes. This module cannot change permissions or publish.

Artifacts freeze actual saved inputs/outputs. Offline model runs are explicitly
FunctionModel replays, engineering evidence only, never semantic quality proof.
"""
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import Field, StrictBool, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.tasks import fingerprint
from investment_agent.investor.common import Contract, ObjectRef, Text
from investment_agent.investor.foundations import InvestorError, storage_conflicts
from investment_agent.investor.publication import FIXED_METHOD
from investment_agent.storage.models.agent import AgentTask, ToolAction
from investment_agent.storage.models.investor import (
    InvestorMutation,
    MethodArtifact,
    MethodHead,
    MethodProposal,
    TaskMethodBinding,
)


def method_transaction[T](fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    @wraps(fn)
    @storage_conflicts
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> T:
        await self.reserve()
        async with self.session.begin_nested():
            return await fn(self, *args, **kwargs)
    return wrapped


class MethodConfig(Contract):
    schema_version: Literal[1] = 1
    require_two_counter_hypotheses: StrictBool = False
    require_verified_counter_evidence: StrictBool = False
    require_explicit_stop_decision: StrictBool = False

    def stronger_than(self, old: "MethodConfig") -> bool:
        keys = tuple(k for k in type(self).model_fields if k != "schema_version")
        return all(not getattr(old, k) or getattr(self, k) for k in keys) and any(getattr(self, k) != getattr(old, k) for k in keys)


class MethodBinding(Contract):
    schema_version: Literal[1] = 1
    method_ref: ObjectRef = FIXED_METHOD
    config: MethodConfig = MethodConfig()
    listing_ref: ObjectRef | None = None


class CreateProposal(Contract):
    mutation_id: UUID
    review_id: UUID
    listing_ref: ObjectRef
    old_method: ObjectRef
    error_pattern: Text = Field(max_length=1000)
    summary: Text = Field(max_length=1000)
    config: MethodConfig


class EvaluationCase(Contract):
    review_id: UUID
    role: Literal["failure", "counterexample"]
    expected: Literal["block", "pass"]


class EvaluateProposal(Contract):
    mutation_id: UUID
    proposal_id: UUID
    expected_revision: int = Field(ge=1)
    cases: tuple[EvaluationCase, ...] = Field(min_length=2, max_length=12)

    @model_validator(mode="after")
    def distinct_cases(self) -> "EvaluateProposal":
        if len({c.review_id for c in self.cases}) != len(self.cases):
            raise ValueError("distinct completed reviews required")
        return self


class MethodDecision(Contract):
    mutation_id: UUID
    proposal_id: UUID
    expected_revision: int = Field(ge=1)
    expected_head_revision: int = Field(ge=0)
    listing_ref: ObjectRef
    evidence_hash: Text
    summary: Text
    decision: Literal["approve", "reject"]


class MethodRollback(Contract):
    mutation_id: UUID
    listing_ref: ObjectRef
    expected_head_revision: int = Field(ge=1)
    target_method: ObjectRef
    reason: Text = Field(max_length=1000)


async def task_method(session: AsyncSession, task_id: UUID) -> MethodBinding:
    receipt = await session.get(TaskMethodBinding, str(task_id))
    return MethodBinding.model_validate(receipt.payload) if receipt else MethodBinding()


async def enforce_steps(session: AsyncSession, config: MethodConfig, progress: Any, cutoff: Any, *, final: bool = False, formal_directional: bool = False) -> None:
    from investment_agent.agent.execution import ResearchValidationError
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.investor.publication_numbers import NumericResolver
    from investment_agent.storage.models.evidence import SavedEvidence
    if progress.kind not in {"company_research", "review"}:
        return
    if config.require_two_counter_hypotheses and len({" ".join(h.alternative.split()).casefold() for h in progress.hypotheses}) < 2:
        raise ResearchValidationError("method_counter_hypotheses_required", "progress.hypotheses", "本任务方法要求两个不同的反向解释。")
    if final and config.require_explicit_stop_decision and not progress.answered and not progress.stopped_because:
        raise ResearchValidationError("method_stop_decision_required", "progress.stopped_because", "本任务方法要求明确的停止或继续判断。")
    if final and (progress.answered or formal_directional) and config.require_verified_counter_evidence:
        if not progress.counter_refs:
            raise ResearchValidationError("method_verified_counter_required", "progress.counter_refs", "本任务方法要求已验证的反证来源。")
        for ref in progress.counter_refs:
            row = await session.get(SavedEvidence, (str(ref.id), ref.revision)) if ref.kind == "evidence" else None
            if row is None:
                raise ResearchValidationError("method_verified_counter_required", "progress.counter_refs", "反证必须为已验证、无未解冲突的原始证据。")
            subject = ObjectRef.model_validate(row.payload["subject_ref"])
            fact = await NumericResolver(EvidenceService(session), subject, cutoff).fact(ref, require_fresh=False)
            if fact.verification != "verified" or fact.conflict == "unresolved":
                raise ResearchValidationError("method_verified_counter_required", "progress.counter_refs", "反证必须为已验证、无未解冲突的原始证据。")


class MethodService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner") -> None:
        self.session, self.owner = session, owner

    async def reserve(self) -> None:
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            active = await connection.run_sync(lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False)))
            if not active:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")

    async def replay(self, key: UUID, request: Contract) -> dict[str, Any] | None:
        await self.reserve()
        row = await self.session.get(InvestorMutation, (self.owner, str(key)))
        if row and row.request_hash != fingerprint({"method": request.model_dump(mode="json")}):
            raise InvestorError("idempotency_conflict", "该操作编号已用于其他内容")
        return row.result if row else None

    async def receipt(self, key: UUID, request: Contract, result: dict[str, Any]) -> dict[str, Any]:
        self.session.add(InvestorMutation(owner_id=self.owner, key=str(key), request_hash=fingerprint({"method": request.model_dump(mode="json")}), result=result))
        await self.session.flush()
        return result

    def save(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "proposal_version":
            payload = {**payload, "proposal_id": payload["id"]}
        value = {**payload, "id": str(uuid4()), "schema_version": 1, "created_at": datetime.now(UTC).isoformat()}
        self.session.add(MethodArtifact(id=value["id"], owner_id=self.owner, kind=kind, payload=value))
        return value

    async def artifact(self, identifier: UUID, kind: str) -> dict[str, Any]:
        row = await self.session.get(MethodArtifact, str(identifier))
        if not row or row.owner_id != self.owner or row.kind != kind:
            raise InvestorError("not_found", "方法证据不可用")
        return row.payload

    async def proposal(self, identifier: UUID) -> MethodProposal:
        row = await self.session.get(MethodProposal, str(identifier), populate_existing=True)
        if not row or row.owner_id != self.owner:
            raise InvestorError("not_found", "方法提案不可用")
        return row

    async def scope(self, ref: ObjectRef) -> None:
        from investment_agent.storage.models.reference import Listing
        if ref.kind != "listing" or ref.revision != 1 or ref.field_path is not None or await self.session.get(Listing, ref.id) is None:
            raise InvestorError("invalid_reference", "适用范围必须是一个明确证券")

    async def known(self, ref: ObjectRef) -> MethodBinding:
        if ref == FIXED_METHOD:
            return MethodBinding()
        if ref.kind != "method" or ref.revision != 1 or ref.field_path:
            raise InvestorError("invalid_reference", "未知方法版本")
        value = await self.artifact(ref.id, "version")  # type: ignore[arg-type]
        return MethodBinding.model_validate(value["binding"])

    async def current(self, listing: ObjectRef) -> tuple[int, MethodBinding]:
        await self.scope(listing)
        row = await self.session.get(MethodHead, (self.owner, listing.id), populate_existing=True)
        return (row.revision, MethodBinding.model_validate(row.payload)) if row else (0, MethodBinding(listing_ref=listing))

    async def freeze_task(self, task_id: UUID, refs: tuple[ObjectRef, ...], research_kind: str) -> None:
        # Historical Tasks without this receipt always remain the exact baseline.
        # Multi-listing/comparison/portfolio contexts have a documented baseline,
        # never a silently mixed policy. Only one-listing full research can opt in.
        listings = tuple({r for r in refs if r.kind == "listing"})
        binding = MethodBinding()
        if len(listings) == 1 and research_kind in {"company_research", "review"}:
            head = await self.session.get(MethodHead, (self.owner, listings[0].id))
            binding = MethodBinding.model_validate(head.payload) if head else MethodBinding(listing_ref=listings[0])
        self.session.add(TaskMethodBinding(task_id=str(task_id), payload=binding.model_dump(mode="json")))

    @method_transaction
    async def process_review(self, task_id: UUID) -> dict[str, Any]:
        await self.reserve()
        task = await self.session.get(AgentTask, str(task_id))
        if not task or task.owner_id != self.owner or task.status not in {"succeeded", "partial", "failed"}:
            raise InvestorError("invalid_reference", "过程复盘需要真实已完成任务")
        rows = (await self.session.scalars(select(ToolAction).where(ToolAction.task_id == str(task_id), ToolAction.state == "committed"))).all()
        actions = [{"operation_id": r.operation_id, "parameters": r.parameters, "result": r.result} for r in rows]
        progress = [r.result["progress"] for r in rows if r.result and r.parameters.get("tool") == "save_research_progress"]
        if not progress:
            raise InvestorError("invalid_reference", "任务没有可冻结的真实研究输出")
        from investment_agent.storage.models.publication import PublicationArtifact
        artifacts = []
        for ref in task.payload.get("artifact_refs", []):
            row = await self.session.get(PublicationArtifact, (ref["id"], ref["revision"]))
            if row and row.owner_id == self.owner:
                artifacts.append({"kind": row.kind, "payload": row.payload})
        from investment_agent.storage.models.agent import ExecutionGrantRow
        from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
        grant = await self.session.get(ExecutionGrantRow, task.id)
        materials = []
        for saved_progress in progress:
            refs = [*saved_progress.get("fact_refs", []), *saved_progress.get("counter_refs", []), *(r for d in saved_progress.get("dimensions", []) for r in d.get("refs", []))]
            for ref in refs:
                model = SavedEvidence if ref["kind"] == "evidence" else SavedCalculation if ref["kind"] == "calculation" else None
                material: Any = await self.session.get(model, (ref["id"], ref["revision"])) if model else None
                if material and material.owner_id == self.owner:
                    materials.append(material.payload)
        inputs = {"task": task.payload, "admission": grant.payload if grant else None, "actions": actions, "publications": artifacts, "materials": materials}
        existing = (await self.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == self.owner, MethodArtifact.kind == "process_review"))).all()
        for saved in existing:
            if saved.payload["task_id"] == str(task_id) and saved.payload["input_hash"] == fingerprint(inputs):
                return saved.payload
        result = self.save("process_review", {"task_id": str(task_id), "status": task.status, "old_method": (await task_method(self.session, task_id)).model_dump(mode="json"), "inputs": inputs, "input_hash": fingerprint(inputs), "progress": progress[-1], "quality": "saved_process_only_no_profit_or_semantic_rating"})
        await self.session.flush()
        return result

    @method_transaction
    async def create(self, request: CreateProposal) -> dict[str, Any]:
        if result := await self.replay(request.mutation_id, request):
            return result
        await self.scope(request.listing_ref)
        review = await self.artifact(request.review_id, "process_review")
        old = MethodBinding.model_validate(review["old_method"])
        scopes = review["inputs"]["task"]["binding"]["base_refs"]
        if request.listing_ref.model_dump(mode="json") not in scopes or request.old_method != old.method_ref or review["status"] not in {"failed", "partial"}:
            raise InvestorError("invalid_reference", "提案必须关联该证券实际失败或不完整复盘及原方法")
        if not request.config.stronger_than(old.config):
            raise InvestorError("invalid_reference", "仅支持加强现有研究步骤；不能放松规则")
        identifier = str(uuid4())
        value = {"id": identifier, "revision": 1, "created_at": datetime.now(UTC).isoformat(), "status": "evaluating", "request": request.model_dump(mode="json"), "evaluation_ids": [], "shadow_ids": [], "prospective_ids": []}
        self.session.add(MethodProposal(id=identifier, owner_id=self.owner, revision=1, payload=value))
        self.save("proposal_version", value)
        return await self.receipt(request.mutation_id, request, value)

    async def revise(self, row: MethodProposal, revision: int, changes: dict[str, Any]) -> dict[str, Any]:
        value = {**row.payload, **changes, "revision": revision + 1}
        result = await self.session.execute(update(MethodProposal).where(MethodProposal.id == row.id, MethodProposal.revision == revision).values(revision=revision + 1, payload=value))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise InvestorError("stale_revision", "提案已变更，请刷新")
        self.save("proposal_version", value)
        await self.session.flush()
        return value

    async def check_sample(self, review: dict[str, Any], config: MethodConfig, progress: dict[str, Any]) -> dict[str, Any]:
        from investment_agent.agent.execution import ExecutionDeniedError
        from investment_agent.agent.research import ResearchProgress, validate_progress
        try:
            parsed = ResearchProgress.model_validate(progress)
            from investment_agent.investor.contracts import CompanyRecommendation
            from investment_agent.investor.publication import PublicationService
            directional = any(PublicationService._directional(CompanyRecommendation.model_validate(item["payload"])) for item in review["inputs"]["publications"] if item["kind"] == "recommendation")
            await validate_progress(self.session, UUID(review["task_id"]), parsed, method_config=config, final=True, formal_directional=directional)
            return {"result": "pass", "errors": []}
        except (ExecutionDeniedError, InvestorError, ValueError, LookupError) as exc:
            return {"result": "block", "errors": [getattr(exc, "code", type(exc).__name__ + ":" + str(exc)[:200])]}

    @method_transaction
    async def evaluate(self, request: EvaluateProposal) -> dict[str, Any]:
        if result := await self.replay(request.mutation_id, request):
            return result
        row = await self.proposal(request.proposal_id)
        if row.revision != request.expected_revision or row.payload["status"] != "evaluating":
            raise InvestorError("stale_revision", "提案状态已变化")
        config = MethodConfig.model_validate(row.payload["request"]["config"])
        old = await self.known(ObjectRef.model_validate(row.payload["request"]["old_method"]))
        results = []
        for sample in request.cases:
            review = await self.artifact(sample.review_id, "process_review")
            scopes = review["inputs"]["task"]["binding"]["base_refs"]
            applicable = row.payload["request"]["listing_ref"] in scopes
            baseline = MethodBinding.model_validate(review["old_method"])
            if (sample.role == "failure" and not applicable) or (applicable and baseline.method_ref != old.method_ref):
                raise InvestorError("invalid_reference", "失败样本范围或原方法不一致")
            if (sample.role == "failure" and (review["status"] not in {"failed", "partial"} or sample.expected != "block")) or (sample.role == "counterexample" and (review["status"] != "succeeded" or sample.expected != "pass")):
                raise InvestorError("invalid_reference", "失败与反例必须来自相应真实任务状态")
            results.append({**sample.model_dump(mode="json"), "frozen_review": review, "applicable": applicable, "baseline": await self.check_sample(review, baseline.config, review["progress"]), "proposed": await self.check_sample(review, config if applicable else baseline.config, review["progress"])})
        complete = {r["role"] for r in results} == {"failure", "counterexample"} and any(r["role"] == "counterexample" and not r["applicable"] for r in results) and any(r["role"] == "counterexample" and r["applicable"] for r in results) and row.payload["request"]["review_id"] in {r["review_id"] for r in results}
        passed = complete and all(r["proposed"]["result"] == r["expected"] for r in results) and all(r["baseline"]["result"] == "pass" for r in results if r["role"] == "counterexample") and any(r["baseline"]["result"] == "pass" and r["proposed"]["result"] == "block" for r in results if r["role"] == "failure")
        value = self.save("evaluation", {"proposal_id": row.id, "proposal_revision": row.revision, "cases": results, "complete": complete, "passed": passed, "quality": "structural_engineering_replay_not_semantic_acceptance"})
        proposal = await self.revise(row, request.expected_revision, {"evaluation_ids": [*row.payload["evaluation_ids"], value["id"]], "status": "shadow" if passed else "evaluating"})
        return await self.receipt(request.mutation_id, request, {"evaluation": value, "proposal": proposal})

    @method_transaction
    async def prospective(self, request: "ProspectiveRequest") -> dict[str, Any]:
        if result := await self.replay(request.mutation_id, request):
            return result
        row = await self.proposal(request.proposal_id)
        evaluation = await self.artifact(request.evaluation_id, "evaluation")
        if row.revision != request.expected_revision or row.payload["status"] != "shadow" or not evaluation["passed"] or row.payload["evaluation_ids"][-1] != evaluation["id"]:
            raise InvestorError("stale_revision", "需要当前已通过的评估")
        config = MethodConfig.model_validate(row.payload["request"]["config"])
        at = datetime.fromisoformat(evaluation["created_at"])
        sampled_ids = {c["frozen_review"]["task_id"] for c in evaluation["cases"]}
        cases = []
        for task_id in request.task_ids:
            review = await self.process_review(task_id)
            task = review["inputs"]["task"]
            admission = review["inputs"]["admission"]
            if str(task_id) in sampled_ids or datetime.fromisoformat(task["created_at"].replace("Z", "+00:00")) <= at or row.payload["request"]["listing_ref"] not in task["binding"]["base_refs"] or review["old_method"]["method_ref"] != row.payload["request"]["old_method"] or not admission or admission["research_kind"] not in {"company_research", "review"}:
                raise InvestorError("invalid_reference", "前瞻影子只接受评估后新建、同范围同原方法的真实完整研究任务；历史重放不算前瞻")
            baseline = MethodBinding.model_validate(review["old_method"])
            checked_old = await self.check_sample(review, baseline.config, review["progress"])
            checked_new = await self.check_sample(review, config, review["progress"])
            expected = "pass" if review["status"] == "succeeded" else "block"
            cases.append({"task_id": str(task_id), "task_revision": task["revision"], "expected": expected, "frozen_review": review, "baseline": checked_old, "proposed": checked_new})
        complete = {c["expected"] for c in cases} == {"pass", "block"}
        passed = complete and all(c["baseline"]["result"] == "pass" and c["proposed"]["result"] == c["expected"] for c in cases)
        value = self.save("prospective_shadow", {"proposal_id": row.id, "proposal_revision": row.revision, "evaluation_id": evaluation["id"], "evaluation_hash": fingerprint(evaluation), "cases": cases, "complete": complete, "passed": passed, "quality": "prospective_gate_only_actual_new_native_task_outputs_not_semantic_quality", "side_effects": "none; observes existing completed tasks; no model/publish/notification/ledger calls"})
        proposal = await self.revise(row, row.revision, {"prospective_ids": [*row.payload.get("prospective_ids", []), value["id"]]})
        return await self.receipt(request.mutation_id, request, {"prospective": value, "proposal": proposal})

    def summary(self, row: MethodProposal) -> dict[str, Any]:
        payload = dict(row.payload)
        counts = {}
        for field in ("evaluation_ids", "shadow_ids", "prospective_ids"):
            ids = payload.get(field, [])
            counts[field] = len(ids)
            payload[field] = ids[-1:]
        return {"proposal": payload, "history_counts": counts, "detail_loaded": False}

    @staticmethod
    def review_summary(review: dict[str, Any], *, full_progress: bool = False) -> dict[str, Any]:
        progress = review["progress"]
        display = {"question": progress["question"][:2000], "kind": progress["kind"], "answered": progress["answered"], "stopped_because": (progress.get("stopped_because") or "")[:2000] or None}
        if full_progress:
            display["hypotheses"] = [{"question": h["question"][:1000], "alternative": h["alternative"][:2000], "falsification_condition": h["falsification_condition"][:2000]} for h in progress.get("hypotheses", [])[:8]]
            display["gaps"] = [g[:2000] for g in progress.get("gaps", [])[:8]]
        return {"id": review["id"], "task_id": review["task_id"], "status": review["status"], "created_at": review["created_at"], "old_method": review["old_method"], "input_hash": review["input_hash"], "progress": display, "inputs": {"task": {"binding": {"base_refs": review["inputs"]["task"]["binding"]["base_refs"][:16]}}}, "scope_display_truncated": len(review["inputs"]["task"]["binding"]["base_refs"]) > 16, "display": "bounded excerpts; exact immutable source hash and artifact remain available"}

    async def evidence(self, row: MethodProposal) -> dict[str, Any]:
        # Approval binds the exact current revision and latest immutable evidence.
        # Earlier attempts remain in artifacts/proposal-version history; never
        # duplicate every saved task/action history in a workbench response.
        summary = self.summary(row)
        hashes = {}
        collections: dict[str, list[dict[str, Any]]] = {}
        for field, output, kind in (("evaluation_ids", "evaluations", "evaluation"), ("shadow_ids", "shadows", "shadow"), ("prospective_ids", "prospective", "prospective_shadow")):
            values = []
            for identifier in row.payload.get(field, [])[-1:]:
                record = await self.artifact(UUID(identifier), kind)
                hashes[identifier] = fingerprint(record)
                value = dict(record)
                cases = []
                for sample in record.get("cases", []):
                    case = dict(sample)
                    if "frozen_review" in case:
                        case["frozen_review"] = self.review_summary(case["frozen_review"], full_progress=True)
                    if "output" in case and "output" in case["output"]:
                        shadow_output = dict(case["output"])
                        progress = shadow_output["output"]
                        shadow_output["output"] = {"question": progress["question"][:2000], "stopped_because": (progress.get("stopped_because") or "")[:2000] or None, "hypotheses": [{"alternative": h["alternative"][:2000]} for h in progress.get("hypotheses", [])[:8]]}
                        case["output"] = shadow_output
                    cases.append(case)
                value["cases"] = cases
                values.append(value)
            collections[output] = values
        revision, current = await self.current(ObjectRef.model_validate(row.payload["request"]["listing_ref"]))
        payload = {**summary, **collections, "artifact_hashes": hashes, "head_revision": revision, "current_binding": current.model_dump(mode="json"), "detail_loaded": True, "history_note": "Latest evidence only; prior immutable artifacts accessible by exact ID. Display excerpts are bounded."}
        return {**payload, "evidence_hash": fingerprint(payload), "approval_summary": row.payload["request"]["summary"]}

    async def move_head(self, listing: ObjectRef, expected: int, binding: MethodBinding) -> None:
        head = await self.session.get(MethodHead, (self.owner, listing.id), populate_existing=True)
        if (head.revision if head else 0) != expected:
            raise InvestorError("stale_revision", "适用范围的方法已变化")
        if head:
            changed = await self.session.execute(update(MethodHead).where(MethodHead.owner_id == self.owner, MethodHead.listing_id == listing.id, MethodHead.revision == expected).values(revision=expected + 1, payload=binding.model_dump(mode="json")))
            if changed.rowcount != 1:  # type: ignore[attr-defined]
                raise InvestorError("stale_revision", "适用范围的方法已变化")
        else:
            self.session.add(MethodHead(owner_id=self.owner, listing_id=listing.id, revision=1, payload=binding.model_dump(mode="json")))

    @method_transaction
    async def decide(self, request: MethodDecision, *, actor: str) -> dict[str, Any]:
        if actor != "authenticated_user":
            raise InvestorError("invalid_reference", "模型不能批准方法")
        if result := await self.replay(request.mutation_id, request):
            return result
        row = await self.proposal(request.proposal_id)
        evidence = await self.evidence(row)
        if row.revision != request.expected_revision or row.payload["request"]["listing_ref"] != request.listing_ref.model_dump(mode="json") or evidence["evidence_hash"] != request.evidence_hash or evidence["approval_summary"] != request.summary:
            raise InvestorError("stale_revision", "确认内容、范围或冻结证据已变化")
        if row.payload["status"] not in {"evaluating", "shadow"}:
            raise InvestorError("stale_revision", "该提案已结束")
        changes: dict[str, Any] = {"status": "rejected", "decision": request.model_dump(mode="json"), "actor": actor}
        if request.decision == "approve":
            evaluations, prospective = evidence["evaluations"], evidence["prospective"]
            if row.payload["status"] != "shadow" or not evaluations or not evaluations[-1]["passed"] or not prospective or not prospective[-1]["passed"] or prospective[-1]["evaluation_id"] != evaluations[-1]["id"]:
                raise InvestorError("invalid_reference", "需要完整通过的失败样本、反例与隔离影子运行")
            _, current = await self.current(request.listing_ref)
            if current.method_ref != ObjectRef.model_validate(row.payload["request"]["old_method"]):
                raise InvestorError("stale_revision", "当前方法已改变，需要重新评估")
            method_id = uuid4()
            binding = MethodBinding(method_ref=ObjectRef(kind="method", id=method_id, revision=1), listing_ref=request.listing_ref, config=MethodConfig.model_validate(row.payload["request"]["config"]))
            self.session.add(MethodArtifact(id=str(method_id), owner_id=self.owner, kind="version", payload={"id": str(method_id), "created_at": datetime.now(UTC).isoformat(), "binding": binding.model_dump(mode="json"), "proposal_id": row.id, "evidence_hash": request.evidence_hash, "actor": actor}))
            active_proposals = (await self.session.scalars(select(MethodProposal).where(MethodProposal.owner_id == self.owner))).all()
            for active in active_proposals:
                if active.id != row.id and active.payload.get("method_ref") == current.method_ref.model_dump(mode="json") and active.payload["status"] == "active":
                    await self.revise(active, active.revision, {"status": "retired"})
            await self.move_head(request.listing_ref, request.expected_head_revision, binding)
            changes.update(status="active", method_ref=binding.method_ref.model_dump(mode="json"))
        proposal = await self.revise(row, request.expected_revision, changes)
        self.save("event", {"operation": request.decision, "proposal_id": row.id, "proposal": proposal, "frozen_evidence": evidence, "request": request.model_dump(mode="json")})
        return await self.receipt(request.mutation_id, request, proposal)

    @method_transaction
    async def rollback(self, request: MethodRollback, *, actor: str) -> dict[str, Any]:
        if actor != "authenticated_user":
            raise InvestorError("invalid_reference", "模型不能回退方法")
        if result := await self.replay(request.mutation_id, request):
            return result
        target = await self.known(request.target_method)
        if target.listing_ref is not None and target.listing_ref != request.listing_ref:
            raise InvestorError("invalid_reference", "目标方法属于其他证券")
        revision, previous = await self.current(request.listing_ref)
        if revision != request.expected_head_revision:
            raise InvestorError("stale_revision", "方法已变化")
        # Every rollback appends a new version, including rollback to baseline.
        method_id = uuid4()
        binding = target.model_copy(update={"method_ref": ObjectRef(kind="method", id=method_id, revision=1), "listing_ref": request.listing_ref})
        value = {"id": str(method_id), "created_at": datetime.now(UTC).isoformat(), "binding": binding.model_dump(mode="json"), "rollback_target": request.target_method.model_dump(mode="json"), "previous": previous.model_dump(mode="json"), "reason": request.reason, "actor": actor}
        self.session.add(MethodArtifact(id=str(method_id), owner_id=self.owner, kind="version", payload=value))
        await self.move_head(request.listing_ref, revision, binding)
        proposals = (await self.session.scalars(select(MethodProposal).where(MethodProposal.owner_id == self.owner))).all()
        for row in proposals:
            if row.payload.get("method_ref") == previous.method_ref.model_dump(mode="json") and row.payload["status"] == "active":
                await self.revise(row, row.revision, {"status": "retired"})
        self.save("event", {"operation": "rollback", "version": value, "request": request.model_dump(mode="json")})
        return await self.receipt(request.mutation_id, request, value)


class ShadowRequest(Contract):
    mutation_id: UUID
    proposal_id: UUID
    expected_revision: int = Field(ge=1)
    evaluation_id: UUID


async def run_offline_shadow(factory: Any, request: ShadowRequest, model: Any) -> dict[str, Any]:
    """One finite native FunctionModel run per frozen sample, no tools or effects.

    This is an offline model-boundary replay, never a paid/production trial. No
    transaction spans a model call. Reject/change/rollback fences stale results.
    Stable mutation replay never repeats an uncertain model call.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import UsageLimits

    from investment_agent.agent.research import ResearchProgress
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
        claim = service.save("shadow_claim", {"proposal_id": row.id, "evaluation_id": evaluation["id"], "evaluation_hash": fingerprint(evaluation), "request": request.model_dump(mode="json")})
        await service.revise(row, row.revision, {"shadow_claim_id": claim["id"]})
        await service.receipt(request.mutation_id, request, {"status": "running", "claim_id": claim["id"], "retry_policy": "same mutation returns receipt; explicit new trial required after interrupted offline run"})
        await session.commit()
    outputs = []
    for sample in evaluation["cases"]:
        try:
            import json
            agent = Agent(model, output_type=ResearchProgress, retries=0, instructions="Offline method replay. Return the frozen research progress; no tools, publication or external effects.")
            native_result = await agent.run(json.dumps({"frozen_progress": sample["frozen_review"]["progress"], "method_config": config.model_dump(mode="json")}), usage_limits=UsageLimits(request_limit=1))
            outputs.append({"review_id": sample["review_id"], "output": native_result.output.model_dump(mode="json"), "usage": {"requests": native_result.usage.requests, "input_tokens": native_result.usage.input_tokens, "output_tokens": native_result.usage.output_tokens}})
        except Exception as exc:
            outputs.append({"review_id": sample["review_id"], "error": type(exc).__name__})
    async with factory() as session:
        service = MethodService(session)
        await service.reserve()
        row = await service.proposal(request.proposal_id)
        if row.revision != request.expected_revision + 1 or row.payload["status"] != "shadow" or row.payload.get("shadow_claim_id") != claim["id"]:
            value = service.save("shadow", {"proposal_id": row.id, "evaluation_id": evaluation["id"], "outputs": outputs, "passed": False, "fenced": True, "quality": "offline_FunctionModel_engineering_only"})
        else:
            cases = []
            for sample, output in zip(evaluation["cases"], outputs, strict=True):
                sample_config = config if sample["applicable"] else MethodBinding.model_validate(sample["frozen_review"]["old_method"]).config
                checked = await service.check_sample(sample["frozen_review"], sample_config, output["output"]) if "output" in output else {"result": "model_error", "errors": [output["error"]]}
                cases.append({"review_id": sample["review_id"], "role": sample["role"], "expected": sample["expected"], "checked": checked, "output": output})
            value = service.save("shadow", {"proposal_id": row.id, "evaluation_id": evaluation["id"], "evaluation_hash": fingerprint(evaluation), "cases": cases, "passed": all(c["checked"]["result"] == c["expected"] for c in cases), "quality": "offline_FunctionModel_engineering_only"})
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
    def unique_tasks(self) -> "ProspectiveRequest":
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("distinct actual fresh tasks required")
        return self

"""Finite research-step changes. This module cannot change permissions or publish.

Artifacts freeze actual saved inputs/outputs. Offline model runs are explicitly
FunctionModel replays, engineering evidence only, never semantic quality proof.
"""
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    ValidationError,
    model_serializer,
    model_validator,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.tasks import COMPLETED_TASK_STATUSES, fingerprint
from investment_agent.investor.common import Contract, ObjectRef, Text
from investment_agent.investor.foundations import InvestorError, storage_conflicts
from investment_agent.investor.method_shadow import (
    CANDIDATE_OUTPUT_CONTRACT,
    CANDIDATE_SHADOW_FORBIDDEN_TOOLS,
    CandidateShadowBatchRequest,
    CandidateShadowItem,
    CandidateShadowRequest,
    CandidateShadowResultRequest,
    ProspectiveRequest,
    ShadowRequest,
    _candidate_shadow_replay,
    _frozen_ref_identities,
    run_offline_shadow,
)

__all__ = [
    "CANDIDATE_OUTPUT_CONTRACT",
    "CANDIDATE_SHADOW_FORBIDDEN_TOOLS",
    "CandidateShadowBatchRequest",
    "CandidateShadowItem",
    "CandidateShadowRequest",
    "CandidateShadowResultRequest",
    "MethodBinding",
    "MethodConfig",
    "MethodService",
    "ProspectiveRequest",
    "ShadowRequest",
    "_candidate_shadow_replay",
    "_frozen_ref_identities",
    "enforce_steps",
    "run_offline_shadow",
    "task_method",
    "task_method_receipt",
]
from investment_agent.investor.publication import FIXED_METHOD
from investment_agent.storage.models.agent import AgentTask, ToolAction
from investment_agent.storage.models.investor import (
    InvestorMutation,
    MethodArtifact,
    MethodDefaultHead,
    MethodHead,
    MethodProposal,
    TaskMethodBinding,
)

if TYPE_CHECKING:
    from investment_agent.agent.method_receipt import TaskMethodReceipt
    from investment_agent.config import Settings
    from investment_agent.investor.method_packages import MethodPackage


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
    schema_version: Literal[1, 2] = 1
    method_ref: ObjectRef = FIXED_METHOD
    config: MethodConfig = MethodConfig()
    listing_ref: ObjectRef | None = None
    # v2 only: the immutable package artifact behind this method version.
    package_ref: ObjectRef | None = None
    package_hash: str | None = None

    @model_validator(mode="after")
    def package_shape(self) -> "MethodBinding":
        if self.schema_version == 1 and (self.package_ref is not None or self.package_hash is not None):
            raise ValueError("v1 bindings cannot carry a package reference")
        if self.schema_version == 2 and (self.package_ref is None or self.package_hash is None):
            raise ValueError("v2 bindings require package_ref and package_hash")
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # v1 payloads keep their exact existing shape (no new keys); only v2
        # bindings carry the package fields (§4.3).
        data = cast(dict[str, Any], handler(self))
        if self.schema_version == 1:
            data.pop("package_ref", None)
            data.pop("package_hash", None)
        return data


# ``method_packages`` imports this module for ``MethodConfig`` while the
# proposal contract below reuses its ``MethodPackageDefinition``; the import
# resolves here, after that class exists (see the import note in
# investor/method_packages.py), so either import order converges. The stored
# ``MethodPackage`` artifact class is defined only after that deferred import,
# so it stays under TYPE_CHECKING and binds at call time in the helpers.
from investment_agent.investor.method_packages import MethodPackageDefinition  # noqa: E402


class CreateProposal(Contract):
    mutation_id: UUID
    review_id: UUID
    listing_ref: ObjectRef
    old_method: ObjectRef
    error_pattern: Text = Field(max_length=1000)
    summary: Text = Field(max_length=1000)
    config: MethodConfig
    package: MethodPackageDefinition | None = None  # §7: package proposals carry the candidate contract


# In the packages-first import order this class is created while
# method_packages is still completing its own deferred discipline annotation;
# the rebuild makes schema completion explicit instead of relying on
# pydantic's implicit rebuild at first use.
CreateProposal.model_rebuild()


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


class ActivateGlobal(Contract):
    mutation_id: UUID
    channel: str = "company-research"
    package_ref: ObjectRef
    expected_revision: int = Field(ge=0)
    reason: Text


async def task_method(session: AsyncSession, task_id: UUID) -> MethodBinding:
    from investment_agent.agent.method_receipt import parse_task_method_payload
    receipt = await session.get(TaskMethodBinding, str(task_id))
    if not receipt:
        return MethodBinding()
    binding, _ = parse_task_method_payload(receipt.payload)
    return binding


async def task_method_receipt(session: AsyncSession, task_id: UUID) -> "TaskMethodReceipt | None":
    """The v2 admission receipt; None for v1 tasks, which never froze one."""
    from investment_agent.agent.method_receipt import parse_task_method_payload
    receipt = await session.get(TaskMethodBinding, str(task_id))
    if not receipt:
        return None
    return parse_task_method_payload(receipt.payload)[1]


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

    @method_transaction
    async def save_package(
        self, definition: "MethodPackageDefinition", *, source: str, proposal_id: UUID | None = None, evidence_hash: str | None = None
    ) -> dict[str, Any]:
        """Append-only package storage; identical content dedupes to one artifact."""
        from investment_agent.investor.method_packages import (
            MethodPackage,
            MethodPackageError,
            canonical_hash,
            validate_definition,
        )
        if errors := validate_definition(definition):
            raise MethodPackageError(errors, source=source)
        content_hash = canonical_hash(definition)
        rows = (await self.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == self.owner, MethodArtifact.kind == "package"))).all()
        versions: list[int] = []
        for row in rows:
            if row.payload["package_key"] != definition.package_key:
                continue
            versions.append(row.payload["version"])
            if row.payload["content_hash"] == content_hash:
                return row.payload
        artifact_id = uuid4()
        package = MethodPackage(
            **definition.model_dump(),
            artifact_id=artifact_id,
            version=max(versions, default=0) + 1,
            content_hash=content_hash,
            created_at=datetime.now(UTC),
            source=source,
            proposal_id=proposal_id,
            evidence_hash=evidence_hash,
        )
        payload = package.model_dump(mode="json")
        self.session.add(MethodArtifact(id=str(artifact_id), owner_id=self.owner, kind="package", payload=payload))
        await self.session.flush()
        return payload

    async def package(self, package_key: str, version: int) -> dict[str, Any]:
        rows = (await self.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == self.owner, MethodArtifact.kind == "package"))).all()
        for row in rows:
            if row.payload["package_key"] == package_key and row.payload["version"] == version:
                return row.payload
        raise InvestorError("not_found", "方法包版本不可用")

    async def package_by_ref(self, ref: ObjectRef) -> dict[str, Any]:
        row = await self.session.get(MethodArtifact, str(ref.id))
        if ref.kind != "package" or not row or row.owner_id != self.owner or row.kind != "package":
            raise InvestorError("not_found", "方法包工件不可用")
        return row.payload

    async def packages(self, package_key: str | None = None) -> list[dict[str, Any]]:
        """Read-only listing of stored package artifacts, key filter, version ascending."""
        rows = (await self.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == self.owner, MethodArtifact.kind == "package"))).all()
        payloads = [row.payload for row in rows if package_key is None or row.payload["package_key"] == package_key]
        return sorted(payloads, key=lambda payload: (payload["package_key"], payload["version"]))

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

    async def default_head(self, channel: str = "company-research") -> tuple[int, MethodBinding | None]:
        row = await self.session.get(MethodDefaultHead, (self.owner, channel), populate_existing=True)
        return (row.revision, MethodBinding.model_validate(row.payload)) if row else (0, None)

    async def resolve_binding(self, listing: ObjectRef | None, research_kind: str) -> MethodBinding:
        """per-listing head -> global default head -> legacy v1 baseline (§5)."""
        if listing is not None and research_kind in {"company_research", "review"}:
            revision, binding = await self.current(listing)
            if revision:
                return binding
            _, default = await self.default_head()
            if default is not None:
                return default
        return MethodBinding()

    async def freeze_task(self, task_id: UUID, refs: tuple[ObjectRef, ...], research_kind: str, *, tool_names: tuple[str, ...] = (), override_binding: "MethodBinding | None" = None) -> None:
        # Historical Tasks without this receipt always remain the exact baseline.
        # Multi-listing/comparison/portfolio contexts have a documented baseline,
        # never a silently mixed policy. Only one-listing full research can opt in.
        listings = tuple({r for r in refs if r.kind == "listing"})
        if override_binding is not None:
            # §7.3 candidate-package prospective shadow: the caller freezes the
            # proposal's candidate v2 binding; no head is read and the shadow
            # task really executes the candidate package.
            binding = override_binding
        else:
            binding = MethodBinding()
            if len(listings) == 1 and research_kind in {"company_research", "review"}:
                # §5 resolution order: per-listing head, then the explicit global
                # default head; without either, the documented v1 baseline keeps
                # its exact current payload shape (listing_ref included).
                head = await self.session.get(MethodHead, (self.owner, listings[0].id))
                resolved = MethodBinding.model_validate(head.payload) if head else None
                if resolved is None:
                    resolved = (await self.default_head())[1]
                binding = MethodBinding(listing_ref=listings[0]) if resolved is None else resolved
        if binding.package_ref is None:
            self.session.add(TaskMethodBinding(task_id=str(task_id), payload=binding.model_dump(mode="json")))
            return
        receipt = await self._freeze_package_receipt(binding, research_kind, tool_names, shadow=override_binding is not None)
        self.session.add(TaskMethodBinding(task_id=str(task_id), payload={"schema_version": 2, "receipt": receipt.model_dump(mode="json")}))

    async def _freeze_package_receipt(self, binding: MethodBinding, research_kind: str, tool_names: tuple[str, ...], *, shadow: bool = False) -> "TaskMethodReceipt":
        """v2 admission (§6.1): verify the package artifact, then freeze the receipt.

        A hash or config mismatch is storage corruption and fails closed; the
        stored package is the immutable source, never re-fetched from YAML.
        Missing capabilities deny admission - they never silently downgrade
        the task to the v1 baseline.
        """
        from investment_agent.agent.execution import ExecutionDeniedError
        from investment_agent.agent.method_receipt import VALIDATOR_V1, build_receipt
        from investment_agent.investor.method_packages import (
            MethodPackage,
            canonical_hash,
            validate_definition,
        )
        assert binding.package_ref is not None and binding.package_hash is not None
        try:
            package = MethodPackage.model_validate(await self.package_by_ref(binding.package_ref))
        except ValidationError as exc:
            raise InvestorError("method_package_corrupt", "方法包工件不可读，任务拒绝准入") from exc
        if canonical_hash(package) != binding.package_hash or binding.config != package.discipline:
            raise InvestorError("method_package_corrupt", "方法包工件与绑定不一致，任务拒绝准入")
        if errors := validate_definition(package):
            raise InvestorError("method_package_corrupt", f"方法包复验失败：{'；'.join(errors)}")
        if research_kind not in package.applicable_kinds:
            raise InvestorError("method_package_not_applicable", "方法包不适用于该研究类型，任务拒绝准入")
        # Card 07 (§11 M2): new v2 tasks freeze as enforcing (validator v1).
        # Receipts frozen before this switch keep their render-only version and
        # stay render-only forever (§10) - the validator dispatches on the
        # frozen string, never on the deployed code.
        receipt = build_receipt(binding, package, validator_version=VALIDATOR_V1)
        required: set[str] = set()
        for members in receipt.tool_family_members.values():
            required.update(members)
        if missing := sorted(required - set(tool_names)):
            raise ExecutionDeniedError(f"method_capability_unavailable: {', '.join(missing)}")
        if shadow:
            # §7.3: the shadow receipt records the marker plus the proposal's
            # old-method ref (the caller builds the binding with exactly that
            # method_ref); candidate_shadow_result re-checks the equality.
            receipt = receipt.model_copy(update={"shadow": "candidate_package", "candidate_base_method_ref": binding.method_ref})
        return receipt

    @method_transaction
    async def process_review(self, task_id: UUID) -> dict[str, Any]:
        await self.reserve()
        task = await self.session.get(AgentTask, str(task_id))
        if not task or task.owner_id != self.owner or task.status not in COMPLETED_TASK_STATUSES:
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
        identifier = str(uuid4())
        frozen: dict[str, Any] = {}
        if request.package is None:
            # config-only proposals keep the exact existing strengthening path.
            if not request.config.stronger_than(old.config):
                raise InvestorError("invalid_reference", "仅支持加强现有研究步骤；不能放松规则")
        else:
            frozen = await self._freeze_package_candidate(request, old, identifier)
        # The request section keeps the full model dump (including nested nulls):
        # evaluate/prospective/decide compare these dicts against review scope
        # refs, and package proposals add their frozen candidate keys on top.
        request_payload = request.model_dump(mode="json")
        request_payload.update(frozen)
        value = {"id": identifier, "revision": 1, "created_at": datetime.now(UTC).isoformat(), "status": "evaluating", "request": request_payload, "evaluation_ids": [], "shadow_ids": [], "prospective_ids": []}
        self.session.add(MethodProposal(id=identifier, owner_id=self.owner, revision=1, payload=value))
        self.save("proposal_version", value)
        return await self.receipt(request.mutation_id, request, value)

    async def _freeze_package_candidate(self, request: CreateProposal, old: MethodBinding, identifier: str) -> dict[str, Any]:
        """Validate a package proposal and freeze its candidate (design §7).

        ``config`` must equal ``package.discipline`` field by field; the
        definition passes the aggregate validator; §8 requires zero
        strengthening violations against the old method's package and at least
        one strictly stronger research requirement (version/hash/presentation
        changes alone are no enhancement). The stored artifact ref, content
        hash and rendered-method hash then freeze with this proposal version:
        ``revise`` never rewrites them and a duplicate ``create`` replays.
        """
        from investment_agent.agent.method_receipt import render_method
        from investment_agent.investor.method_packages import (
            MethodPackage,
            legacy_package_for,
            strengthening_violations,
            strictly_stronger_requirements,
            validate_definition,
        )
        assert request.package is not None
        package = request.package
        mismatched = [name for name in type(request.config).model_fields if getattr(request.config, name) != getattr(package.discipline, name)]
        if mismatched:
            raise InvestorError("invalid_reference", f"提案 config 与 package.discipline 不一致：{', '.join(mismatched)}")
        if errors := validate_definition(package):
            raise InvestorError("invalid_reference", f"方法包校验失败：{'；'.join(errors)}")
        if old.package_ref is None:
            # FIXED_METHOD or a v1 method version: the virtual legacy baseline
            # with the review's frozen discipline switches (§8).
            old_definition, old_kind = legacy_package_for(old.config), "legacy"
        else:
            # A v2 method version: the stored package artifact is the baseline.
            old_definition, old_kind = MethodPackage.model_validate(await self.package_by_ref(old.package_ref)), "package"
        if violations := strengthening_violations(old_definition, package):
            raise InvestorError("invalid_reference", f"包提案违反加强约束：{'；'.join(violations)}")
        if not strictly_stronger_requirements(old_definition, package):
            raise InvestorError("invalid_reference", "包提案无实质增强：仅版本、哈希或文案变化不构成研究要求增强")
        saved = await self.save_package(package, source="proposal", proposal_id=UUID(identifier), evidence_hash=None)
        rendered = render_method(package)
        return {
            "package_ref": ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]).model_dump(mode="json"),
            "content_hash": saved["content_hash"],
            "rendered_method_hash": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "old_package_kind": old_kind,
        }

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
        # §7.1: package proposals replay every applicable sample against the
        # proposal-frozen candidate; config-only proposals keep the exact
        # existing check_sample path byte for byte.
        candidate = await self._frozen_candidate(row)
        strictly = await self._strengthened_requirements(row, candidate) if candidate is not None else ()
        results = []
        shadow: set[str] = set()
        for sample in request.cases:
            review = await self.artifact(sample.review_id, "process_review")
            scopes = review["inputs"]["task"]["binding"]["base_refs"]
            applicable = row.payload["request"]["listing_ref"] in scopes
            baseline = MethodBinding.model_validate(review["old_method"])
            if (sample.role == "failure" and not applicable) or (applicable and baseline.method_ref != old.method_ref):
                raise InvestorError("invalid_reference", "失败样本范围或原方法不一致")
            if (sample.role == "failure" and (review["status"] not in {"failed", "partial"} or sample.expected != "block")) or (sample.role == "counterexample" and (review["status"] != "succeeded" or sample.expected != "pass")):
                raise InvestorError("invalid_reference", "失败与反例必须来自相应真实任务状态")
            baseline_check = await self.check_sample(review, baseline.config, review["progress"])
            if candidate is None:
                proposed = await self.check_sample(review, config if applicable else baseline.config, review["progress"])
            elif applicable:
                proposed = await self._replay_sample(review, candidate)
                shadow.update(proposed["requires_candidate_shadow"])
            else:
                # Out-of-scope counterexamples keep their own method and the
                # existing baseline pass semantics; the candidate never touches them.
                proposed = await self.check_sample(review, baseline.config, review["progress"])
            results.append({**sample.model_dump(mode="json"), "frozen_review": review, "applicable": applicable, "baseline": baseline_check, "proposed": proposed})
        complete = {r["role"] for r in results} == {"failure", "counterexample"} and any(r["role"] == "counterexample" and not r["applicable"] for r in results) and any(r["role"] == "counterexample" and r["applicable"] for r in results) and row.payload["request"]["review_id"] in {r["review_id"] for r in results}
        if candidate is None:
            passed = complete and all(r["proposed"]["result"] == r["expected"] for r in results) and all(r["baseline"]["result"] == "pass" for r in results if r["role"] == "counterexample") and any(r["baseline"]["result"] == "pass" and r["proposed"]["result"] == "block" for r in results if r["role"] == "failure")
        else:
            def substantial(entry: dict[str, Any]) -> bool:
                return entry["proposed"]["state"] == "block" and bool(set(entry["proposed"]["blocked_requirements"]) & set(strictly))
            failures = [r for r in results if r["role"] == "failure"]
            passed = (complete
                and all(r["baseline"]["result"] == "pass" for r in results if r["role"] == "counterexample")
                and any(r["baseline"]["result"] == "pass" for r in failures)
                and all(substantial(r) for r in failures)
                and all(r["proposed"]["state"] != "block" for r in results if r["role"] == "counterexample" and r["applicable"])
                and all(r["proposed"]["result"] == "pass" for r in results if r["role"] == "counterexample" and not r["applicable"]))
        value = self.save("evaluation", {"proposal_id": row.id, "proposal_revision": row.revision, "cases": results, "complete": complete, "passed": passed, "quality": "structural_engineering_replay_not_semantic_acceptance"})
        changes: dict[str, Any] = {"evaluation_ids": [*row.payload["evaluation_ids"], value["id"]], "status": "shadow" if passed else "evaluating"}
        if candidate is not None:
            # Cumulative not-replayable requirement ids across evaluations (§7.1);
            # a non-empty set never blocks here - the prospective candidate
            # shadows must cover it before approval (§7.3, card 15/16).
            changes["requires_candidate_shadow"] = sorted(set(row.payload.get("requires_candidate_shadow", [])) | shadow)
        proposal = await self.revise(row, request.expected_revision, changes)
        return await self.receipt(request.mutation_id, request, {"evaluation": value, "proposal": proposal})

    async def _frozen_candidate(self, row: MethodProposal) -> "MethodPackage | None":
        """The proposal-frozen candidate artifact; None for config-only proposals."""
        from investment_agent.investor.method_packages import MethodPackage
        if row.payload["request"].get("package_ref") is None:
            return None
        return MethodPackage.model_validate(await self.package_by_ref(ObjectRef.model_validate(row.payload["request"]["package_ref"])))

    async def _strengthened_requirements(self, row: MethodProposal, candidate: "MethodPackageDefinition") -> tuple[str, ...]:
        """§8 ids of the research requirements the candidate tightens over the old method."""
        from investment_agent.investor.method_packages import (
            MethodPackage,
            legacy_package_for,
            strictly_stronger_requirements,
        )
        old = await self.known(ObjectRef.model_validate(row.payload["request"]["old_method"]))
        old_definition: MethodPackageDefinition
        if row.payload["request"].get("old_package_kind") == "package":
            # A v2 method version: the stored package artifact is the baseline,
            # exactly as frozen at proposal creation (§7/§8).
            assert old.package_ref is not None
            old_definition = MethodPackage.model_validate(await self.package_by_ref(old.package_ref))
        else:
            # FIXED_METHOD or a v1 method version: the virtual legacy baseline.
            old_definition = legacy_package_for(old.config)
        return strictly_stronger_requirements(old_definition, candidate)

    async def _replay_sample(self, review: dict[str, Any], candidate: "MethodPackage") -> dict[str, Any]:
        """One sample's three-state replay payload against the candidate (§7.1)."""
        from investment_agent.agent.method_receipt import RENDERER_VERSION, VALIDATOR_V1
        from investment_agent.agent.method_validation import (
            replay_context,
            replay_requirements,
            replay_sample_state,
        )
        from investment_agent.agent.research import MethodPlannedProgress
        context = await replay_context(self.session, review, candidate, renderer_version=RENDERER_VERSION, validator_version=VALIDATOR_V1)
        replayed, significant = await replay_requirements(context, MethodPlannedProgress.model_validate(review["progress"]))
        state = replay_sample_state(replayed)
        significant = sorted(set(significant))
        return {"state": state, "replayed": replayed, "blocked_requirements": significant if state == "block" else [], "requires_candidate_shadow": significant if state == "not_replayable" else []}

    @method_transaction
    async def prospective(self, request: ProspectiveRequest) -> dict[str, Any]:
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

    @method_transaction
    async def start_candidate_shadow(self, request: CandidateShadowRequest, *, base: "Settings") -> dict[str, Any]:
        """§7.3 candidate-package prospective shadow initiation (card 15).

        Only package proposals in ``status="shadow"`` with a passed latest
        evaluation qualify. The server builds a background shadow grant bound
        to the proposal listing: publication (``prepare_company_view``) and the
        portfolio family are stripped from the tool whitelist (no publishing,
        no ledger), and the budget follows the existing mandate daily/task cost
        configuration. ``admit()`` enqueues a real ``agent_research`` task
        whose receipt freezes the candidate v2 binding (FunctionModel is never
        usable on this path). The same mutation replays without a new task.
        Card 19 caps the evidence-collection cost: once the proposal already
        has ``method_candidate_shadow_max_per_proposal`` claim artifacts the
        initiation is refused before any write. Card 20 routes the per-item
        admission through the shared ``_admit_candidate_shadow`` coroutine.
        """
        if result := await self.replay(request.mutation_id, request):
            return result
        row, evaluation, candidate = await self._candidate_shadow_target(request.proposal_id)
        started = sum(1 for item in await self._started_shadow_claims() if item.payload["proposal_id"] == row.id)
        if started >= base.method_candidate_shadow_max_per_proposal:
            raise InvestorError("invalid_reference", f"该包提案的候选影子任务数已达上限 {base.method_candidate_shadow_max_per_proposal}（已发起 {started} 个），不能继续发起")
        claim, task_id = await self._admit_candidate_shadow(row, evaluation, candidate, request.prompt, request.expected, f"candidate-shadow:{row.id}:{request.mutation_id}", base=base)
        return await self.receipt(request.mutation_id, request, {"claim": claim, "task_id": task_id, "status": "queued"})

    async def _candidate_shadow_target(self, proposal_id: UUID) -> tuple[MethodProposal, dict[str, Any], "MethodPackage"]:
        """Shared admission state check (card 20): a package proposal in
        ``status="shadow"`` whose latest evaluation passed, plus its
        proposal-frozen candidate artifact."""
        row = await self.proposal(proposal_id)
        if row.payload["status"] != "shadow" or not row.payload.get("evaluation_ids") or row.payload["request"].get("package_ref") is None:
            raise InvestorError("invalid_reference", "候选包前瞻影子只接受已通过评估的包提案")
        evaluation = await self.artifact(UUID(row.payload["evaluation_ids"][-1]), "evaluation")
        if not evaluation["passed"] or evaluation["proposal_id"] != row.id:
            raise InvestorError("stale_revision", "需要当前已通过的评估")
        candidate = await self._frozen_candidate(row)
        assert candidate is not None  # package_ref presence checked above
        return row, evaluation, candidate

    async def _started_shadow_claims(self) -> list[MethodArtifact]:
        """Every ``candidate_shadow_claim`` artifact this owner has written."""
        return list((await self.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == self.owner, MethodArtifact.kind == "candidate_shadow_claim"))).all())

    async def _admit_candidate_shadow(
        self, row: MethodProposal, evaluation: dict[str, Any], candidate: "MethodPackage", prompt: Text, expected: Literal["pass", "block"], input_key: str, *, base: "Settings"
    ) -> tuple[dict[str, Any], str]:
        """Shared per-item admission (card 20): listing scope and kind
        admission, the shadow grant (publication/portfolio tools stripped),
        the ``freeze_task`` override via ``admit(method_override=...)`` and
        the ``candidate_shadow_claim`` artifact. The single initiation and
        every batch item run exactly this code path, so their behaviors
        cannot drift; only the caller's ``input_key`` distinguishes them.
        """
        from investment_agent.agent.contracts import ContextBinding
        from investment_agent.agent.execution import ExecutionGrant, admit
        from investment_agent.agent.research import registrations
        from investment_agent.investor.foundations import InvestorService
        listing = ObjectRef.model_validate(row.payload["request"]["listing_ref"])
        await self.scope(listing)
        kinds = [kind for kind in ("company_research", "review") if kind in candidate.applicable_kinds]
        if not kinds:
            raise InvestorError("invalid_reference", "候选包不适用于任何前瞻影子研究类型")
        mandate = await InvestorService(self.session).get_mandate("local_owner")
        tool_names = tuple(name for name in (registration.name for registration in registrations()) if name not in CANDIDATE_SHADOW_FORBIDDEN_TOOLS)
        binding = MethodBinding(
            schema_version=2,
            method_ref=ObjectRef.model_validate(row.payload["request"]["old_method"]),
            config=candidate.discipline,
            listing_ref=listing,
            package_ref=ObjectRef(kind="package", id=candidate.artifact_id, revision=candidate.version),
            package_hash=candidate.content_hash,
        )
        grant = ExecutionGrant(mode="background", source="authorized_background", prompt=prompt,
            tool_names=tool_names, scope_refs=(listing,), mandate_id=mandate.id, current_research=True, research_kind=kinds[0])
        task = await admit(self.session, base=base, input_key=input_key,
            binding=ContextBinding(context_refs=(listing,), base_refs=(listing,), as_of=datetime.now(UTC)),
            grant=grant, kind="agent_research", method_override=binding)
        claim = self.save("candidate_shadow_claim", {"proposal_id": row.id, "proposal_revision": row.revision,
            "evaluation_id": evaluation["id"], "task_id": str(task.id), "expected": expected,
            "prompt": prompt, "research_kind": kinds[0],
            "quality": "real_worker_shadow_task_no_function_model"})
        return claim, str(task.id)

    @method_transaction
    async def start_candidate_shadows(self, request: CandidateShadowBatchRequest, *, base: "Settings") -> dict[str, Any]:
        """§9 batch initiation (card 20): one mutation, one transaction, up to
        four distinct-prompt items.

        Every item admits through the same shared coroutine as the single
        initiation with its own ``input_key``
        (``candidate-shadow:{proposal_id}:{mutation_id}:{index}``) and its own
        ``candidate_shadow_claim`` artifact; the card-19 per-proposal cap
        counts the whole batch before any write, so an over-cap batch is
        refused atomically. A same-mutation replay returns the original
        receipt without starting new tasks.
        """
        if result := await self.replay(request.mutation_id, request):
            return result
        row, evaluation, candidate = await self._candidate_shadow_target(request.proposal_id)
        started = sum(1 for item in await self._started_shadow_claims() if item.payload["proposal_id"] == row.id)
        if started + len(request.items) > base.method_candidate_shadow_max_per_proposal:
            raise InvestorError("invalid_reference", f"该包提案的候选影子任务数已达上限 {base.method_candidate_shadow_max_per_proposal}（已发起 {started} 个），批量发起 {len(request.items)} 个将超出上限")
        claims: list[dict[str, Any]] = []
        task_ids: list[str] = []
        for index, item in enumerate(request.items):
            claim, task_id = await self._admit_candidate_shadow(row, evaluation, candidate, item.prompt, item.expected, f"candidate-shadow:{row.id}:{request.mutation_id}:{index}", base=base)
            claims.append(claim)
            task_ids.append(task_id)
        return await self.receipt(request.mutation_id, request, {"claims": claims, "task_ids": task_ids, "status": "queued"})

    @method_transaction
    async def candidate_shadow_result(self, request: CandidateShadowResultRequest) -> dict[str, Any]:
        """Register one finished candidate shadow (§7.3/card 15 §4, idempotent replay).

        Reads the shadow task's committed progress plus committed actions,
        rebuilds the frozen validation context (candidate package, frozen
        receipt/grant) and runs ``validate_final``: ``expected="pass"`` must
        pass (positive control covering the current ``requires_candidate_shadow``
        ids), ``expected="block"`` must be rejected (negative control). The
        result artifact records the actual outcome either way; only matched
        runs count as approval evidence (card 16).
        """
        from investment_agent.agent.execution import ExecutionGrant, ResearchValidationError
        from investment_agent.agent.method_validation import build_context, validate_final
        from investment_agent.agent.progress import committed_progress
        from investment_agent.agent.research import MethodPlannedProgress
        from investment_agent.agent.tasks import TaskService
        from investment_agent.storage.models.agent import ExecutionGrantRow
        if result := await self.replay(request.mutation_id, request):
            return result
        row = await self.proposal(request.proposal_id)
        if row.payload["request"].get("package_ref") is None:
            raise InvestorError("invalid_reference", "候选影子结果只登记包提案")
        claims = (await self.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == self.owner, MethodArtifact.kind == "candidate_shadow_claim"))).all()
        claim = next((item.payload for item in claims if item.payload["task_id"] == str(request.task_id) and item.payload["proposal_id"] == row.id), None)
        if claim is None:
            raise InvestorError("not_found", "该任务不是此提案发起的前瞻影子")
        task_row = await self.session.get(AgentTask, str(request.task_id))
        if task_row is None or task_row.owner_id != self.owner or task_row.status not in COMPLETED_TASK_STATUSES:
            raise InvestorError("invalid_reference", "前瞻影子结果需要真实已完成的影子任务")
        receipt = await task_method_receipt(self.session, request.task_id)
        old_method = ObjectRef.model_validate(row.payload["request"]["old_method"])
        if receipt is None or receipt.shadow != "candidate_package" or receipt.candidate_base_method_ref != old_method:
            raise InvestorError("invalid_reference", "影子任务未绑定该提案的候选包或原方法")
        evaluation = await self.artifact(UUID(claim["evaluation_id"]), "evaluation")
        admitted_at = datetime.fromisoformat(task_row.payload["created_at"].replace("Z", "+00:00"))
        if admitted_at <= datetime.fromisoformat(evaluation["created_at"]):
            raise InvestorError("invalid_reference", "前瞻影子任务必须在评估之后新建")
        grant_row = await self.session.get(ExecutionGrantRow, str(request.task_id))
        if grant_row is None:
            raise InvestorError("invalid_reference", "影子任务缺少冻结授权")
        grant = ExecutionGrant.model_validate(grant_row.payload)
        if row.payload["request"]["listing_ref"] not in [ref.model_dump(mode="json") for ref in grant.scope_refs]:
            raise InvestorError("invalid_reference", "影子任务范围与提案不一致")
        service = TaskService(self.session)
        context = await build_context(self.session, task_id=request.task_id, receipt=receipt, grant=grant)
        committed = await committed_progress(self.session, await service.get_task(request.task_id), MethodPlannedProgress)
        if not committed:
            raise InvestorError("invalid_reference", "影子任务没有已提交的研究进展")
        outcome, errors = "passed", []
        try:
            await validate_final(self.session, context, committed[-1][1])
        except ResearchValidationError as exc:
            outcome = "blocked"
            errors = [f"{exc.code}: {error.field}: {error.message}" for error in exc.field_errors]
        matched = (claim["expected"] == "pass" and outcome == "passed") or (claim["expected"] == "block" and outcome == "blocked")
        requirement_ids = sorted(row.payload.get("requires_candidate_shadow", [])) if claim["expected"] == "pass" and outcome == "passed" else []
        value = self.save("candidate_shadow", {"proposal_id": row.id, "proposal_revision": row.revision,
            "claim_id": claim["id"], "task_id": str(request.task_id), "expected": claim["expected"],
            "outcome": outcome, "matched": matched, "requirement_ids": requirement_ids, "errors": errors,
            "rendered_method_hash": receipt.rendered_method_hash,
            "quality": "prospective_shadow_engineering_evidence_only_no_publication"})
        proposal = await self.revise(row, request.expected_revision, {"candidate_shadow_ids": [*row.payload.get("candidate_shadow_ids", []), value["id"]]})
        return await self.receipt(request.mutation_id, request, {"candidate_shadow": value, "proposal": proposal})


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
        if row.payload["request"].get("package_ref") is not None:
            # §7.4: a package approval additionally binds the candidate side
            # (frozen package/render hashes, versions, last evaluation and the
            # latest prospective candidate shadow). The offline shadow stays
            # engineering evidence only - it is shown in the shadows collection
            # above and never enters this binding. Config-only proposals keep
            # their exact payload shape.
            payload["candidate_binding"] = await self._candidate_binding(row, hashes)
        return {**payload, "evidence_hash": fingerprint(payload), "approval_summary": row.payload["request"]["summary"]}

    async def _candidate_binding(self, row: MethodProposal, hashes: dict[str, str]) -> dict[str, Any]:
        """§7.4 candidate-side fields one package-proposal approval binds (card 16).

        The evidence fingerprint covers the proposal-frozen package content
        hash, the frozen rendered-method hash and the renderer/validator
        versions, the last evaluation and the latest prospective
        ``candidate_shadow`` artifact by exact id + fingerprint - so any newer
        shadow registration fences a stale approval with ``stale_revision``.
        """
        from investment_agent.agent.method_receipt import RENDERER_VERSION, VALIDATOR_V1
        request = row.payload["request"]
        evaluations, shadows = row.payload["evaluation_ids"], row.payload.get("candidate_shadow_ids", [])
        binding: dict[str, Any] = {
            "package_ref": request["package_ref"],
            "content_hash": request["content_hash"],
            "rendered_method_hash": request["rendered_method_hash"],
            "renderer_version": RENDERER_VERSION,
            "validator_version": VALIDATOR_V1,
            "evaluation_id": evaluations[-1] if evaluations else None,
            "evaluation_hash": hashes.get(evaluations[-1]) if evaluations else None,
            "candidate_shadow_id": shadows[-1] if shadows else None,
            "candidate_shadow_hash": None,
        }
        if shadows:
            binding["candidate_shadow_hash"] = fingerprint(await self.artifact(UUID(shadows[-1]), "candidate_shadow"))
        return binding

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

    async def move_default_head(self, channel: str, expected: int, binding: MethodBinding) -> int:
        head = await self.session.get(MethodDefaultHead, (self.owner, channel), populate_existing=True)
        if (head.revision if head else 0) != expected:
            raise InvestorError("stale_revision", "全局默认方法已变化")
        if head:
            changed = await self.session.execute(update(MethodDefaultHead).where(MethodDefaultHead.owner_id == self.owner, MethodDefaultHead.channel == channel, MethodDefaultHead.revision == expected).values(revision=expected + 1, payload=binding.model_dump(mode="json")))
            if changed.rowcount != 1:  # type: ignore[attr-defined]
                raise InvestorError("stale_revision", "全局默认方法已变化")
        else:
            self.session.add(MethodDefaultHead(owner_id=self.owner, channel=channel, revision=1, payload=binding.model_dump(mode="json")))
        return expected + 1 if head else 1

    @method_transaction
    async def decide(self, request: MethodDecision, *, actor: str, base: "Settings | None" = None) -> dict[str, Any]:
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
            package_ref = row.payload["request"].get("package_ref")
            evaluations = evidence["evaluations"]
            if row.payload["status"] != "shadow" or not evaluations or not evaluations[-1]["passed"]:
                raise InvestorError("invalid_reference", "需要完整通过的失败样本、反例与隔离影子运行")
            if package_ref is None:
                # Config-only approvals keep the exact existing prospective gate.
                prospective = evidence["prospective"]
                if not prospective or not prospective[-1]["passed"] or prospective[-1]["evaluation_id"] != evaluations[-1]["id"]:
                    raise InvestorError("invalid_reference", "需要完整通过的失败样本、反例与隔离影子运行")
            else:
                # §7.3/§7.4 (card 16): package approvals require the latest
                # prospective candidate shadow to cover every
                # requires_candidate_shadow id plus one rejected expected-block
                # candidate - never the config-only observation path. Card 19
                # routes the matched-positive minimum through the caller's
                # Settings; without one the card-16 default (1) applies.
                min_positives = base.method_candidate_shadow_min_positives if base is not None else 1
                await self._require_candidate_shadow(row, min_positives=min_positives)
            _, current = await self.current(request.listing_ref)
            if current.method_ref != ObjectRef.model_validate(row.payload["request"]["old_method"]):
                raise InvestorError("stale_revision", "当前方法已改变，需要重新评估")
            method_id = uuid4()
            if package_ref is None:
                binding = MethodBinding(method_ref=ObjectRef(kind="method", id=method_id, revision=1), listing_ref=request.listing_ref, config=MethodConfig.model_validate(row.payload["request"]["config"]))
            else:
                # §7.4: the approved method version is a v2 binding on the
                # proposal-frozen package artifact (never re-read from YAML);
                # the head moves per-listing only, MethodDefaultHead is left to
                # the explicit activate-global operation (§5).
                binding = MethodBinding(schema_version=2, method_ref=ObjectRef(kind="method", id=method_id, revision=1), listing_ref=request.listing_ref, config=MethodConfig.model_validate(row.payload["request"]["config"]), package_ref=ObjectRef.model_validate(package_ref), package_hash=row.payload["request"]["content_hash"])
            self.session.add(MethodArtifact(id=str(method_id), owner_id=self.owner, kind="version", payload={"id": str(method_id), "created_at": datetime.now(UTC).isoformat(), "binding": binding.model_dump(mode="json"), "proposal_id": row.id, "evidence_hash": request.evidence_hash, "actor": actor}))
            active_proposals = (await self.session.scalars(select(MethodProposal).where(MethodProposal.owner_id == self.owner))).all()
            for active in active_proposals:
                if active.id != row.id and active.payload.get("method_ref") == current.method_ref.model_dump(mode="json") and active.payload["status"] == "active":
                    await self.revise(active, active.revision, {"status": "retired"})
            await self.move_head(request.listing_ref, request.expected_head_revision, binding)
            changes.update(status="active", method_ref=binding.method_ref.model_dump(mode="json"))
        proposal = await self.revise(row, request.expected_revision, changes)
        event: dict[str, Any] = {"operation": request.decision, "proposal_id": row.id, "proposal": proposal, "frozen_evidence": evidence, "request": request.model_dump(mode="json")}
        if request.decision == "approve" and row.payload["request"].get("package_ref") is not None:
            # §7.4: the decision event freezes the candidate hashes and the
            # exact evaluation/shadow references the approval was bound to.
            bound = evidence["candidate_binding"]
            event["candidate"] = {"package_hash": bound["content_hash"], "rendered_method_hash": bound["rendered_method_hash"], "renderer_version": bound["renderer_version"], "validator_version": bound["validator_version"], "evaluation_id": bound["evaluation_id"], "candidate_shadow_id": bound["candidate_shadow_id"]}
        self.save("event", event)
        return await self.receipt(request.mutation_id, request, proposal)

    async def _require_candidate_shadow(self, row: MethodProposal, *, min_positives: int = 1) -> None:
        """§7.3/§7.4 approval coverage gate for package proposals (card 16).

        The latest registered candidate shadow must be a matched positive run
        whose frozen requirement ids cover every ``requires_candidate_shadow``
        item, and at least one expected-block candidate task must have been
        rejected by the package-level validation. Card 19 adds the operations
        default ``min_positives``: that many matched positive runs (each
        expected="pass", matched, outcome="passed") must be registered before
        an approval; the coverage rule itself is never relaxed. Every refusal
        names the exact open requirements or counts.
        """
        shadows = row.payload.get("candidate_shadow_ids", [])
        required = sorted({str(item) for item in row.payload.get("requires_candidate_shadow", [])})
        if not shadows:
            raise InvestorError("invalid_reference", "批准前必须已有覆盖待影子要求的前瞻影子；缺口：" + (", ".join(required) or "全部待影子要求"))
        records = [await self.artifact(UUID(identifier), "candidate_shadow") for identifier in shadows]
        latest = records[-1]
        covered = {str(item) for item in latest["requirement_ids"]} if latest["expected"] == "pass" and latest["matched"] and latest["outcome"] == "passed" else set[str]()
        if missing := sorted(set(required) - covered):
            raise InvestorError("invalid_reference", f"最新前瞻影子未覆盖全部待影子要求：{', '.join(missing)}")
        if not any(record["expected"] == "block" and record["matched"] for record in records):
            raise InvestorError("invalid_reference", "批准前缺少一个被包级校验拒绝的预期失败候选影子")
        positives = [record for record in records if record["expected"] == "pass" and record["matched"] and record["outcome"] == "passed"]
        if len(positives) < min_positives:
            raise InvestorError("invalid_reference", f"批准前需要 {min_positives} 个匹配成功的正例前瞻影子，已有 {len(positives)} 个")

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

    @method_transaction
    async def activate_global(self, request: ActivateGlobal, *, actor: str) -> dict[str, Any]:
        """Explicit user activation of the global default head; never moves per-listing heads."""
        from investment_agent.investor.method_packages import MethodPackage
        if actor != "authenticated_user":
            raise InvestorError("invalid_reference", "模型不能启用全局方法")
        if result := await self.replay(request.mutation_id, request):
            return result
        if request.channel != "company-research":
            raise InvestorError("invalid_reference", "首版全局默认仅支持 company-research 通道")
        definition = MethodPackage.model_validate(await self.package_by_ref(request.package_ref))
        if not {"company_research", "review"} <= set(definition.applicable_kinds):
            raise InvestorError("invalid_reference", "首版通道要求包同时适用于 company_research 与 review")
        method_id = uuid4()
        binding = MethodBinding(
            schema_version=2,
            method_ref=ObjectRef(kind="method", id=method_id, revision=1),
            config=definition.discipline,
            listing_ref=None,
            package_ref=ObjectRef(kind="package", id=definition.artifact_id, revision=definition.version),
            package_hash=definition.content_hash,
        )
        value = {"id": str(method_id), "created_at": datetime.now(UTC).isoformat(), "binding": binding.model_dump(mode="json"), "package_hash": definition.content_hash, "reason": request.reason, "actor": actor}
        self.session.add(MethodArtifact(id=str(method_id), owner_id=self.owner, kind="version", payload=value))
        head_revision = await self.move_default_head(request.channel, request.expected_revision, binding)
        self.save("event", {"operation": "activate_global", "version": value, "request": request.model_dump(mode="json"), "package_hash": definition.content_hash})
        return await self.receipt(request.mutation_id, request, {**value, "channel": request.channel, "head_revision": head_revision})



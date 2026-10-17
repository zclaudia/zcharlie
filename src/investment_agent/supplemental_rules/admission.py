"""Atomic P2 command receipts and frozen evaluation admission."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.investor import InvestorMutation
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.pipeline_run import (
    PipelineRunRepository,
)
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.canonical import mutation_request_hash
from investment_agent.supplemental_rules.contracts import (
    EvaluationAdmission,
    EvaluationRequest,
    MutationReceipt,
    UpdateRecoveryCommand,
)
from investment_agent.supplemental_rules.service import (
    SupplementalRuleService,
    validate_manifest,
)

SUPPLEMENTAL_RULE_EVALUATE = "supplemental_rule_evaluate"
P2_RESULT_SCHEMA_VERSION = 1


def receipt_result(
    *, receipt: MutationReceipt, result: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the discriminated P2 mutation envelope stored in InvestorMutation."""
    return {
        "p2_schema_version": P2_RESULT_SCHEMA_VERSION,
        "receipt": receipt.model_dump(mode="json"),
        "result": result or {},
    }


async def reserve_and_replay(
    session: AsyncSession,
    *,
    owner_id: OwnerId,
    operation_id: UUID,
    command: str,
    target_ref: ObjectRef | None,
    body: Any,
) -> tuple[str, dict[str, Any] | None]:
    """Reserve SQLite's writer, then look up a matching P2 receipt.

    This must be the first database operation in every P2 mutation. Callers
    check the runtime write switch and current CAS only after this returns no
    saved result.
    """
    await SupplementalRulesRepository(session).reserve_registry(owner_id)
    request_hash = mutation_request_hash(command, str(target_ref.id) if target_ref else None, body)
    prior = await session.get(InvestorMutation, (owner_id, str(operation_id)))
    if prior is None:
        return request_hash, None
    if prior.request_hash != request_hash:
        raise InvestorError("idempotency_conflict", "操作标识已用于不同命令、目标或正文")
    result = prior.result
    try:
        if result.get("p2_schema_version") != P2_RESULT_SCHEMA_VERSION:
            raise ValueError("not a P2 receipt")
        receipt = MutationReceipt.model_validate(result["receipt"])
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise InvestorError("idempotency_conflict", "操作标识已有不同类型的收据") from exc
    if (
        receipt.owner_id != owner_id
        or receipt.operation_id != operation_id
        or receipt.command != command
        or receipt.target_ref != target_ref
    ):
        raise InvestorError("idempotency_conflict", "操作收据的命令或目标不匹配")
    return request_hash, result


async def require_enabled(session: AsyncSession, base_settings: Settings) -> Settings:
    """Resolve the runtime flag after receipt replay has been checked."""
    from investment_agent.settings.loader import load_settings

    effective = await load_settings(session, base=base_settings)
    if not effective.supplemental_rules_enabled:
        raise InvestorError("writes_disabled", "补充事项写入与评估已关闭")
    return effective


def save_receipt(
    session: AsyncSession,
    *,
    owner_id: OwnerId,
    operation_id: UUID,
    request_hash: str,
    receipt: MutationReceipt,
    result: dict[str, Any] | None = None,
) -> None:
    """Stage the P2 result in the shared globally keyed mutation table."""
    session.add(
        InvestorMutation(
            owner_id=owner_id,
            key=str(operation_id),
            request_hash=request_hash,
            result=receipt_result(receipt=receipt, result=result),
        )
    )


class SupplementalRuleAdmission:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        owner_id: OwnerId = "local_owner",
    ) -> None:
        self.session = session
        self.settings = settings
        self.owner_id = owner_id

    async def accept_evaluation(self, request: EvaluationRequest) -> EvaluationAdmission:
        operation_id = request.idempotency_key
        command = "supplemental_rule.evaluate.v1"
        request_hash, prior = await reserve_and_replay(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=None,
            body=request,
        )
        if prior is not None:
            try:
                admission = EvaluationAdmission.model_validate(prior["result"]["admission"])
                receipt = MutationReceipt.model_validate(prior["receipt"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvestorError("idempotency_conflict", "评估收据类型不匹配") from exc
            if (
                receipt.result_kind != "evaluation_admission"
                or receipt.run_ref != admission.run_ref
                or receipt.operation_id != operation_id
            ):
                raise InvestorError("idempotency_conflict", "评估收据与原运行不匹配")
            return admission

        await require_enabled(self.session, self.settings)
        accepted_at = datetime.now(UTC)
        if request.mode == "historical_replay":
            manifest = await _authorized_replay_manifest(
                self.session, request, owner_id=self.owner_id
            )
        else:
            # Import locally to keep the transaction protocol independent from
            # evaluation implementation and avoid a jobs/service dependency cycle.
            manifest = await SupplementalRuleService(self.session, self.owner_id).freeze(
                request, as_of=accepted_at
            )
        manifest = validate_manifest(manifest)
        manifest_hash = manifest.manifest_hash
        run_ref, source_manifest_hash = await enqueue_manifest(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=None,
            command_body=request,
            request_hash=request_hash,
            manifest=manifest,
            accepted_at=accepted_at,
            evaluation_purpose=request.evaluation_purpose,
            trigger="supplemental_rules_api",
            mode=request.mode,
        )
        receipt = MutationReceipt(
            owner_id=self.owner_id,
            operation_id=operation_id,
            result_kind="evaluation_admission",
            command=command,
            accepted_at=accepted_at,
            run_ref=run_ref,
            source_manifest_hash=source_manifest_hash,
        )
        admission = EvaluationAdmission(
            operation_id=operation_id,
            run_ref=run_ref,
            manifest_hash=manifest_hash,
            accepted_at=accepted_at,
        )
        save_receipt(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            request_hash=request_hash,
            receipt=receipt,
            result={"admission": admission.model_dump(mode="json")},
        )
        await self.session.flush()
        return admission

    async def accept_update_recovery(
        self, request: UpdateRecoveryCommand
    ) -> EvaluationAdmission:
        """Admit only checkpoint-pending pairs from an immutable P4-linked P2 run."""
        command = "supplemental_rule.update_recovery.v1"
        operation_id = request.idempotency_key
        request_hash, prior = await reserve_and_replay(
            self.session, owner_id=self.owner_id, operation_id=operation_id,
            command=command, target_ref=None, body=request,
        )
        if prior is not None:
            try:
                admission = EvaluationAdmission.model_validate(prior["result"]["admission"])
                receipt = MutationReceipt.model_validate(prior["receipt"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvestorError("idempotency_conflict", "恢复收据类型不匹配") from exc
            if receipt.result_kind != "evaluation_admission" or receipt.run_ref != admission.run_ref:
                raise InvestorError("idempotency_conflict", "恢复收据与原运行不匹配")
            return admission

        await require_enabled(self.session, self.settings)
        original_run, original, original_member = await _authorize_recovery_source(
            self.session, request, owner_id=self.owner_id,
        )
        from investment_agent.supplemental_rules.jobs import _read_checkpoint, _verify_checkpoint

        checkpoint = _read_checkpoint(original_run.metadata_json, original.manifest)
        await _verify_checkpoint(self.session, checkpoint, original)
        selected = _recovery_pairs(
            request, original.manifest, checkpoint,
            listing_id=original_member.listing_id, context=original_member.context,
        )
        manifest = _recovery_manifest(original.manifest, selected)
        manifest = validate_manifest(manifest)
        accepted_at = datetime.now(UTC)
        run_ref, source_manifest_hash = await enqueue_manifest(
            self.session, owner_id=self.owner_id, operation_id=operation_id,
            command=command, target_ref=None, command_body=request,
            request_hash=request_hash, manifest=manifest, accepted_at=accepted_at,
            evaluation_purpose="coverage", trigger="supplemental_update_recovery",
            mode="live",
        )
        receipt = MutationReceipt(
            owner_id=self.owner_id, operation_id=operation_id,
            result_kind="evaluation_admission", command=command,
            accepted_at=accepted_at, run_ref=run_ref,
            source_manifest_hash=source_manifest_hash,
        )
        admission = EvaluationAdmission(
            operation_id=operation_id, run_ref=run_ref,
            manifest_hash=manifest.manifest_hash, accepted_at=accepted_at,
        )
        save_receipt(self.session, owner_id=self.owner_id, operation_id=operation_id,
                     request_hash=request_hash, receipt=receipt,
                     result={"admission": admission.model_dump(mode="json")})
        await self.session.flush()
        from investment_agent.storage.models.analysis import PipelineRun

        new_run = await self.session.get(PipelineRun, int(run_ref.id))
        if new_run is None:
            raise InvestorError("not_found", "恢复运行未能持久化")
        await _append_recovery_admission_sidecar(
            self.session, request, owner_id=self.owner_id, run_id=new_run.id,
            manifest_hash=manifest.manifest_hash,
        )
        return admission


async def enqueue_manifest(
    session: AsyncSession,
    *,
    owner_id: OwnerId,
    operation_id: UUID,
    command: str,
    target_ref: ObjectRef | None,
    command_body: Any,
    request_hash: str,
    accepted_at: datetime,
    evaluation_purpose: str = "coverage",
    origin_ref: ObjectRef | None = None,
    trigger: str = "supplemental_rules_mutation",
    manifest: Any | None = None,
    source: Any | None = None,
    mode: str = "live",
) -> tuple[ObjectRef, str]:
    """Freeze and enqueue a bounded evaluation inside the caller's transaction.

    Lifecycle and event services call this only after staging their head or
    event/generation writes, so the manifest sees the post-change snapshot.
    """
    if manifest is None:
        if source is None:
            raise ValueError("a reassessment source or frozen manifest is required")
        manifest = await SupplementalRuleService(session, owner_id).freeze_reassessment(
            source, operation_id=operation_id, as_of=accepted_at
        )
    manifest = validate_manifest(manifest)
    envelope = {
        "mode": mode,
        "evaluation_purpose": evaluation_purpose,
        "origin_ref": origin_ref.model_dump(mode="json") if origin_ref else None,
    }
    run_payload: dict[str, Any] = {
        "schema_version": 1,
        "owner_id": owner_id,
        "operation_id": str(operation_id),
        "command": command,
        "command_target": target_ref.model_dump(mode="json") if target_ref else None,
        "command_body": command_body.model_dump(mode="json")
        if hasattr(command_body, "model_dump")
        else command_body,
        "request_hash": request_hash,
        "accepted_at": accepted_at.isoformat(),
        "source_manifest": manifest.model_dump(mode="json"),
        "source_manifest_hash": manifest.manifest_hash,
        "execution_envelope": envelope,
    }
    run, created = await PipelineRunRepository(session).enqueue(
        kind=SUPPLEMENTAL_RULE_EVALUATE,
        idempotency_key=f"{SUPPLEMENTAL_RULE_EVALUATE}:{owner_id}:{operation_id}",
        triggered_by=trigger,
        request_json=run_payload,
    )
    if not created:
        raise InvestorError("idempotency_conflict", "运行已存在但缺少原子接收收据")
    return ObjectRef(kind="pipeline_run", id=run.id, revision=1), manifest.manifest_hash


async def _authorize_recovery_source(
    session: AsyncSession, request: UpdateRecoveryCommand, *, owner_id: OwnerId,
) -> tuple[Any, Any, Any]:
    from investment_agent.supplemental_rules.jobs import authorize_supplemental_run

    run, authorized = await authorize_supplemental_run(
        session, request.original_run_id, owner_id=owner_id,
    )
    if run.status not in {"failed", "partial", "canceled"}:
        raise InvestorError("invalid_reference", "原评估尚未进入可恢复终态")
    if authorized.command not in {
        "supplemental_rule.evaluate.v1", "supplemental_rule.update_recovery.v1",
    } or authorized.mode != "live":
        raise InvestorError("invalid_reference", "恢复只能接续原始live评估")
    if authorized.manifest.manifest_hash != request.original_manifest_hash:
        raise InvestorError("invalid_reference", "原始冻结清单摘要不匹配")
    if authorized.command == "supplemental_rule.evaluate.v1":
        expected_generation = 1
    else:
        parent_body = UpdateRecoveryCommand.model_validate(run.request_json.get("command_body"))
        expected_generation = parent_body.recovery_generation + 1
    if request.recovery_generation != expected_generation or expected_generation > 16:
        raise InvestorError("invalid_reference", "恢复代次超出受限授权链")
    sidecar = (await session.execute(select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == owner_id,
        SupplementalChangeReceiptRow.kind == "evaluation_admission",
        SupplementalChangeReceiptRow.origin_run_id == run.id,
        SupplementalChangeReceiptRow.source_manifest_hash == authorized.manifest.manifest_hash,
    ))).scalar_one_or_none()
    if sidecar is None:
        raise InvestorError("invalid_reference", "原评估缺少P4不可变授权")
    sequences = sidecar.payload.get("scope_member_sequences")
    if not isinstance(sequences, list) or request.member_sequence not in sequences:
        raise InvestorError("invalid_reference", "恢复成员不属于原始P4授权")
    from investment_agent.storage.repositories.supplemental_updates import (
        SupplementalUpdatesRepository,
    )

    repo = SupplementalUpdatesRepository(session)
    source = await repo.get_change(owner_id, request.source_sequence)
    member = await repo.get_change(owner_id, request.member_sequence)
    if (
        source is None or source.kind not in {
            "rule_revision", "rule_lifecycle", "research_event", "evidence_revision",
            "material_revision", "due_boundary", "bootstrap", "source_attempt",
        }
        or member is None or member.kind != "scope_member"
        or member.payload.get("source_sequence") != source.sequence
        or member.payload.get("source_id") != str(source.id)
        or member.payload.get("source_hash") != source.payload_hash
        or member.listing_id is None or member.context is None
        or source.sequence != sidecar.parent_sequence
        or (member.listing_id, member.context) not in {
            (int(ref.id), context)
            for ref in authorized.manifest.listing_refs
            for context in authorized.manifest.contexts
        }
    ):
        raise InvestorError("invalid_reference", "恢复成员与原P4来源不匹配")
    return run, authorized, member


def _recovery_pairs(
    request: UpdateRecoveryCommand, manifest: Any, checkpoint: Any, *,
    listing_id: int | None, context: str | None,
) -> list[Any]:
    if len(manifest.pairs) != len(checkpoint.members):
        raise InvestorError("invalid_reference", "原检查点与冻结成员数不匹配")
    if listing_id is None or context is None:
        raise InvestorError("invalid_reference", "没有待恢复的规则序列")
    scope = (listing_id, context)
    pending: list[Any] = []
    saved_refs: list[ObjectRef] = []
    for pair, item in zip(manifest.pairs, checkpoint.members, strict=True):
        pair_scope = (int(pair.listing_ref.id), pair.context)
        if pair_scope == scope and item.state == "pending":
            pending.append(pair)
        elif pair_scope == scope and item.state == "saved":
            assert item.application_ref is not None
            saved_refs.append(item.application_ref)
    if not pending:
        raise InvestorError("invalid_reference", "没有待恢复的规则序列")
    if tuple(pair.ordinal for pair in pending) != request.pending_ordinals:
        raise InvestorError("invalid_reference", "恢复序列必须精确匹配原检查点的全部待处理成员")
    if tuple(saved_refs) != request.saved_application_refs:
        raise InvestorError("invalid_reference", "恢复请求未精确绑定已保存的规则序列")
    return pending


def _recovery_manifest(original: Any, pending: list[Any]) -> Any:
    from investment_agent.supplemental_rules.canonical import canonical_hash
    from investment_agent.supplemental_rules.contracts import FrozenManifest
    from investment_agent.supplemental_rules.service import manifest_digest, scope_digest

    if not pending:
        raise InvestorError("invalid_reference", "恢复清单不能为空")
    listing_ref = pending[0].listing_ref
    context = pending[0].context
    pairs = []
    for ordinal, pair in enumerate(pending):
        if pair.listing_ref != listing_ref or pair.context != context:
            raise InvestorError("invalid_reference", "一次恢复只能包含一个精确scope")
        reindexed = pair.model_copy(update={"ordinal": ordinal, "input_hash": "0" * 64})
        pairs.append(reindexed.model_copy(update={
            "input_hash": canonical_hash(reindexed.model_dump(mode="python", exclude={"input_hash"}))
        }))
    manifest = FrozenManifest(
        owner_id=original.owner_id, listing_refs=(listing_ref,), contexts=frozenset({context}),
        as_of=original.as_of, evaluated_at=original.evaluated_at,
        rules_generation=original.rules_generation, events_generation=original.events_generation,
        matcher_version=original.matcher_version, evaluator_version=original.evaluator_version,
        pairs=tuple(pairs), unbound_events=original.unbound_events,
        scan_diagnostics=original.scan_diagnostics,
        scope_semantic_hash="0" * 64, input_hash="0" * 64, manifest_hash="0" * 64,
    )
    semantic = canonical_hash([scope_digest(manifest, listing_ref, context)])
    manifest = manifest.model_copy(update={"scope_semantic_hash": semantic})
    digest = manifest_digest(manifest)
    return manifest.model_copy(update={"input_hash": digest, "manifest_hash": digest})


async def _append_recovery_admission_sidecar(
    session: AsyncSession, request: UpdateRecoveryCommand, *, owner_id: OwnerId, run_id: int,
    manifest_hash: str,
) -> None:
    from investment_agent.storage.repositories.supplemental_updates import (
        SupplementalUpdatesRepository,
    )

    repo = SupplementalUpdatesRepository(session)
    source = await repo.get_change(owner_id, request.source_sequence)
    if source is None:
        raise InvestorError("not_found", "恢复来源收据不存在")
    await repo.append_change(
        owner_id,
        change_key=f"p4:recovery-admission:{request.idempotency_key}",
        kind="evaluation_admission",
        parent_sequence=request.source_sequence,
        origin_run_id=run_id,
        source_manifest_hash=manifest_hash,
        payload={"receipt_sequence": request.source_sequence,
                 "scope_member_sequences": [request.member_sequence],
                 "run_id": run_id, "source_manifest_hash": manifest_hash,
                 "recovery_of_run_id": request.original_run_id,
                 "recovery_of_manifest_hash": request.original_manifest_hash,
                 "recovered_member_sequence": request.member_sequence,
                 "pending_ordinals": list(request.pending_ordinals),
                 "recovery_generation": request.recovery_generation,
                 "has_manual_pair": request.has_manual_pair},
    )


async def _authorized_replay_manifest(
    session: AsyncSession,
    request: EvaluationRequest,
    *,
    owner_id: OwnerId,
) -> Any:
    """Authorize replay solely from an original live evaluation receipt."""
    from investment_agent.supplemental_rules.jobs import authorize_supplemental_run

    assert request.replay_run_ref is not None
    run, original = await authorize_supplemental_run(
        session, int(request.replay_run_ref.id), owner_id=owner_id
    )
    if request.replay_run_ref.revision != 1:
        raise InvestorError("invalid_reference", "历史评估必须指定运行版本1")
    if original.command != "supplemental_rule.evaluate.v1":
        raise InvestorError("historical_manifest_unavailable", "仅支持重放原始用户评估运行")
    try:
        original_request = EvaluationRequest.model_validate(run.request_json["command_body"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvestorError("historical_manifest_unavailable", "原始评估请求不可验证") from exc
    if original_request.mode != "live":
        raise InvestorError("historical_manifest_unavailable", "不能递归重放历史重放运行")
    if (
        request.listing_refs != original_request.listing_refs
        or request.contexts != original_request.contexts
        or request.evaluation_purpose != original_request.evaluation_purpose
        or request.exact_rule_refs != original_request.exact_rule_refs
        or request.prior_application_refs != original_request.prior_application_refs
    ):
        raise InvestorError("historical_manifest_unavailable", "重放范围或用途与原始请求不匹配")
    if request.input_as_of != original.manifest.as_of:
        raise InvestorError("historical_manifest_unavailable", "重放时点与原始冻结清单不匹配")
    if request.replay_manifest_hash != original.manifest.manifest_hash:
        raise InvestorError("historical_manifest_unavailable", "重放清单摘要与原始冻结清单不匹配")
    return original.manifest

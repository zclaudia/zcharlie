"""Atomic P2 command receipts and frozen evaluation admission."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.investor import InvestorMutation
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

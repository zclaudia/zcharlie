"""Application service for durable local supplemental-update maintenance."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.settings.loader import load_settings
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.supplemental_updates import (
    SupplementalUpdatesRepository,
    update_digest,
)
from investment_agent.supplemental_rules.update_jobs import (
    enqueue_pending_updates,
    request_update_maintenance,
    settle_update_handoffs,
    validate_review_batch,
)

__all__ = [
    "SupplementalUpdatesService",
    "enqueue_pending_updates",
    "settle_update_handoffs",
    "validate_review_batch",
]


class SupplementalUpdatesService:
    """Small transaction-scoped facade used by scheduler and operations API."""

    def __init__(
        self, session: AsyncSession, settings: Settings, owner_id: OwnerId = "local_owner",
    ) -> None:
        self.session = session
        self.settings = settings
        self.owner_id = owner_id
        self.repo = SupplementalUpdatesRepository(session)

    async def sweep(self, *, limit: int = 50) -> dict[str, int]:
        created = await request_update_maintenance(
            self.session, settings=self.settings, owner_id=self.owner_id,
        )
        return {"queued": int(created), "settled": 0}

    async def resume(
        self, *, run_id: int, expected_revision: int, reason: str, operation_id: UUID,
    ) -> dict[str, Any]:
        return await _resume(
            self.session, owner_id=self.owner_id, settings=self.settings,
            run_id=run_id, expected_revision=expected_revision,
            reason=reason, operation_id=operation_id,
        )


async def resume(
    session: AsyncSession, *, run_id: int, expected_revision: int,
    reason: str, operation_id: UUID, settings: Settings,
) -> dict[str, Any]:
    """API entry point; replay precedes current gates and revision checks."""
    return await _resume(
        session, owner_id="local_owner", settings=settings,
        run_id=run_id, expected_revision=expected_revision,
        reason=reason, operation_id=operation_id,
    )


async def _resume(
    session: AsyncSession, *, owner_id: OwnerId, settings: Settings,
    run_id: int, expected_revision: int, reason: str, operation_id: UUID,
) -> dict[str, Any]:
    """Create a new coordinator generation for only untouched P2 scopes."""
    repo = SupplementalUpdatesRepository(session)
    await repo.reserve(owner_id)
    request_body = {
        "run_id": run_id, "expected_revision": expected_revision,
        "reason": reason, "operation_id": str(operation_id),
    }
    request_hash = update_digest({
        "method": "POST", "path": f"/supplemental-updates/batches/{run_id}/resume",
        "body": request_body,
    })
    prior = (await session.execute(select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == owner_id,
        SupplementalChangeReceiptRow.operation_id == str(operation_id),
    ))).scalar_one_or_none()
    if prior is not None:
        if prior.kind != "source_attempt" or prior.request_hash != request_hash:
            raise InvestorError("idempotency_conflict", "恢复操作标识已用于不同请求")
        prior_run_id = prior.payload.get("prior_run_id")
        expected = prior.payload.get("expected_revision")
        if not isinstance(prior_run_id, int) or not isinstance(expected, int):
            raise InvestorError("idempotency_conflict", "恢复收据不完整")
        replayed = await PipelineRunRepository(session).by_idempotency_key(
            kind="supplemental_update_process",
            idempotency_key=f"p4:r:{prior_run_id}:{expected + 1}",
        )
        if replayed is None:
            raise InvestorError("idempotency_conflict", "恢复代次收据与运行不匹配")
        return {"run_id": replayed.id, "replayed": True}

    effective = await load_settings(session, base=settings)
    if not effective.supplemental_rules_enabled or not effective.supplemental_updates_enabled:
        raise InvestorError("writes_disabled", "补充事项本地更新已关闭")
    run = (await session.execute(select(PipelineRun).where(
        PipelineRun.id == run_id,
    ).with_for_update())).scalar_one_or_none()
    if run is None or run.kind not in {
        "supplemental_update_process", "supplemental_rule_evaluate",
    }:
        raise InvestorError("not_found", "补充更新运行不存在")
    if run.kind == "supplemental_update_process" and run.request_json.get("owner_id") != owner_id:
        raise InvestorError("not_found", "补充更新运行不存在")
    if run.status not in {"partial", "failed"}:
        raise InvestorError("invalid_reference", "仅可恢复已终结的部分或失败运行")
    raw_revision = run.metadata_json.get("p4_resume_revision", 0)
    if not isinstance(raw_revision, int):
        raise InvestorError("revision_conflict", "恢复请求版本无效")
    revision = raw_revision
    if revision != expected_revision:
        raise InvestorError("revision_conflict", "恢复请求版本已变化")
    next_revision = revision + 1

    request: dict[str, Any]
    resume_scopes: list[dict[str, Any]] | None = None
    source_sequence: int
    if run.kind == "supplemental_update_process":
        # Retry uses the same frozen request/offset. Member and P2 idempotency
        # keys converge to the already admitted exact pairs; terminal P2
        # executions are never reopened here.
        if run.metadata_json.get("supplemental_update_state") not in {
            None, "capacity_blocked", "paused",
        }:
            raise InvestorError("invalid_reference", "协调批次状态不支持恢复")
        request = dict(run.request_json)
        raw_sequence = request.get("receipt_sequence")
        if not isinstance(raw_sequence, int):
            raise InvestorError("not_found", "补充更新来源收据不存在")
        source_sequence = raw_sequence
    else:
        from investment_agent.supplemental_rules.jobs import (
            _read_checkpoint,
            _verify_checkpoint,
            authorize_supplemental_run,
        )

        try:
            _, authorized = await authorize_supplemental_run(
                session, run.id, owner_id=owner_id,
            )
        except Exception as exc:
            raise InvestorError("invalid_reference", "该P2运行没有P4更新授权") from exc
        if authorized.supplemental_update_sequence is None:
            raise InvestorError("invalid_reference", "该P2运行没有P4更新授权")
        source_sequence = authorized.supplemental_update_sequence
        source_receipt = await repo.get_change(owner_id, source_sequence)
        if source_receipt is None:
            raise InvestorError("not_found", "P4来源收据不存在")

        try:
            checkpoint = _read_checkpoint(run.metadata_json, authorized.manifest)
            await _verify_checkpoint(session, checkpoint, authorized)
        except Exception as exc:
            raise InvestorError("invalid_reference", "P2运行结果无法安全恢复") from exc
        admission_row = (await session.execute(select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.owner_id == owner_id,
            SupplementalChangeReceiptRow.kind == "evaluation_admission",
            SupplementalChangeReceiptRow.origin_run_id == run.id,
            SupplementalChangeReceiptRow.source_manifest_hash == authorized.manifest.manifest_hash,
        ))).scalar_one_or_none()
        raw_member_sequences = admission_row.payload.get("scope_member_sequences") if admission_row else None
        if not isinstance(raw_member_sequences, list) or not raw_member_sequences:
            raise InvestorError("invalid_reference", "P4更新成员收据不完整")
        member_rows = []
        outcomes = {}
        for sequence in raw_member_sequences:
            if not isinstance(sequence, int):
                raise InvestorError("invalid_reference", "P4更新成员收据无效")
            member = await repo.get_change(owner_id, sequence)
            if (
                member is None or member.kind != "scope_member"
                or member.payload.get("source_sequence") != source_sequence
            ):
                raise InvestorError("invalid_reference", "P4更新成员收据与来源不匹配")
            member_rows.append(member)
            outcome = await repo.get_latest_member_outcome(owner_id, member.sequence)
            if outcome is not None:
                outcomes[member.sequence] = outcome
        checkpoint_by_scope: dict[tuple[int, str], list[tuple[Any, Any]]] = {}
        for pair, checkpoint_member in zip(authorized.manifest.pairs, checkpoint.members, strict=True):
            checkpoint_by_scope.setdefault((int(pair.listing_ref.id), pair.context), []).append((pair, checkpoint_member))
        pending: list[dict[str, Any]] = []
        parent_command = run.request_json.get("command")
        inherited_manual = False
        if parent_command == "supplemental_rule.evaluate.v1":
            recovery_generation = 1
        elif parent_command == "supplemental_rule.update_recovery.v1":
            from investment_agent.supplemental_rules.contracts import UpdateRecoveryCommand

            parent_recovery = UpdateRecoveryCommand.model_validate(
                run.request_json.get("command_body")
            )
            recovery_generation = parent_recovery.recovery_generation + 1
            inherited_manual = parent_recovery.has_manual_pair
        else:
            raise InvestorError("invalid_reference", "P2运行命令不支持恢复")
        if recovery_generation > 16:
            raise InvestorError("invalid_reference", "恢复代次达到安全上限，需要人工处理")
        for scope_member in member_rows:
            if scope_member.listing_id is None or scope_member.context is None:
                continue
            prior_outcome = outcomes.get(scope_member.sequence)
            if prior_outcome is not None and prior_outcome.payload.get("state") == "completed":
                continue
            pairs = checkpoint_by_scope.get((scope_member.listing_id, scope_member.context), [])
            pending_pairs = [(pair, item) for pair, item in pairs if item.state == "pending"]
            if pending_pairs:
                pending.append({"listing_id": scope_member.listing_id, "context": scope_member.context,
                                "member_sequence": scope_member.sequence,
                                "original_run_id": run.id,
                                "original_manifest_hash": authorized.manifest.manifest_hash,
                                "recovery_generation": recovery_generation,
                                "pending_ordinals": [pair.ordinal for pair, _ in pending_pairs],
                                "exact_rule_refs": [pair.rule_ref.model_dump(mode="json") for pair, _ in pending_pairs],
                                "prior_application_refs": [
                                    pair.prior_live_application_ref.model_dump(mode="json")
                                    for pair, _ in pending_pairs
                                    if pair.prior_live_application_ref is not None
                                ],
                                "saved_application_refs": [
                                    item.application_ref.model_dump(mode="json")
                                    for _, item in pairs if item.state == "saved" and item.application_ref is not None
                                ],
                                "has_manual_pair": inherited_manual or any(
                                    item.state == "item_failed" for _, item in pairs
                                )})
            elif pairs and all(item.state == "saved" for _, item in pairs):
                await repo.append_change(
                    owner_id, change_key=f"p4:resume-outcome:{operation_id}:{scope_member.sequence}",
                    kind="scope_outcome", parent_sequence=source_sequence,
                    listing_id=scope_member.listing_id, context=scope_member.context,
                    origin_run_id=run.id, source_manifest_hash=authorized.manifest.manifest_hash,
                    payload={"member_sequence": scope_member.sequence, "run_id": run.id,
                             "state": "completed", "reason": "saved_result_verified"},
                )
            elif pairs:
                await repo.append_change(
                    owner_id, change_key=f"p4:resume-manual:{operation_id}:{scope_member.sequence}",
                    kind="scope_outcome", parent_sequence=source_sequence,
                    listing_id=scope_member.listing_id, context=scope_member.context,
                    origin_run_id=run.id, source_manifest_hash=authorized.manifest.manifest_hash,
                    payload={"member_sequence": scope_member.sequence, "run_id": run.id,
                             "state": "needs_manual", "reason": "checkpoint_pair_failed"},
                )
        if not pending:
            raise InvestorError("invalid_reference", "运行没有可安全承接的未完成scope成员")
        resume_scopes = pending
        request = {
            "schema_version": 1, "owner_id": owner_id,
            "receipt_sequence": source_sequence,
            "receipt_id": str(source_receipt.id),
            "receipt_payload_hash": source_receipt.payload_hash,
            "resume_scopes": resume_scopes,
            "resume_parent_sequence": None,
            "resume_operation_id": str(operation_id),
        }

    source_receipt = await repo.get_change(owner_id, source_sequence)
    if source_receipt is None:
        raise InvestorError("not_found", "补充更新来源收据不存在")
    run.metadata_json = {**run.metadata_json, "p4_resume_revision": next_revision}
    await session.flush()
    attempt = await repo.append_change(
        owner_id, change_key=f"p4:resume:{operation_id}",
        kind="source_attempt", operation_id=operation_id,
        request_hash=request_hash, parent_sequence=source_sequence,
        payload={"prior_run_id": run_id,
                 "expected_revision": expected_revision, "reason": reason,
                 "pending_scopes": resume_scopes},
    )
    if resume_scopes is not None:
        request["resume_parent_sequence"] = attempt.sequence
    key = f"p4:r:{run_id}:{next_revision}"
    new_run, created = await PipelineRunRepository(session).enqueue(
        kind="supplemental_update_process", idempotency_key=key,
        triggered_by="supplemental_updates_resume",
        request_json=request, max_attempts=3,
    )
    if not created:
        raise InvestorError("idempotency_conflict", "恢复代次已经存在")
    return {"run_id": new_run.id, "replayed": False,
            "pending_scope_count": len(resume_scopes) if resume_scopes is not None else None}

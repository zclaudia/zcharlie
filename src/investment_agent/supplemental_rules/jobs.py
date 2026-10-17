"""Leased worker and typed resumable checkpoints for P2 evaluation runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import literal_column

from investment_agent.config import Settings
from investment_agent.investor.common import Contract, ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.investor import InvestorMutation
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.investment_assessment import (
    _check_header_columns,
    _parse_assessment,
)
from investment_agent.storage.repositories.pipeline_run import (
    PipelineRunRepository,
)
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.admission import (
    P2_RESULT_SCHEMA_VERSION,
    SUPPLEMENTAL_RULE_EVALUATE,
    _authorize_recovery_source,
    _recovery_manifest,
    _recovery_pairs,
)
from investment_agent.supplemental_rules.canonical import (
    canonical_hash,
    mutation_request_hash,
    stable_series_key,
)
from investment_agent.supplemental_rules.commands import (
    ResearchEventCreateRequest,
    ResearchEventRevisionRequest,
    RuleCreateRequest,
    RuleDecisionRequest,
    RuleRevisionRequest,
    RuleValidationRequest,
)
from investment_agent.supplemental_rules.contracts import (
    EvaluationAdmission,
    EvaluationRequest,
    FrozenManifest,
    MutationReceipt,
    RuleApplication,
    UpdateRecoveryCommand,
)
from investment_agent.supplemental_rules.service import (
    SupplementalRuleService,
    validate_manifest,
)


class CheckpointMember(Contract):
    ordinal: int = Field(ge=0)
    state: Literal["pending", "saved", "item_failed"]
    application_ref: ObjectRef | None = None
    input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: Literal["item_binding_conflict"] | None = None

    @model_validator(mode="after")
    def validate_member(self) -> CheckpointMember:
        if self.state == "saved":
            if (
                self.application_ref is None
                or self.application_ref.kind != "rule_application"
                or self.input_hash is None
            ):
                raise ValueError("saved checkpoint member requires application identity and hash")
            if self.error_code is not None:
                raise ValueError("saved checkpoint member cannot carry an item error")
        elif self.application_ref is not None or self.input_hash is not None:
            raise ValueError("unsaved checkpoint member cannot contain an application")
        if self.state == "item_failed" and self.error_code is None:
            raise ValueError("failed checkpoint member requires a stable error code")
        if self.state == "pending" and self.error_code is not None:
            raise ValueError("pending checkpoint member cannot carry an item error")
        return self


class RunCheckpoint(Contract):
    schema_version: Literal[1] = 1
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    members: tuple[CheckpointMember, ...]
    updated_at: datetime


class AuthorizedSupplementalRun(Contract):
    run_ref: ObjectRef
    operation_id: UUID
    command: str
    target_ref: ObjectRef | None = None
    request_hash: str
    manifest: FrozenManifest
    mode: Literal["live", "historical_replay"] = "live"
    evaluation_purpose: Literal["coverage", "issue_review"]
    origin_ref: ObjectRef | None = None
    supplemental_update_sequence: int | None = Field(default=None, ge=1)
    accepted_at: datetime


def _command_model(command: str) -> type[Contract] | None:
    models: dict[str, type[Contract]] = {
        "supplemental_rule.evaluate.v1": EvaluationRequest,
        "supplemental_rule.update_recovery.v1": UpdateRecoveryCommand,
        "supplemental_rule.create.v1": RuleCreateRequest,
        "supplemental_rule.revision.v1": RuleRevisionRequest,
        "supplemental_rule.validate.v1": RuleValidationRequest,
        "research_event.create_user_report.v1": ResearchEventCreateRequest,
        "research_event.revise_user_report.v1": ResearchEventRevisionRequest,
    }
    if command.startswith("supplemental_rule.") and command.rsplit(".", 2)[-2] in {
        "approve",
        "suspend",
        "retire",
        "reject",
    }:
        return RuleDecisionRequest
    return models.get(command)


async def authorize_supplemental_run(
    session: AsyncSession,
    run_id: int,
    *,
    owner_id: OwnerId = "local_owner",
) -> tuple[PipelineRun, AuthorizedSupplementalRun]:
    """Validate owner, mutation receipt, command digest and frozen source data."""
    run = await session.get(PipelineRun, run_id, populate_existing=True)
    if run is None or run.kind != SUPPLEMENTAL_RULE_EVALUATE:
        raise InvestorError("not_found", "补充事项评估运行不存在")
    payload = run.request_json
    if payload.get("owner_id") != owner_id:
        raise InvestorError("not_found", "补充事项评估运行不存在")
    command = payload.get("command")
    operation_raw = payload.get("operation_id")
    if not isinstance(command, str) or not isinstance(operation_raw, str):
        raise InvestorError("not_found", "评估运行没有可验证的授权收据")
    model = _command_model(command)
    if model is None:
        raise InvestorError("not_found", "评估运行没有可验证的授权收据")
    try:
        operation_id = UUID(operation_raw)
        command_body: Any = model.model_validate(payload.get("command_body"))
        target_raw = payload.get("command_target")
        target = ObjectRef.model_validate(target_raw) if target_raw is not None else None
        request_hash = mutation_request_hash(
            command, str(target.id) if target else None, command_body
        )
        manifest = FrozenManifest.model_validate(payload.get("source_manifest"))
        validate_manifest(manifest)
        envelope = payload.get("execution_envelope")
        if not isinstance(envelope, dict) or envelope.get("mode") not in {
            "live",
            "historical_replay",
        }:
            raise ValueError("unsupported execution envelope")
        mode = envelope["mode"]
        purpose = envelope.get("evaluation_purpose")
        if purpose not in {"coverage", "issue_review"}:
            raise ValueError("invalid evaluation purpose")
        origin_raw = envelope.get("origin_ref")
        origin = ObjectRef.model_validate(origin_raw) if origin_raw is not None else None
        # P4 authorization is an immutable server sidecar. Do not trust a
        # mutable execution-envelope flag on the P2 run as the source of truth.
        update_sequence = None
        accepted_at = datetime.fromisoformat(str(payload.get("accepted_at")))
        if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
            raise ValueError("accepted_at must be timezone aware")
        if manifest.owner_id != owner_id:
            raise ValueError("manifest owner mismatch")
        # Unhashed observation time is still constrained by the admitted
        # instant. Historical runs retain the original manifest and admission.
        if mode == "live" and command != "supplemental_rule.update_recovery.v1" and (
            manifest.as_of != accepted_at or manifest.evaluated_at != accepted_at
        ):
            raise ValueError("live manifest timestamps must match the accepted instant")
    except (TypeError, ValueError, KeyError) as exc:
        raise InvestorError("historical_manifest_unavailable", "补充事项冻结运行内容无效") from exc
    if payload.get("request_hash") != request_hash:
        raise InvestorError("not_found", "评估运行请求摘要与原收据不匹配")
    if payload.get("source_manifest_hash") != manifest.manifest_hash:
        raise InvestorError("not_found", "评估运行清单与原收据不匹配")
    if not hasattr(command_body, "idempotency_key") or command_body.idempotency_key != operation_id:
        raise InvestorError("not_found", "运行命令标识与收据不匹配")

    row = await session.get(InvestorMutation, (owner_id, str(operation_id)))
    if row is None or row.request_hash != request_hash:
        raise InvestorError("not_found", "评估运行没有授权收据")
    result = row.result
    try:
        if result.get("p2_schema_version") != P2_RESULT_SCHEMA_VERSION:
            raise ValueError("wrong receipt format")
        receipt = MutationReceipt.model_validate(result["receipt"])
        expected_receipt_target = target
        if command in {
            "supplemental_rule.create.v1",
            "research_event.create_user_report.v1",
        }:
            expected_receipt_target = None
            expected_kind: Literal["supplemental_rule", "research_event"] = (
                "supplemental_rule" if command.startswith("supplemental_rule") else "research_event"
            )
            if receipt.result_ref != ObjectRef(
                kind=expected_kind,
                id=command_body.idempotency_key,
                revision=1,
            ):
                raise ValueError("create receipt result identity mismatch")
        if (
            receipt.owner_id != owner_id
            or receipt.operation_id != operation_id
            or receipt.command != command
            or receipt.target_ref != expected_receipt_target
            or receipt.run_ref != ObjectRef(kind="pipeline_run", id=run_id, revision=1)
            or receipt.source_manifest_hash != manifest.manifest_hash
            or receipt.accepted_at != accepted_at
        ):
            raise ValueError("receipt/run mismatch")
        if receipt.result_kind == "evaluation_admission":
            admission = EvaluationAdmission.model_validate(result["result"]["admission"])
            if (
                admission.manifest_hash != manifest.manifest_hash
                or admission.run_ref != receipt.run_ref
                or admission.operation_id != operation_id
                or admission.accepted_at != accepted_at
                or (command == "supplemental_rule.evaluate.v1" and (
                    command_body.mode != mode
                    or command_body.evaluation_purpose != purpose
                ))
                or (command == "supplemental_rule.update_recovery.v1" and purpose != "coverage")
            ):
                raise ValueError("evaluation admission mismatch")
        elif receipt.result_kind in {"rule_mutation", "event_mutation"}:
            if mode != "live" or purpose != "coverage":
                raise ValueError("mutation reassessment must use the live coverage envelope")
            expected_origin = receipt.result_ref or receipt.target_ref
            if origin != expected_origin:
                raise ValueError("mutation reassessment origin mismatch")
        else:
            raise ValueError("invalid result discriminator")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise InvestorError("not_found", "评估运行收据类型不匹配") from exc
    if mode == "historical_replay":
        if command != "supplemental_rule.evaluate.v1":
            raise InvestorError("historical_manifest_unavailable", "仅支持原始评估清单的显式重放")
        replay_ref = command_body.replay_run_ref
        if replay_ref is None or replay_ref.revision != 1:
            raise InvestorError("historical_manifest_unavailable", "原始重放运行引用不可用")
        _, original = await authorize_supplemental_run(
            session,
            int(replay_ref.id),
            owner_id=owner_id,
        )
        if original.command != "supplemental_rule.evaluate.v1" or original.mode != "live":
            raise InvestorError("historical_manifest_unavailable", "重放来源必须是原始live评估")
        original_run = await session.get(PipelineRun, int(replay_ref.id))
        if original_run is None:
            raise InvestorError("historical_manifest_unavailable", "原始重放运行不可用")
        original_request = EvaluationRequest.model_validate(
            original_run.request_json["command_body"]
        )
        if (
            command_body.mode != "historical_replay"
            or command_body.listing_refs != original_request.listing_refs
            or command_body.contexts != original_request.contexts
            or command_body.evaluation_purpose != original_request.evaluation_purpose
            or command_body.exact_rule_refs != original_request.exact_rule_refs
            or command_body.prior_application_refs != original_request.prior_application_refs
            or command_body.input_as_of != original.manifest.as_of
            or command_body.replay_manifest_hash != original.manifest.manifest_hash
            or manifest != original.manifest
            or manifest.evaluated_at != original.accepted_at
        ):
            raise InvestorError("historical_manifest_unavailable", "重放运行与原始冻结清单不匹配")
    elif command == "supplemental_rule.update_recovery.v1":
        assert isinstance(command_body, UpdateRecoveryCommand)
        try:
            original_run, original, original_member = await _authorize_recovery_source(
                session, command_body, owner_id=owner_id,
            )
            checkpoint = _read_checkpoint(original_run.metadata_json, original.manifest)
            await _verify_checkpoint(session, checkpoint, original)
            pairs = _recovery_pairs(
                command_body, original.manifest, checkpoint,
                listing_id=original_member.listing_id, context=original_member.context,
            )
            expected_manifest = validate_manifest(_recovery_manifest(original.manifest, pairs))
            if manifest != expected_manifest:
                raise ValueError("recovery manifest is not the exact original pending subset")
            sidecar = (await session.execute(select(SupplementalChangeReceiptRow).where(
                SupplementalChangeReceiptRow.owner_id == owner_id,
                SupplementalChangeReceiptRow.kind == "evaluation_admission",
                SupplementalChangeReceiptRow.origin_run_id == run_id,
                SupplementalChangeReceiptRow.source_manifest_hash == manifest.manifest_hash,
            ))).scalar_one_or_none()
            if (
                sidecar is None
                or sidecar.payload.get("recovery_of_run_id") != command_body.original_run_id
                or sidecar.payload.get("recovery_of_manifest_hash") != command_body.original_manifest_hash
                or sidecar.payload.get("recovered_member_sequence") != command_body.member_sequence
                or sidecar.payload.get("pending_ordinals") != list(command_body.pending_ordinals)
                or sidecar.payload.get("recovery_generation") != command_body.recovery_generation
                or sidecar.payload.get("has_manual_pair") is not command_body.has_manual_pair
            ):
                raise ValueError("recovery P4 sidecar mismatch")
        except (AttributeError, TypeError, ValueError, InvestorError) as exc:
            raise InvestorError("not_found", "恢复运行未通过原始检查点授权") from exc
    p4_admission = (await session.execute(select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == owner_id,
        SupplementalChangeReceiptRow.kind == "evaluation_admission",
        SupplementalChangeReceiptRow.origin_run_id == run.id,
    ))).scalar_one_or_none()
    if p4_admission is not None:
        member_sequences = p4_admission.payload.get("scope_member_sequences")
        source_sequence = p4_admission.parent_sequence
        if (
            p4_admission.payload.get("run_id") != run.id
            or p4_admission.payload.get("receipt_sequence") != source_sequence
            or p4_admission.source_manifest_hash != manifest.manifest_hash
            or p4_admission.payload.get("source_manifest_hash") != manifest.manifest_hash
            or not isinstance(source_sequence, int)
            or not isinstance(member_sequences, list)
            or not member_sequences
        ):
            raise InvestorError("not_found", "P4评估授权收据与P2运行不匹配")
        from investment_agent.storage.repositories.supplemental_updates import (
            SupplementalUpdatesRepository,
        )

        updates_repo = SupplementalUpdatesRepository(session)
        source_receipt = await updates_repo.get_change(owner_id, source_sequence)
        if source_receipt is None or source_receipt.kind not in {
            "rule_revision", "rule_lifecycle", "research_event", "evidence_revision",
            "material_revision", "due_boundary", "bootstrap", "source_attempt",
        }:
            raise InvestorError("not_found", "P4评估来源收据不可用")
        expected_scopes = {
            (int(listing_ref.id), context)
            for listing_ref in manifest.listing_refs
            for context in manifest.contexts
        }
        actual_scopes: set[tuple[int, str]] = set()
        for sequence in member_sequences:
            if not isinstance(sequence, int):
                raise InvestorError("not_found", "P4评估成员收据无效")
            member = await updates_repo.get_change(owner_id, sequence)
            if member is None or member.kind != "scope_member":
                raise InvestorError("not_found", "P4评估成员收据不存在")
            if member.listing_id is None or member.context is None:
                raise InvestorError("not_found", "P4评估成员scope不完整")
            if (
                member.payload.get("source_sequence") != source_sequence
                or member.payload.get("source_id") != str(source_receipt.id)
                or member.payload.get("source_hash") != source_receipt.payload_hash
            ):
                raise InvestorError("not_found", "P4评估成员来源不匹配")
            actual_scopes.add((member.listing_id, member.context))
        if actual_scopes != expected_scopes:
            raise InvestorError("not_found", "P4评估成员不属于冻结清单")
        update_sequence = source_sequence

    return run, AuthorizedSupplementalRun(
        run_ref=receipt.run_ref,
        operation_id=operation_id,
        command=command,
        target_ref=target,
        request_hash=request_hash,
        manifest=manifest,
        mode=mode,
        evaluation_purpose=purpose,
        origin_ref=origin,
        supplemental_update_sequence=update_sequence,
        accepted_at=accepted_at,
    )


class SupplementalRuleHandler(BaseJobHandler):
    kind = SUPPLEMENTAL_RULE_EVALUATE

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        async def work() -> JobOutcome:
            checkpoint: RunCheckpoint | None = None
            async with session_scope() as session:
                await SupplementalRulesRepository(session).reserve_registry("local_owner")
                repo = PipelineRunRepository(session)
                run, authorized = await authorize_supplemental_run(session, job.run_id)
                checkpoint = _read_checkpoint(run.metadata_json, authorized.manifest)
                await _verify_checkpoint(session, checkpoint, authorized)
                effective = await _enabled_settings(session, self.settings)
                if not _run_enabled(effective, authorized):
                    reason = "supplemental_updates_disabled" if authorized.supplemental_update_sequence is not None and effective.supplemental_rules_enabled else "writes_disabled"
                    await repo.mark_failed(job.run_id, reason, lease=job.lease)
                    return "partial"
                await repo.record_metadata(
                    job.run_id,
                    {"supplemental_checkpoint": checkpoint.model_dump(mode="json")},
                    lease=job.lease,
                )
            assert checkpoint is not None
            failed = False
            for pair in authorized.manifest.pairs:
                member = checkpoint.members[pair.ordinal]
                if member.state in {"saved", "item_failed"}:
                    continue
                async with session_scope() as session:
                    await SupplementalRulesRepository(session).reserve_registry("local_owner")
                    repo = PipelineRunRepository(session)
                    run, fresh = await authorize_supplemental_run(session, job.run_id)
                    checkpoint = _read_checkpoint(run.metadata_json, fresh.manifest)
                    effective = await _enabled_settings(session, self.settings)
                    if not _run_enabled(effective, fresh):
                        reason = "supplemental_updates_disabled" if fresh.supplemental_update_sequence is not None and effective.supplemental_rules_enabled else "writes_disabled"
                        await repo.mark_failed(
                            job.run_id, reason,
                            lease=job.lease,
                        )
                        return "partial"
                    member = checkpoint.members[pair.ordinal]
                    if member.state in {"saved", "item_failed"}:
                        continue
                    service = SupplementalRuleService(session, "local_owner")
                    try:
                        async with session.begin_nested():
                            application = await service.save_pair(
                                fresh.manifest,
                                pair,
                                operation_id=fresh.operation_id,
                                mode=fresh.mode,
                                evaluation_purpose=fresh.evaluation_purpose,
                                origin_ref=fresh.origin_ref,
                            )
                    except InvestorError as exc:
                        if exc.detail.code != "item_binding_conflict":
                            raise
                        updated = _replace_member(
                            checkpoint,
                            pair.ordinal,
                            CheckpointMember(
                                ordinal=pair.ordinal,
                                state="item_failed",
                                error_code="item_binding_conflict",
                            ),
                        )
                        await repo.record_metadata(
                            job.run_id,
                            {"supplemental_checkpoint": updated.model_dump(mode="json")},
                            lease=job.lease,
                        )
                        checkpoint = updated
                        failed = True
                    else:
                        _verify_application(application, fresh, pair)
                        updated = _replace_member(
                            checkpoint,
                            pair.ordinal,
                            CheckpointMember(
                                ordinal=pair.ordinal,
                                state="saved",
                                application_ref=application.reference,
                                input_hash=application.input_hash,
                            ),
                        )
                        await repo.record_metadata(
                            job.run_id,
                            {"supplemental_checkpoint": updated.model_dump(mode="json")},
                            lease=job.lease,
                        )
                        checkpoint = updated
            async with session_scope() as session:
                await SupplementalRulesRepository(session).reserve_registry("local_owner")
                repo = PipelineRunRepository(session)
                run, authorized = await authorize_supplemental_run(session, job.run_id)
                checkpoint = _read_checkpoint(run.metadata_json, authorized.manifest)
                await _verify_checkpoint(session, checkpoint, authorized)
                effective = await _enabled_settings(session, self.settings)
                if not _run_enabled(effective, authorized):
                    reason = "supplemental_updates_disabled" if authorized.supplemental_update_sequence is not None and effective.supplemental_rules_enabled else "writes_disabled"
                    await repo.mark_failed(job.run_id, reason, lease=job.lease)
                    return "partial"
                failed = failed or any(
                    member.state == "item_failed" for member in checkpoint.members
                )
                metadata = {
                    "supplemental_checkpoint": checkpoint.model_dump(mode="json"),
                    "completed_count": sum(m.state == "saved" for m in checkpoint.members),
                    "failed_items": [
                        {"ordinal": m.ordinal, "reason_code": m.error_code}
                        for m in checkpoint.members
                        if m.state == "item_failed"
                    ],
                    "source_manifest_hash": authorized.manifest.manifest_hash,
                }
                await repo.record_metadata(job.run_id, metadata, lease=job.lease)
                if failed:
                    await repo.mark_partial(
                        job.run_id, "supplemental_item_binding_conflict", metadata, lease=job.lease
                    )
                    return "partial"
                await repo.mark_success(job.run_id, metadata, lease=job.lease)
                return "completed"

        return await ctx.run_with_heartbeat(job.lease, work)


async def _enabled_settings(session: AsyncSession, base: Settings) -> Settings:
    from investment_agent.settings.loader import load_settings

    return await load_settings(session, base=base)


def _run_enabled(settings: Settings, authorized: AuthorizedSupplementalRun) -> bool:
    """P4-linked runs stop at checkpoints when updates are disabled.

    Native P2 runs retain their original single supplemental_rules gate.
    """
    return settings.supplemental_rules_enabled and (
        authorized.supplemental_update_sequence is None
        or getattr(settings, "supplemental_updates_enabled", False)
    )


def _read_checkpoint(metadata: Any, manifest: FrozenManifest) -> RunCheckpoint:
    raw = metadata.get("supplemental_checkpoint") if isinstance(metadata, dict) else None
    if raw is None:
        return RunCheckpoint(
            source_manifest_hash=manifest.manifest_hash,
            members=tuple(
                CheckpointMember(ordinal=p.ordinal, state="pending") for p in manifest.pairs
            ),
            updated_at=datetime.now().astimezone(),
        )
    try:
        checkpoint = RunCheckpoint.model_validate(raw)
    except ValidationError as exc:
        raise InvestorError(
            "historical_manifest_unavailable", "补充事项运行检查点无效"
        ) from exc
    if checkpoint.source_manifest_hash != manifest.manifest_hash:
        raise ValueError("checkpoint and frozen manifest hash differ")
    if tuple(member.ordinal for member in checkpoint.members) != tuple(range(len(manifest.pairs))):
        raise ValueError("checkpoint does not represent every frozen member in ordinal order")
    return checkpoint


def _replace_member(
    checkpoint: RunCheckpoint, ordinal: int, member: CheckpointMember
) -> RunCheckpoint:
    if ordinal < 0 or ordinal >= len(checkpoint.members) or member.ordinal != ordinal:
        raise ValueError("unknown or changed frozen pair ordinal")
    members = list(checkpoint.members)
    members[ordinal] = member
    return checkpoint.model_copy(
        update={"members": tuple(members), "updated_at": datetime.now().astimezone()}
    )


async def _verify_checkpoint(
    session: AsyncSession,
    checkpoint: RunCheckpoint,
    authorized: AuthorizedSupplementalRun,
) -> None:
    """Re-read saved members and ensure the complete checkpoint is trustworthy."""
    refs: set[ObjectRef] = set()
    stored_refs: set[ObjectRef] = set()
    pair_count = len(authorized.manifest.pairs)
    operation_rows = list((await session.scalars(
        select(InvestmentAssessment)
        .where(
            InvestmentAssessment.owner_id == authorized.manifest.owner_id,
            InvestmentAssessment.kind == "rule_application",
            func.json_extract(
                InvestmentAssessment.payload, literal_column("'$.operation_id'")
            ) == str(authorized.operation_id),
        )
        .limit(pair_count + 1)
    )).all())
    if len(operation_rows) > pair_count:
        raise ValueError("run operation owns more applications than frozen members")

    expected_refs = []
    for pair in authorized.manifest.pairs:
        series = stable_series_key(
            str(authorized.manifest.owner_id),
            str(pair.listing_ref.id),
            str(pair.rule_ref.id),
            pair.context,
        )
        expected_ref = ObjectRef(
            kind="rule_application", id=uuid5(authorized.operation_id, series), revision=1
        )
        expected_refs.append(expected_ref)

    # Read the deterministic member IDs in bounded IN batches. This finds a
    # committed app even if corrupted JSON changed its operation_id, without
    # issuing one SELECT per pair.
    rows_by_key = {
        (row.id, row.revision): row for row in operation_rows
    }
    for offset in range(0, len(expected_refs), 500):
        chunk = expected_refs[offset : offset + 500]
        rows = await session.scalars(
            select(InvestmentAssessment).where(
                InvestmentAssessment.owner_id == authorized.manifest.owner_id,
                InvestmentAssessment.kind == "rule_application",
                InvestmentAssessment.revision == 1,
                InvestmentAssessment.id.in_([str(reference.id) for reference in chunk]),
            )
        )
        rows_by_key.update({(row.id, row.revision): row for row in rows.all()})

    applications: dict[ObjectRef, RuleApplication] = {}
    for (application_id, revision), row in rows_by_key.items():
        artifact = _parse_assessment(row)
        _check_header_columns(row, artifact)
        if not isinstance(artifact, RuleApplication):
            raise ValueError("run application row has an invalid payload type")
        reference = ObjectRef(
            kind="rule_application", id=UUID(application_id), revision=revision
        )
        applications[reference] = artifact
        stored_refs.add(reference)

    for pair, expected_ref in zip(authorized.manifest.pairs, expected_refs, strict=True):
        saved = applications.get(expected_ref)
        if saved is not None:
            _verify_application(saved, authorized, pair)
    for member in checkpoint.members:
        if member.state != "saved":
            continue
        assert member.application_ref is not None
        if member.application_ref in refs:
            raise ValueError("checkpoint contains duplicate application references")
        refs.add(member.application_ref)
        saved = applications.get(member.application_ref)
        if not isinstance(saved, RuleApplication):
            raise ValueError("checkpoint points to a missing or wrong-kind application")
        pair = authorized.manifest.pairs[member.ordinal]
        if (
            saved.operation_id != authorized.operation_id
            or saved.application_mode != authorized.mode
            or saved.evaluation_purpose != authorized.evaluation_purpose
            or saved.origin_ref != authorized.origin_ref
            or saved.source_manifest_hash != authorized.manifest.manifest_hash
            or saved.input_hash != member.input_hash
            or saved.frozen_pair != pair
        ):
            raise ValueError("checkpoint application identity or frozen input changed")
    if refs != stored_refs:
        raise ValueError("checkpoint saved references do not equal the run's persisted applications")


def _verify_application(
    application: RuleApplication,
    authorized: AuthorizedSupplementalRun,
    pair: Any,
) -> None:
    if (
        application.operation_id != authorized.operation_id
        or application.application_mode != authorized.mode
        or application.evaluation_purpose != authorized.evaluation_purpose
        or application.origin_ref != authorized.origin_ref
        or application.source_manifest_hash != authorized.manifest.manifest_hash
        or application.frozen_pair != pair
        or application.listing_ref != pair.listing_ref
        or application.rule_ref != pair.rule_ref
        or application.context != pair.context
        or application.application_mode != authorized.mode
        or application.evaluation_purpose != authorized.evaluation_purpose
        or application.origin_ref != authorized.origin_ref
        or application.input_hash
        != canonical_hash(
            {
                "pair": pair,
                "manifest_hash": authorized.manifest.manifest_hash,
                "mode": authorized.mode,
                "purpose": authorized.evaluation_purpose,
            }
        )
    ):
        raise ValueError("saved application does not match the frozen manifest member")

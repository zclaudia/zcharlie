"""Atomic draft, validation and operator decision service for P2 rules."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.supplemental_rules import SupplementalRuleRevision
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.admission import (
    require_enabled,
    reserve_and_replay,
    save_receipt,
)
from investment_agent.supplemental_rules.commands import (
    RuleCreateRequest,
    RuleDecisionRequest,
    RuleMutationResponse,
    RuleRevisionRequest,
    RuleValidationRequest,
    ValidationResponse,
)
from investment_agent.supplemental_rules.contracts import MutationReceipt, RuleDefinition
from investment_agent.supplemental_rules.validation import validate_rule


class SupplementalRuleLifecycle:
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
        self.repo = SupplementalRulesRepository(session)

    async def create(self, request: RuleCreateRequest) -> RuleMutationResponse:
        operation_id = request.idempotency_key
        rule_id = operation_id
        command = "supplemental_rule.create.v1"
        request_hash, prior = await reserve_and_replay(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=None,
            body=request,
        )
        if prior is not None:
            return _saved_rule_response(prior)
        await require_enabled(self.session, self.settings)

        definition = request.definition.as_revision(rule_id, 1)
        await self.repo.save_rule_revision(self.owner_id, definition)
        head = await self.repo.put_rule_head(
            self.owner_id,
            rule_id,
            expected_head_revision=0,
            active_revision=None,
            active_state=None,
            draft_revision=1,
            draft_state="draft",
            last_validation_revision=None,
            last_validation_hash=None,
            last_validation_payload=None,
        )
        now = datetime.now(UTC)
        await self.repo.append_rule_event(
            self.owner_id,
            rule_id,
            operation_id=operation_id,
            command="draft_created",
            rule_revision=1,
            payload={
                "definition_hash": definition.content_hash,
                "head_revision": head.head_revision,
                "actor": "authenticated_api_operator",
            },
            created_at=now,
        )
        await self.repo.advance_registry(self.owner_id, updated_at=now)
        target = ObjectRef(kind="supplemental_rule", id=rule_id, revision=1)
        await self._record_change(
            definition,
            operation_id=operation_id,
            request_hash=request_hash,
            command=command,
            change_type="revision",
            accepted_at=now,
        )
        receipt = MutationReceipt(
            owner_id=self.owner_id,
            operation_id=operation_id,
            result_kind="rule_mutation",
            command=command,
            target_ref=None,
            accepted_at=now,
            result_ref=target,
        )
        response = RuleMutationResponse(
            receipt=receipt,
            rule_ref=target,
            head_revision=head.head_revision,
            state="draft",
        )
        save_receipt(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            request_hash=request_hash,
            receipt=receipt,
            result={"rule_mutation": response.model_dump(mode="json")},
        )
        await self.session.flush()
        return response

    async def create_revision(
        self, rule_id: UUID, request: RuleRevisionRequest
    ) -> RuleMutationResponse:
        target = ObjectRef(kind="supplemental_rule", id=rule_id, revision=1)
        operation_id = request.idempotency_key
        command = "supplemental_rule.revision.v1"
        request_hash, prior = await reserve_and_replay(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=target,
            body=request,
        )
        if prior is not None:
            return _saved_rule_response(prior)
        await require_enabled(self.session, self.settings)
        head = await self.repo.get_rule_head(self.owner_id, rule_id)
        if head is None:
            raise InvestorError("not_found", "追加规则不存在")
        _check_head(head.head_revision, request.expected_head_revision)
        maximum_revision = await self.session.scalar(
            select(func.max(SupplementalRuleRevision.revision)).where(
                SupplementalRuleRevision.owner_id == self.owner_id,
                SupplementalRuleRevision.id == str(rule_id),
            )
        )
        revision = int(maximum_revision or 0) + 1
        definition = request.definition.as_revision(rule_id, revision)
        await self.repo.save_rule_revision(self.owner_id, definition)
        head = await self.repo.put_rule_head(
            self.owner_id,
            rule_id,
            expected_head_revision=request.expected_head_revision,
            active_revision=head.active_revision,
            active_state=head.active_state,
            draft_revision=revision,
            draft_state="draft",
            last_validation_revision=None,
            last_validation_hash=None,
            last_validation_payload=None,
        )
        now = datetime.now(UTC)
        await self.repo.append_rule_event(
            self.owner_id,
            rule_id,
            operation_id=operation_id,
            command="draft_created",
            rule_revision=revision,
            payload={
                "definition_hash": definition.content_hash,
                "head_revision": head.head_revision,
                "actor": "authenticated_api_operator",
            },
            created_at=now,
        )
        await self.repo.advance_registry(self.owner_id, updated_at=now)
        ref = ObjectRef(kind="supplemental_rule", id=rule_id, revision=revision)
        await self._record_change(
            definition,
            operation_id=operation_id,
            request_hash=request_hash,
            command=command,
            change_type="revision",
            accepted_at=now,
        )
        receipt = MutationReceipt(
            owner_id=self.owner_id,
            operation_id=operation_id,
            result_kind="rule_mutation",
            command=command,
            target_ref=target,
            accepted_at=now,
            result_ref=ref,
        )
        response = RuleMutationResponse(
            receipt=receipt,
            rule_ref=ref,
            head_revision=head.head_revision,
            state="draft",
        )
        save_receipt(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            request_hash=request_hash,
            receipt=receipt,
            result={"rule_mutation": response.model_dump(mode="json")},
        )
        await self.session.flush()
        return response

    async def validate(self, rule_id: UUID, request: RuleValidationRequest) -> ValidationResponse:
        target = ObjectRef(kind="supplemental_rule", id=rule_id, revision=1)
        operation_id = request.idempotency_key
        command = "supplemental_rule.validate.v1"
        request_hash, prior = await reserve_and_replay(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=target,
            body=request,
        )
        if prior is not None:
            try:
                return ValidationResponse.model_validate(prior["result"]["validation"])
            except (KeyError, TypeError, ValueError) as exc:
                raise InvestorError("idempotency_conflict", "校验收据类型不匹配") from exc
        await require_enabled(self.session, self.settings)
        head = await self.repo.get_rule_head(self.owner_id, rule_id)
        if head is None:
            raise InvestorError("not_found", "追加规则不存在")
        _check_head(head.head_revision, request.expected_head_revision)
        definition = await self.repo.get_rule_revision(self.owner_id, request.rule_ref)
        if (
            definition is None
            or request.rule_ref.id != rule_id
            or head.draft_revision != request.rule_ref.revision
            or definition.content_hash != request.content_hash
        ):
            raise InvestorError("revision_conflict", "校验目标不是最新草案或内容摘要已变化")
        outcome = await validate_rule(
            self.session,
            self.owner_id,
            definition,
            expected_head_revision=request.expected_head_revision,
            committed_head_revision=request.expected_head_revision + 1,
        )
        head = await self.repo.put_rule_head(
            self.owner_id,
            rule_id,
            expected_head_revision=request.expected_head_revision,
            active_revision=head.active_revision,
            active_state=head.active_state,
            draft_revision=head.draft_revision,
            draft_state="validated" if outcome.outcome == "pass" else "draft",
            last_validation_revision=request.rule_ref.revision,
            last_validation_hash=outcome.validation_hash,
            last_validation_payload=outcome.payload(),
        )
        now = outcome.validated_at
        event = await self.repo.append_rule_event(
            self.owner_id,
            rule_id,
            operation_id=operation_id,
            command=f"validation_{outcome.outcome}",
            rule_revision=request.rule_ref.revision,
            payload={**outcome.payload(), "actor": "deterministic_validator"},
            created_at=now,
        )
        await self.repo.advance_registry(self.owner_id, updated_at=now)
        receipt = MutationReceipt(
            owner_id=self.owner_id,
            operation_id=operation_id,
            result_kind="rule_mutation",
            command=command,
            target_ref=target,
            accepted_at=now,
            result_ref=request.rule_ref,
        )
        response = ValidationResponse(
            receipt=receipt,
            rule_ref=request.rule_ref,
            content_hash=outcome.content_hash,
            validation_hash=outcome.validation_hash,
            outcome=outcome.outcome,
            diagnostics=outcome.diagnostics,
            head_revision=head.head_revision,
            event_sequence=event.sequence,
        )
        save_receipt(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            request_hash=request_hash,
            receipt=receipt,
            result={"validation": response.model_dump(mode="json")},
        )
        await self.session.flush()
        return response

    async def decide(self, rule_id: UUID, request: RuleDecisionRequest) -> RuleMutationResponse:
        target = ObjectRef(kind="supplemental_rule", id=rule_id, revision=1)
        operation_id = request.idempotency_key
        command = f"supplemental_rule.{request.action}.v1"
        request_hash, prior = await reserve_and_replay(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=target,
            body=request,
        )
        if prior is not None:
            return _saved_rule_response(prior)
        await require_enabled(self.session, self.settings)
        head = await self.repo.get_rule_head(self.owner_id, rule_id)
        if head is None:
            raise InvestorError("not_found", "追加规则不存在")
        _check_head(head.head_revision, request.expected_head_revision)
        definition = await self.repo.get_rule_revision(self.owner_id, request.rule_ref)
        if (
            definition is None
            or request.rule_ref.id != rule_id
            or definition.content_hash != request.content_hash
        ):
            raise InvestorError("revision_conflict", "决策版本或内容摘要已变化")
        now = datetime.now(UTC)
        active_revision, active_state = head.active_revision, head.active_state
        draft_revision, draft_state = head.draft_revision, head.draft_state
        validation_revision, validation_hash = (
            head.last_validation_revision,
            head.last_validation_hash,
        )
        validation_payload = head.last_validation_payload
        audit_rows: list[tuple[str, int | None, dict[str, object]]] = []
        rules_delta = 0

        if request.action == "approve":
            if head.draft_revision != request.rule_ref.revision:
                raise InvestorError("revision_conflict", "只能批准最新草案")
            saved_validation = validation_payload or {}
            if (
                validation_revision != request.rule_ref.revision
                or head.draft_state != "validated"
                or validation_hash != request.validation_hash
                or saved_validation.get("outcome") != "pass"
                or saved_validation.get("content_hash") != definition.content_hash
                or saved_validation.get("committed_head_revision") != head.head_revision
                or _iso_after(saved_validation.get("expires_at"), now) is False
            ):
                raise InvestorError("revision_conflict", "最新校验缺失、失败或已过期")
            if definition.valid_from > now or (
                definition.valid_until is not None and definition.valid_until <= now
            ):
                raise InvestorError("invalid_reference", "规则当前不在可批准的有效期内")
            if definition.review_due_at <= now:
                raise InvestorError("invalid_reference", "规则复核时间已到期")
            latest_validation = await validate_rule(
                self.session,
                self.owner_id,
                definition,
                expected_head_revision=head.head_revision - 1,
                committed_head_revision=head.head_revision,
                validated_at=now,
            )
            if (
                latest_validation.outcome != "pass"
                or latest_validation.dependency_fingerprint
                != saved_validation.get("dependency_fingerprint")
            ):
                raise InvestorError("revision_conflict", "校验依赖或来源已变化，请重新校验")
            if active_revision is not None:
                audit_rows.append(
                    (
                        "rule_superseded",
                        active_revision,
                        {"replaced_by": request.rule_ref.model_dump(mode="json")},
                    )
                )
            audit_rows.append(
                (
                    "rule_approved",
                    request.rule_ref.revision,
                    {"validation_hash": request.validation_hash, "reason": request.reason},
                )
            )
            active_revision, active_state = request.rule_ref.revision, "active"
            draft_revision, draft_state = None, None
            validation_revision, validation_hash, validation_payload = None, None, None
            rules_delta = 1
        elif request.action in {"suspend", "retire"}:
            allowed_states = {"active"} if request.action == "suspend" else {"active", "suspended"}
            if (
                head.active_revision != request.rule_ref.revision
                or head.active_state not in allowed_states
            ):
                raise InvestorError("revision_conflict", "暂停或退役必须指向当前active版本")
            next_state = "suspended" if request.action == "suspend" else "retired"
            if head.active_state == next_state:
                raise InvestorError("revision_conflict", "规则已经处于目标状态")
            active_state = next_state
            audit_rows.append(
                (f"rule_{next_state}", request.rule_ref.revision, {"reason": request.reason})
            )
            rules_delta = 1
        elif request.action == "reject":
            if head.draft_revision != request.rule_ref.revision:
                raise InvestorError("revision_conflict", "只能拒绝最新草案")
            draft_revision, draft_state = None, None
            validation_revision, validation_hash, validation_payload = None, None, None
            audit_rows.append(
                ("draft_rejected", request.rule_ref.revision, {"reason": request.reason})
            )

        head = await self.repo.put_rule_head(
            self.owner_id,
            rule_id,
            expected_head_revision=request.expected_head_revision,
            active_revision=active_revision,
            active_state=active_state,
            draft_revision=draft_revision,
            draft_state=draft_state,
            last_validation_revision=validation_revision,
            last_validation_hash=validation_hash,
            last_validation_payload=validation_payload,
        )
        for audit_command, revision, payload in audit_rows:
            await self.repo.append_rule_event(
                self.owner_id,
                rule_id,
                operation_id=operation_id,
                command=audit_command,
                rule_revision=revision,
                payload={
                    **payload,
                    "head_revision": head.head_revision,
                    "actor": "authenticated_api_operator",
                },
                created_at=now,
            )
        await self.repo.advance_registry(
            self.owner_id,
            rules_delta=rules_delta,
            updated_at=now,
        )
        run_ref, source_manifest_hash = (
            await self._enqueue_reassessment(
                definition if request.action in {"approve", "reject"} else definition,
                operation_id=operation_id,
                command=command,
                request=request,
                target=target,
                request_hash=request_hash,
                accepted_at=now,
            )
            if rules_delta
            else (None, None)
        )
        await self._record_change(
            definition,
            operation_id=operation_id,
            request_hash=request_hash,
            command=command,
            change_type="lifecycle",
            accepted_at=now,
            run_id=int(run_ref.id) if run_ref is not None else None,
            manifest_hash=source_manifest_hash,
        )
        receipt = MutationReceipt(
            owner_id=self.owner_id,
            operation_id=operation_id,
            result_kind="rule_mutation",
            command=command,
            target_ref=target,
            accepted_at=now,
            result_ref=request.rule_ref,
            run_ref=run_ref,
            source_manifest_hash=source_manifest_hash,
        )
        response = RuleMutationResponse(
            receipt=receipt,
            rule_ref=request.rule_ref,
            head_revision=head.head_revision,
            state=active_state or (draft_state or "rejected"),
            run_ref=run_ref,
        )
        save_receipt(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            request_hash=request_hash,
            receipt=receipt,
            result={"rule_mutation": response.model_dump(mode="json")},
        )
        await self.session.flush()
        return response

    async def _enqueue_reassessment(
        self,
        definition: RuleDefinition,
        *,
        operation_id: UUID,
        command: str,
        request: RuleDecisionRequest,
        target: ObjectRef,
        request_hash: str,
        accepted_at: datetime,
    ) -> tuple[ObjectRef, str]:
        # The bounded subject selection belongs to the read/evaluation service;
        # no rules are evaluated in this lifecycle transaction.
        from investment_agent.supplemental_rules.admission import enqueue_manifest

        run_ref, source_manifest_hash = await enqueue_manifest(
            self.session,
            owner_id=self.owner_id,
            operation_id=operation_id,
            command=command,
            target_ref=target,
            command_body=request,
            request_hash=request_hash,
            source=definition,
            accepted_at=accepted_at,
            trigger="supplemental_rules_lifecycle",
            origin_ref=request.rule_ref,
        )
        return run_ref, source_manifest_hash

    async def _record_change(
        self,
        definition: RuleDefinition,
        *,
        operation_id: UUID,
        request_hash: str,
        command: str,
        change_type: str,
        accepted_at: datetime,
        run_id: int | None = None,
        manifest_hash: str | None = None,
    ) -> None:
        """Append a domain-change receipt atomically with its P2 mutation."""
        from investment_agent.storage.repositories.supplemental_updates import (
            SupplementalUpdatesRepository,
            supplemental_updates_journal_available,
        )

        if not await supplemental_updates_journal_available(self.session):
            return
        await SupplementalUpdatesRepository(self.session).append_change(
            self.owner_id,
            change_key=f"p2:rule:{definition.id}:revision:{definition.revision}:operation:{operation_id}",
            kind="rule_lifecycle" if change_type == "lifecycle" else "rule_revision",
            operation_id=operation_id,
            request_hash=request_hash,
            origin_run_id=run_id,
            source_manifest_hash=manifest_hash,
            acquired_at=accepted_at,
            available_from=accepted_at,
            committed_at=accepted_at,
            payload={
                "schema_version": 1,
                "source_ref": {
                    "kind": "supplemental_rule",
                    "id": str(definition.id),
                    "revision": definition.revision,
                },
                "change_type": change_type,
                "command": command,
                "content_hash": definition.content_hash,
                "semantic_hash": definition.semantic_hash,
                "rules_generation": (
                    await self.repo.get_registry_head(self.owner_id)
                ).rules_generation,
                "request_hash": request_hash,
            },
        )


def _check_head(actual: int, expected: int) -> None:
    if actual != expected:
        raise InvestorError("revision_conflict", "追加规则已发生变化，请刷新后重试")


def _iso_after(raw: object, now: datetime) -> bool:
    if not isinstance(raw, str):
        return False
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if value.tzinfo is None or value.utcoffset() is None:
        return False
    return value > now


def _saved_rule_response(envelope: dict[str, object]) -> RuleMutationResponse:
    try:
        receipt = MutationReceipt.model_validate(envelope["receipt"])
        if receipt.result_kind != "rule_mutation":
            raise ValueError("receipt discriminator mismatch")
        result = envelope["result"]
        if not isinstance(result, dict):
            raise ValueError("receipt result must be a mapping")
        return RuleMutationResponse.model_validate(result["rule_mutation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvestorError("idempotency_conflict", "规则收据类型不匹配") from exc

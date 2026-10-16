"""Atomic user admission and leased local account-fit jobs.

The receipt hashes only the client command. All worker inputs are separately
frozen in the existing run; retries never choose newer evidence.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.portfolio.calculations import TradingRuleResolver
from investment_agent.portfolio.service import PortfolioService, digest
from investment_agent.portfolio.suitability import AccountFitService
from investment_agent.portfolio.suitability_calculations import FitItemEvaluationError
from investment_agent.portfolio.suitability_contracts import (
    AccountFitRequest,
    FrozenAccountInputs,
    HistoricalReplayBinding,
)
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.settings.loader import load_settings
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investor import InvestorMutation
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

ACCOUNT_FIT_JOB = "account_fit_evaluate"
CANDIDATE_RANK_JOB = "candidate_rank"
ASSESSMENT_JOBS = {ACCOUNT_FIT_JOB, CANDIDATE_RANK_JOB}


class AssessmentWritesDisabledError(InvestorError):
    def __init__(self) -> None:
        super().__init__("writes_disabled", "账户适配写入已关闭")


class AdmissionReceipt(Contract):
    operation_id: UUID
    run_ref: ObjectRef
    accepted_as_of: datetime
    request_hash: str
    source_manifest_hash: str
    replayed: bool = False


class AssessmentRunView(Contract):
    run_ref: ObjectRef
    operation_id: UUID
    status: str
    completed_count: int
    failed_items: tuple[dict[str, Any], ...]
    fit_refs: tuple[ObjectRef, ...]
    ranking_ref: ObjectRef | None
    failure_code: str | None


def validated_batch_frozen_inputs(value: Any) -> FrozenAccountInputs:
    """Validate the immutable batch fingerprint before a worker consumes it.

    Historical replay changes only ``mode``; normalize that field back to the
    original live admission value before checking the batch digest.
    """
    frozen = FrozenAccountInputs.model_validate(value)
    canonical = frozen.model_copy(update={"mode": "live"})
    expected = digest(
        canonical.model_dump(mode="json", exclude={"manifest_hash", "source_fingerprint"})
    )
    if frozen.manifest_hash != expected or frozen.source_fingerprint != expected:
        raise ValueError("frozen account input fingerprint mismatch")
    return frozen


def validate_batch_request_mode(frozen: FrozenAccountInputs, request: Any) -> None:
    """Keep the request's live/replay contract bound to the frozen batch mode."""
    request_mode = getattr(request, "mode", None)
    if request_mode != frozen.mode:
        raise ValueError("assessment request mode does not match frozen input mode")


async def check_enabled(session: AsyncSession, base: Settings, *, ranking: bool = False) -> None:
    effective = await load_settings(session, base=base)
    if (
        not effective.portfolio_ledger_enabled
        or not effective.account_fit_enabled
        or (ranking and not effective.candidate_ranking_enabled)
    ):
        raise AssessmentWritesDisabledError()


async def authorized_run(
    session: AsyncSession, run_id: int, *, owner: str = "local_owner"
) -> PipelineRun:
    run = await session.get(PipelineRun, run_id, populate_existing=True)
    if run is None or run.kind not in ASSESSMENT_JOBS or run.request_json.get("owner_id") != owner:
        raise InvestorError("not_found", "评估运行不存在")
    payload = run.request_json
    key = str(payload.get("operation_id", ""))
    receipt = await session.get(InvestorMutation, (owner, key))
    client_request = payload.get("client_request")
    expected_request_hash = (
        digest({"command": run.kind, "body": client_request})
        if isinstance(client_request, dict)
        else None
    )
    if (
        receipt is None
        or expected_request_hash is None
        or receipt.request_hash != expected_request_hash
        or payload.get("request_hash") != expected_request_hash
    ):
        raise InvestorError("not_found", "评估运行没有授权收据")
    saved = AdmissionReceipt.model_validate(receipt.result)
    if saved.run_ref.id != run_id or saved.source_manifest_hash != payload.get(
        "source_manifest_hash"
    ):
        raise InvestorError("not_found", "评估运行与授权收据不匹配")
    return run


async def historical_payload(
    session: AsyncSession, binding: HistoricalReplayBinding, *, owner: str, kind: str
) -> dict[str, Any]:
    try:
        run = await authorized_run(session, int(binding.run_ref.id), owner=owner)
    except InvestorError as exc:
        if exc.detail.code != "not_found":
            raise
        raise InvestorError("historical_manifest_unavailable", "没有可授权回放的历史清单") from exc
    if (
        run.kind != kind
        or binding.run_ref.revision != 1
        or run.request_json.get("source_manifest_hash") != binding.manifest_hash
    ):
        raise InvestorError("historical_manifest_unavailable", "历史清单身份不匹配")
    try:
        frozen = validated_batch_frozen_inputs(run.request_json.get("frozen_inputs"))
        client_request = run.request_json.get("client_request")
        if not isinstance(client_request, dict) or client_request.get("mode") != frozen.mode:
            raise ValueError("assessment request mode does not match frozen input mode")
    except (TypeError, ValueError) as exc:
        raise InvestorError("historical_manifest_unavailable", "历史账户输入摘要校验失败") from exc
    if (
        kind == ACCOUNT_FIT_JOB
        and run.request_json.get("source_manifest_hash") != frozen.manifest_hash
    ):
        raise InvestorError("historical_manifest_unavailable", "历史账户输入与授权摘要不匹配")
    if frozen.assessment_as_of != binding.input_as_of:
        raise InvestorError("historical_manifest_unavailable", "历史清单时点不匹配")
    return run.request_json


async def admit_operation(
    session: AsyncSession,
    *,
    owner: str,
    base: Settings,
    kind: str,
    operation_id: UUID,
    body: dict[str, Any],
    freeze: Callable[[datetime], Awaitable[dict[str, Any]]],
) -> AdmissionReceipt:
    # DML first: a preview/read transaction cannot be upgraded after a newer
    # writer commits. This reservation precedes receipt and source reads.
    await PortfolioService(session, owner)._reserve()
    request_hash = digest({"command": kind, "body": body})
    prior = await session.get(InvestorMutation, (owner, str(operation_id)))
    if prior is not None:
        if prior.request_hash != request_hash:
            raise InvestorError("idempotency_conflict", "操作标识已用于不同命令或输入")
        return AdmissionReceipt.model_validate(prior.result).model_copy(update={"replayed": True})
    await check_enabled(session, base, ranking=kind == CANDIDATE_RANK_JOB)
    accepted_at = datetime.now(UTC)
    frozen = await freeze(accepted_at)
    source_hash = str(frozen["source_manifest_hash"])
    payload = {
        "owner_id": owner,
        "operation_id": str(operation_id),
        "request_hash": request_hash,
        "client_request": body,
        **frozen,
    }
    run, created = await PipelineRunRepository(session).enqueue(
        kind=kind,
        idempotency_key=f"{kind}:{owner}:{operation_id}",
        triggered_by="account_fit_api",
        request_json=payload,
    )
    if not created:
        raise InvestorError("idempotency_conflict", "运行已存在但缺少原子接收收据")
    receipt = AdmissionReceipt(
        operation_id=operation_id,
        run_ref=ObjectRef(kind="pipeline_run", id=run.id, revision=1),
        accepted_as_of=accepted_at,
        request_hash=request_hash,
        source_manifest_hash=source_hash,
    )
    session.add(
        InvestorMutation(
            owner_id=owner,
            key=str(operation_id),
            request_hash=request_hash,
            result=receipt.model_dump(mode="json"),
        )
    )
    await session.flush()
    return receipt


class AccountFitAdmission:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        owner: str = "local_owner",
        rules: TradingRuleResolver | None = None,
    ) -> None:
        self.session, self.settings, self.owner, self.rules = session, settings, owner, rules

    async def accept(self, request: AccountFitRequest) -> AdmissionReceipt:
        async def freeze(as_of: datetime) -> dict[str, Any]:
            if request.historical_replay is not None:
                prior = await historical_payload(
                    self.session, request.historical_replay, owner=self.owner, kind=ACCOUNT_FIT_JOB
                )
                original = AccountFitRequest.model_validate(prior["client_request"])
                if request.model_dump(
                    mode="json", exclude={"operation_id", "mode", "historical_replay"}
                ) != original.model_dump(
                    mode="json", exclude={"operation_id", "mode", "historical_replay"}
                ):
                    raise InvestorError(
                        "historical_manifest_unavailable", "历史回放输入必须与原命令一致"
                    )
                frozen = validated_batch_frozen_inputs(prior["frozen_inputs"]).model_copy(
                    update={"mode": "historical_replay"}
                )
                return {
                    "frozen_inputs": frozen.model_dump(mode="json"),
                    "source_manifest_hash": prior["source_manifest_hash"],
                }
            frozen = await SuitabilityInputService(self.session, self.owner, self.rules).freeze(
                request, as_of=as_of
            )
            return {
                "frozen_inputs": frozen.model_dump(mode="json"),
                "source_manifest_hash": frozen.manifest_hash,
            }

        return await admit_operation(
            self.session,
            owner=self.owner,
            base=self.settings,
            kind=ACCOUNT_FIT_JOB,
            operation_id=request.operation_id,
            body=request.model_dump(mode="json"),
            freeze=freeze,
        )


async def run_view(
    session: AsyncSession, run_id: int, *, owner: str = "local_owner"
) -> AssessmentRunView:
    run = await authorized_run(session, run_id, owner=owner)
    progress: dict[str, Any] = dict(run.metadata_json or {})
    completed = progress.get("completed", {})
    failed = progress.get("failed_items", [])
    return AssessmentRunView(
        run_ref=ObjectRef(kind="pipeline_run", id=run.id, revision=1),
        operation_id=UUID(str(run.request_json["operation_id"])),
        status=run.status,
        completed_count=len(completed),
        failed_items=tuple(failed),
        fit_refs=tuple(
            ObjectRef.model_validate(value["fit_ref"])
            for value in completed.values()
            if value.get("fit_ref") is not None
        ),
        ranking_ref=ObjectRef.model_validate(progress["ranking_ref"])
        if progress.get("ranking_ref") is not None and run.status in {"success", "partial"}
        else None,
        failure_code="writes_disabled"
        if run.error_summary and "AssessmentWritesDisabledError" in run.error_summary
        else "evaluation_failed"
        if run.status == "failed"
        else None,
    )


class AccountFitHandler(BaseJobHandler):
    kind = ACCOUNT_FIT_JOB

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        async def work() -> JobOutcome:
            payload = job.request_json
            frozen = validated_batch_frozen_inputs(payload["frozen_inputs"])
            if payload.get("source_manifest_hash") != frozen.manifest_hash:
                raise InvestorError(
                    "historical_manifest_unavailable", "冻结账户输入与授权摘要不匹配"
                )
            request = AccountFitRequest.model_validate(payload["client_request"])
            validate_batch_request_mode(frozen, request)
            completed: dict[str, Any] = {}
            failed_items: list[dict[str, Any]] = []
            for item in request.items:
                try:
                    async with session_scope() as session:
                        await PortfolioService(session)._reserve()
                        repo = PipelineRunRepository(session)
                        await repo.record_metadata(job.run_id, {}, lease=job.lease)
                        await check_enabled(session, self.settings)
                        await authorized_run(session, job.run_id)
                        fit = await AccountFitService(session).save(
                            frozen, item, operation_id=request.operation_id
                        )
                        completed[str(item.listing_ref.id)] = {
                            "fit_ref": fit.reference.model_dump(mode="json"),
                            "input_hash": fit.input_hash,
                        }
                        await repo.record_metadata(
                            job.run_id,
                            {"completed": completed, "failed_items": failed_items},
                            lease=job.lease,
                        )
                except FitItemEvaluationError:
                    failed_items.append(
                        {
                            "listing_ref": item.listing_ref.model_dump(mode="json"),
                            "reason_code": "evaluation_failed",
                        }
                    )
            async with session_scope() as session:
                await PortfolioService(session)._reserve()
                repo = PipelineRunRepository(session)
                await repo.record_metadata(job.run_id, {}, lease=job.lease)
                await check_enabled(session, self.settings)
                metadata: dict[str, object] = {"completed": completed, "failed_items": failed_items}
                if failed_items:
                    await repo.mark_partial(
                        job.run_id, "item_evaluation_failed", metadata, lease=job.lease
                    )
                else:
                    await repo.mark_success(job.run_id, metadata, lease=job.lease)
            return "partial" if failed_items else "completed"

        return await ctx.run_with_heartbeat(job.lease, work)

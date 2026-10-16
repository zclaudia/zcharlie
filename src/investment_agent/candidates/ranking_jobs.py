"""Admission, leased evaluation and read-only views for account rankings."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.candidates.lifecycle import CandidatePolicy
from investment_agent.candidates.ranking import (
    FinancialStatus,
    RankingCursorError,
    RankingPage,
    build_ranking_entries,
    page_ranking_entries,
)
from investment_agent.candidates.ranking_contracts import (
    RANKING_POLICY_VERSION,
    PerListingTradeAssumptions,
    RankingEntry,
    RankingRequest,
    RankingSnapshot,
    RankingSourcePolicy,
    SourceManifest,
    canonical_universe_hash,
)
from investment_agent.candidates.ranking_sources import (
    MAX_SOURCE_MEMBERS,
    SourceManifestCapacityError,
    freeze_candidate_sources,
    resolve_source_runs,
)
from investment_agent.config import Settings
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.portfolio.calculations import TradingRuleResolver
from investment_agent.portfolio.service import PortfolioService, digest
from investment_agent.portfolio.suitability import AccountFitService, CurrentLimits, source_semantic
from investment_agent.portfolio.suitability_calculations import FitItemEvaluationError
from investment_agent.portfolio.suitability_contracts import (
    FitRequestItem,
    FrozenAccountInputs,
    TradeAssumptions,
)
from investment_agent.portfolio.suitability_inputs import (
    SuitabilityInputService,
    resolve_buy_action,
)
from investment_agent.portfolio.suitability_jobs import (
    CANDIDATE_RANK_JOB,
    AdmissionReceipt,
    admit_operation,
    authorized_run,
    check_enabled,
    historical_payload,
    run_view,
    validate_batch_request_mode,
    validated_batch_frozen_inputs,
)
from investment_agent.settings.loader import load_settings
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

KNOWN_TTL_SECONDS = 300
UNKNOWN_TTL_SECONDS = 30


def _supported_ranking_policy_version(
    payload: dict[str, Any],
) -> Literal["account_round_robin_v1"]:
    version = payload.get("ranking_policy_version", RANKING_POLICY_VERSION)
    if version != RANKING_POLICY_VERSION:
        raise ValueError(f"unsupported candidate ranking policy version: {version!r}")
    return RANKING_POLICY_VERSION


def _validated_source_manifest(payload: dict[str, Any]) -> SourceManifest:
    manifest = SourceManifest.model_validate(payload["source_manifest"])
    if payload.get("source_manifest_hash") != manifest.manifest_hash:
        raise InvestorError("historical_manifest_unavailable", "冻结候选来源与授权摘要不匹配")
    return manifest


def _ranking_result_has_unknown(entries: Sequence[RankingEntry]) -> bool:
    return any(
        entry.financial_status == "unknown"
        or any(check.state == "unknown" for check in entry.checks)
        for entry in entries
    )


class RankingView(Contract):
    status: Literal["available", "missing"]
    snapshot: RankingSnapshot | None
    entries: tuple[RankingEntry, ...]
    count: int
    next_cursor: str | None
    current_limits: CurrentLimits | None


def _source_state(manifest: SourceManifest) -> dict[str, Any]:
    """Drop only the wall-clock snapshot timestamp from source comparisons."""

    return {
        "candidate_policy": manifest.candidate_policy.model_dump(mode="json"),
        "source_runs": [run.model_dump(mode="json") for run in manifest.source_runs],
        "members": [member.model_dump(mode="json") for member in manifest.members],
        "exclusions": [item.model_dump(mode="json") for item in manifest.exclusions],
        "source_issues": [issue.model_dump(mode="json") for issue in manifest.source_issues],
        "coverage": manifest.coverage,
        "total_unique_count": manifest.total_unique_count,
        "processed_count": manifest.processed_count,
        "unprocessed_count": manifest.unprocessed_count,
    }


def _ranking_change_reasons(
    previous: RankingSnapshot,
    previous_frozen: FrozenAccountInputs,
    previous_request: RankingRequest,
    manifest: SourceManifest,
    frozen: FrozenAccountInputs,
    request: RankingRequest,
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if digest(_source_state(previous.source_manifest)) != digest(_source_state(manifest)):
        reasons.add("candidate_sources_changed")
    if previous_frozen.snapshot.reference != frozen.snapshot.reference:
        reasons.add("portfolio_snapshot_changed")
    if previous_frozen.valuation_binding != frozen.valuation_binding:
        reasons.add("valuation_changed")
    if (
        previous_frozen.mandate.id != frozen.mandate.id
        or previous_frozen.mandate.revision != frozen.mandate.revision
        or previous_frozen.mandate.portfolio_constraints != frozen.mandate.portfolio_constraints
        or previous_frozen.mandate.hard_constraints != frozen.mandate.hard_constraints
    ):
        reasons.add("mandate_changed")
    previous_purpose_hash = (
        previous_frozen.purpose_version.semantic_hash
        if previous_frozen.purpose_version is not None
        else None
    )
    current_purpose_hash = (
        frozen.purpose_version.semantic_hash if frozen.purpose_version is not None else None
    )
    if (
        previous_frozen.purpose_binding != frozen.purpose_binding
        or previous_purpose_hash != current_purpose_hash
    ):
        reasons.add("purposes_changed")
    if tuple(map(source_semantic, previous_frozen.listing_sources)) != tuple(
        map(source_semantic, frozen.listing_sources)
    ):
        reasons.add("listing_sources_changed")
    if previous_request.source_policy != request.source_policy:
        reasons.add("source_policy_changed")
    if previous_request.trade_assumptions != request.trade_assumptions:
        reasons.add("trade_assumptions_changed")
    return tuple(sorted(reasons))


def _trade_assumptions(
    request: RankingRequest,
    frozen: FrozenAccountInputs,
    listing_ref: ObjectRef,
    action: Literal["open", "add", "hold"],
) -> TradeAssumptions | None:
    if action == "hold":
        return None
    policy = request.trade_assumptions
    if isinstance(policy, PerListingTradeAssumptions):
        by_listing = {item.listing_ref: item.assumptions for item in policy.items}
        return by_listing[listing_ref]
    source = next(item for item in frozen.listing_sources if item.listing_ref == listing_ref)
    execution_price = source.price.price
    unknown: list[Literal["execution_price", "fixed_fee", "fee_rate"]] = [
        name for name in policy.unknown_fields
    ]
    if execution_price is None:
        unknown.append("execution_price")
    return TradeAssumptions(
        execution_price=execution_price,
        fixed_fee=policy.fixed_fee,
        fee_rate=policy.fee_rate,
        assumption_source=policy.assumption_source,
        unknown_fields=tuple(unknown),
    )


def _validate_assumption_coverage(
    request: RankingRequest, manifest: SourceManifest, frozen: FrozenAccountInputs
) -> None:
    policy = request.trade_assumptions
    if not isinstance(policy, PerListingTradeAssumptions):
        return
    required = {
        member.listing_ref
        for member in manifest.members
        if resolve_buy_action(frozen.snapshot, member.listing_ref) in {"open", "add"}
    }
    provided = {item.listing_ref for item in policy.items}
    if provided != required:
        raise InvestorError(
            "invalid_assumptions",
            "逐股费用假设必须且只能覆盖持仓状态已确认的建仓或加仓标的",
        )


def _derive_frozen_action_partition(
    request: RankingRequest, frozen: FrozenAccountInputs, manifest: SourceManifest
) -> tuple[list[dict[str, Any]], list[int]]:
    """Rebuild derived worker actions from the three authorized frozen inputs."""
    _validate_assumption_coverage(request, manifest, frozen)
    expanded_items: list[dict[str, Any]] = []
    unknown_action_listing_ids: list[int] = []
    for member in manifest.members:
        listing_id = int(member.listing_ref.id)
        raw_action = resolve_buy_action(frozen.snapshot, member.listing_ref)
        if raw_action not in {"open", "add", "hold"}:
            unknown_action_listing_ids.append(listing_id)
            continue
        action = cast(Literal["open", "add", "hold"], raw_action)
        expanded_items.append(
            FitRequestItem(
                listing_ref=member.listing_ref,
                action=action,
                trade_assumptions=_trade_assumptions(request, frozen, member.listing_ref, action),
            ).model_dump(mode="json")
        )
    return expanded_items, unknown_action_listing_ids


def _validate_frozen_action_partition(
    payload: dict[str, Any],
    request: RankingRequest,
    frozen: FrozenAccountInputs,
    manifest: SourceManifest,
) -> tuple[tuple[FitRequestItem, ...], frozenset[int]]:
    expected_items, expected_unknown_ids = _derive_frozen_action_partition(
        request, frozen, manifest
    )
    if (
        payload.get("expanded_items") != expected_items
        or payload.get("unknown_action_listing_ids") != expected_unknown_ids
    ):
        raise InvestorError("historical_manifest_unavailable", "冻结排序动作与授权输入不匹配")
    return (
        tuple(FitRequestItem.model_validate(item) for item in expected_items),
        frozenset(expected_unknown_ids),
    )


async def _freeze_ranking(
    session: AsyncSession,
    request: RankingRequest,
    *,
    owner: str,
    settings: Settings,
    rules: TradingRuleResolver | None,
    as_of: datetime,
) -> dict[str, Any]:
    if request.historical_replay is not None:
        prior = await historical_payload(
            session,
            request.historical_replay,
            owner=owner,
            kind=CANDIDATE_RANK_JOB,
        )
        try:
            original = RankingRequest.model_validate(prior["client_request"])
            frozen = validated_batch_frozen_inputs(prior["frozen_inputs"]).model_copy(
                update={"mode": "historical_replay"}
            )
            manifest = _validated_source_manifest(prior)
            policy_version = _supported_ranking_policy_version(prior)
            _validate_frozen_action_partition(prior, original, frozen, manifest)
        except (TypeError, ValueError, KeyError) as exc:
            raise InvestorError(
                "historical_manifest_unavailable", "历史排序清单内容或版本校验失败"
            ) from exc
        excluded = {"operation_id", "mode", "historical_replay"}
        if request.model_dump(mode="json", exclude=excluded) != original.model_dump(
            mode="json", exclude=excluded
        ):
            raise InvestorError("historical_manifest_unavailable", "历史回放输入必须与原命令一致")
        return {
            "frozen_inputs": frozen.model_dump(mode="json"),
            "source_manifest": manifest.model_dump(mode="json"),
            "source_manifest_hash": manifest.manifest_hash,
            "ranking_policy_version": policy_version,
            "expanded_items": prior["expanded_items"],
            "unknown_action_listing_ids": prior["unknown_action_listing_ids"],
        }

    source_runs, source_issues = await resolve_source_runs(
        session, as_of=as_of, policy=request.source_policy
    )
    manifest = await freeze_candidate_sources(
        session,
        as_of=as_of,
        source_runs=source_runs,
        source_issues=source_issues,
        candidate_policy=CandidatePolicy.from_settings(await load_settings(session, base=settings)),
        source_cap=MAX_SOURCE_MEMBERS,
    )
    frozen = await SuitabilityInputService(session, owner, rules).freeze(
        request,
        as_of=as_of,
        extra_listings=tuple(member.listing_ref for member in manifest.members),
        validate_actions=False,
    )
    expanded_items, unknown_action_listing_ids = _derive_frozen_action_partition(
        request, frozen, manifest
    )
    return {
        "frozen_inputs": frozen.model_dump(mode="json"),
        "source_manifest": manifest.model_dump(mode="json"),
        "source_manifest_hash": manifest.manifest_hash,
        "ranking_policy_version": RANKING_POLICY_VERSION,
        "expanded_items": expanded_items,
        "unknown_action_listing_ids": unknown_action_listing_ids,
    }


class CandidateRankingAdmission:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        owner: str = "local_owner",
        rules: TradingRuleResolver | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.owner = owner
        self.rules = rules

    async def accept(self, request: RankingRequest) -> AdmissionReceipt:
        async def freeze(as_of: datetime) -> dict[str, Any]:
            return await _freeze_ranking(
                self.session,
                request,
                owner=self.owner,
                settings=self.settings,
                rules=self.rules,
                as_of=as_of,
            )

        return await admit_operation(
            self.session,
            owner=self.owner,
            base=self.settings,
            kind=CANDIDATE_RANK_JOB,
            operation_id=request.operation_id,
            body=request.model_dump(mode="json"),
            freeze=freeze,
        )


class CandidateRankingHandler(BaseJobHandler):
    kind = CANDIDATE_RANK_JOB

    def __init__(self, *, settings: Settings, owner: str = "local_owner") -> None:
        self.settings = settings
        self.owner = owner

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        async def work() -> JobOutcome:
            payload = cast(dict[str, Any], job.request_json)
            policy_version = _supported_ranking_policy_version(payload)
            request = RankingRequest.model_validate(payload["client_request"])
            frozen = validated_batch_frozen_inputs(payload["frozen_inputs"])
            validate_batch_request_mode(frozen, request)
            manifest = _validated_source_manifest(payload)
            items, unknown_ids = _validate_frozen_action_partition(
                payload, request, frozen, manifest
            )
            resolved_actions: dict[int, Literal["open", "add", "hold"] | None] = {}
            assessments = {}
            failed_items: dict[int, str] = {}
            completed: dict[str, Any] = {}
            items_by_id = {int(item.listing_ref.id): item for item in items}
            member_ids = {int(member.listing_ref.id) for member in manifest.members}
            if (
                unknown_ids - member_ids
                or set(items_by_id) - member_ids
                or unknown_ids & set(items_by_id)
            ):
                raise InvestorError(
                    "historical_manifest_unavailable", "冻结排序动作与来源成员不匹配"
                )
            if unknown_ids | set(items_by_id) != member_ids:
                raise InvestorError("historical_manifest_unavailable", "冻结排序动作不完整")
            for member in manifest.members:
                listing_id = int(member.listing_ref.id)
                item = items_by_id.get(listing_id)
                if item is None:
                    resolved_actions[listing_id] = None
                    continue
                resolved_actions[listing_id] = item.action
                try:
                    async with session_scope() as session:
                        await PortfolioService(session, self.owner)._reserve()
                        repo = PipelineRunRepository(session)
                        await repo.record_metadata(job.run_id, {}, lease=job.lease)
                        await check_enabled(session, self.settings, ranking=True)
                        await authorized_run(session, job.run_id, owner=self.owner)
                        assessment = await AccountFitService(session, self.owner).save(
                            frozen, item, operation_id=request.operation_id
                        )
                        assessments[listing_id] = assessment
                        completed[str(listing_id)] = {
                            "fit_ref": assessment.reference.model_dump(mode="json"),
                            "input_hash": assessment.input_hash,
                        }
                        await repo.record_metadata(
                            job.run_id,
                            {
                                "completed": completed,
                                "failed_items": [
                                    {
                                        "listing_ref": ObjectRef(
                                            kind="listing", id=listing_id, revision=1
                                        ).model_dump(mode="json"),
                                        "reason_code": reason,
                                    }
                                    for listing_id, reason in sorted(failed_items.items())
                                ],
                            },
                            lease=job.lease,
                        )
                except FitItemEvaluationError:
                    failed_items[listing_id] = "evaluation_failed"

            ranking_id = uuid5(request.operation_id, "candidate_ranking")
            ranking_ref = ObjectRef(kind="candidate_ranking", id=ranking_id, revision=1)
            entries = build_ranking_entries(
                manifest,
                ranking_ref=ranking_ref,
                resolved_actions=resolved_actions,
                assessments=assessments,
                listing_labels={
                    int(source.listing_ref.id): (source.ticker, source.name)
                    for source in frozen.listing_sources
                },
                failed_items=failed_items,
            )
            now = datetime.now(UTC)
            current_status, current_reasons = await _ranking_current_state(
                self.settings,
                self.owner,
                frozen,
                manifest,
                request.source_policy,
                unknown_result=_ranking_result_has_unknown(entries),
                now=now,
            )
            input_hash = digest(
                {
                    "request": request.model_dump(mode="json"),
                    "frozen_inputs": frozen.model_dump(mode="json"),
                    "source_manifest_hash": manifest.manifest_hash,
                    "ranking_policy_version": policy_version,
                }
            )
            async with session_scope() as session:
                await PortfolioService(session, self.owner)._reserve()
                repo = PipelineRunRepository(session)
                await repo.record_metadata(job.run_id, {}, lease=job.lease)
                await check_enabled(session, self.settings, ranking=True)
                await authorized_run(session, job.run_id, owner=self.owner)
                previous_ref = await _previous_ranking_ref(session, request.account_id)
                change_reasons: tuple[str, ...] = ()
                if previous_ref is not None:
                    previous = await InvestmentAssessmentRepository(session).read_ranking(
                        cast(Literal["local_owner"], self.owner), previous_ref
                    )
                    if previous is not None:
                        previous_frozen, _, previous_request = await _load_original_inputs(
                            session,
                            previous_ref,
                            account_id=request.account_id,
                            manifest_hash=previous.snapshot.manifest_hash,
                        )
                        change_reasons = _ranking_change_reasons(
                            previous.snapshot,
                            previous_frozen,
                            previous_request,
                            manifest,
                            frozen,
                            request,
                        )
                snapshot = RankingSnapshot(
                    id=ranking_id,
                    reference=ranking_ref,
                    input_hash=input_hash,
                    manifest_hash=manifest.manifest_hash,
                    universe_hash=canonical_universe_hash(manifest.members),
                    account_id=request.account_id,
                    created_at=now,
                    as_of=manifest.as_of,
                    source_manifest=manifest,
                    entry_count=manifest.processed_count,
                    coverage=manifest.coverage,
                    current_status=current_status,
                    ranking_policy_version=policy_version,
                    previous_ref=previous_ref,
                    change_reasons=change_reasons,
                )
                await InvestmentAssessmentRepository(session).save_ranking(
                    cast(Literal["local_owner"], self.owner), snapshot, entries
                )
                failed_payload = [
                    {
                        "listing_ref": ObjectRef(
                            kind="listing", id=listing_id, revision=1
                        ).model_dump(mode="json"),
                        "reason_code": reason,
                    }
                    for listing_id, reason in sorted(failed_items.items())
                ]
                metadata: dict[str, object] = {
                    "completed": completed,
                    "failed_items": failed_payload,
                    "ranking_ref": ranking_ref.model_dump(mode="json"),
                    "source_manifest_hash": manifest.manifest_hash,
                }
                if failed_items:
                    await repo.mark_partial(
                        job.run_id,
                        "item_evaluation_failed",
                        metadata,
                        lease=job.lease,
                    )
                else:
                    await repo.mark_success(job.run_id, metadata, lease=job.lease)
            return "partial" if failed_items else "completed"

        return await ctx.run_with_heartbeat(job.lease, work)


async def _previous_ranking_ref(session: AsyncSession, account_id: UUID) -> ObjectRef | None:
    row = await session.scalar(
        select(InvestmentAssessment)
        .join(
            PipelineRun,
            PipelineRun.metadata_json["ranking_ref"]["id"].as_string() == InvestmentAssessment.id,
        )
        .where(
            InvestmentAssessment.owner_id == "local_owner",
            InvestmentAssessment.kind == "candidate_ranking",
            InvestmentAssessment.account_id == str(account_id),
            PipelineRun.request_json["frozen_inputs"]["mode"].as_string() == "live",
        )
        .order_by(InvestmentAssessment.as_of.desc(), InvestmentAssessment.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return ObjectRef(kind="candidate_ranking", id=UUID(row.id), revision=row.revision)


async def _ranking_current_state(
    settings: Settings,
    owner: str,
    frozen: FrozenAccountInputs,
    manifest: SourceManifest,
    source_policy: RankingSourcePolicy,
    *,
    now: datetime,
    rules: TradingRuleResolver | None = None,
    unknown_result: bool = False,
) -> tuple[Literal["valid", "stale"], tuple[str, ...]]:
    reasons: set[str] = set()
    async with session_scope() as session:
        inputs = SuitabilityInputService(session, owner, rules=rules)
        common = await inputs.context(frozen.account_id, as_of=now)
        has_unknown = unknown_result or any(
            source.price.price is None
            or source.sector.code is None
            or (source.trusted_rule is None and source.rule_unknown_reason != "not_applicable")
            for source in frozen.listing_sources
        )
        limits = await AccountFitService(session, owner, rules=rules).input_limits(
            frozen,
            expires_at=manifest.as_of
            + timedelta(seconds=UNKNOWN_TTL_SECONDS if has_unknown else KNOWN_TTL_SECONDS),
            context=common,
            now=now,
        )
        reasons.update(limits.reasons)

        effective_settings = await load_settings(session, base=settings)
        current_runs, issues = await resolve_source_runs(session, as_of=now, policy=source_policy)
        try:
            latest = await freeze_candidate_sources(
                session,
                as_of=now,
                source_runs=current_runs,
                source_issues=issues,
                candidate_policy=CandidatePolicy.from_settings(effective_settings),
                source_cap=MAX_SOURCE_MEMBERS,
            )
        except SourceManifestCapacityError:
            reasons.add("source_capacity_exceeded")
        else:
            if digest(_source_state(latest)) != digest(_source_state(manifest)):
                reasons.add("candidate_sources_changed")
    return ("stale" if reasons else "valid", tuple(sorted(reasons)))


async def read_ranking_view(
    session: AsyncSession,
    account_id: UUID,
    ranking_ref: ObjectRef,
    limit: int = 50,
    bucket: Literal["incomplete", "blocked"] | None = None,
    financial_status: FinancialStatus | None = None,
    cursor: str | None = None,
    settings: Settings | None = None,
    rules: TradingRuleResolver | None = None,
) -> RankingView:
    artifact = await InvestmentAssessmentRepository(session).read_ranking(
        "local_owner", ranking_ref
    )
    if artifact is None or artifact.snapshot.account_id != account_id:
        raise InvestorError("not_found", "该账户排序结果不存在")
    try:
        page: RankingPage = page_ranking_entries(
            artifact.entries,
            ranking_ref=ranking_ref,
            limit=limit,
            bucket_filter=bucket,
            financial_status_filter=financial_status,
            cursor=cursor,
        )
    except RankingCursorError as exc:
        raise InvestorError("invalid_cursor", "排序分页游标无效") from exc
    frozen, source_policy, _ = await _load_original_inputs(
        session,
        ranking_ref,
        account_id=account_id,
        manifest_hash=artifact.snapshot.manifest_hash,
    )
    current_status, reasons = await _ranking_current_state(
        settings or await load_settings(session),
        "local_owner",
        frozen,
        artifact.snapshot.source_manifest,
        source_policy,
        rules=rules,
        unknown_result=_ranking_result_has_unknown(artifact.entries),
        now=datetime.now(UTC),
    )
    limits = CurrentLimits(
        current_status=current_status,
        reasons=reasons,
        refresh_required=bool(reasons),
    )
    return RankingView(
        status="available",
        snapshot=artifact.snapshot,
        entries=page.entries,
        count=page.count,
        next_cursor=page.next_cursor,
        current_limits=limits,
    )


async def _load_original_inputs(
    session: AsyncSession,
    ranking_ref: ObjectRef,
    *,
    account_id: UUID,
    manifest_hash: str,
) -> tuple[FrozenAccountInputs, RankingSourcePolicy, RankingRequest]:
    row = await session.scalar(
        select(PipelineRun).where(
            PipelineRun.kind == CANDIDATE_RANK_JOB,
            PipelineRun.metadata_json["ranking_ref"]["id"].as_string() == str(ranking_ref.id),
        )
    )
    if row is None:
        raise InvestorError("not_found", "排序来源清单不存在")
    await authorized_run(session, row.id)
    request = RankingRequest.model_validate(row.request_json["client_request"])
    if (
        request.account_id != account_id
        or row.request_json.get("source_manifest_hash") != manifest_hash
    ):
        raise InvestorError("not_found", "排序结果与冻结来源清单不匹配")
    return (
        validated_batch_frozen_inputs(row.request_json["frozen_inputs"]),
        request.source_policy,
        request,
    )


__all__ = [
    "CandidateRankingAdmission",
    "CandidateRankingHandler",
    "RankingView",
    "read_ranking_view",
    "run_view",
]

"""Authenticated AG04 foundation endpoints; application service owns semantics."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import AwareDatetime

from investment_agent.api.auth import get_bootstrap_settings, verify_api_key_or_session
from investment_agent.api.deps import session_service
from investment_agent.config import Settings
from investment_agent.evidence.contracts import EvidenceRecord, ToolResult
from investment_agent.investor.common import Contract, Mutation, ObjectRef
from investment_agent.investor.contracts import (
    Case,
    Episode,
    Mandate,
    RecommendationView,
    ResearchPlan,
    ThesisRevision,
)
from investment_agent.investor.enums import CaseStatus
from investment_agent.investor.foundations import (
    CaseDetail,
    CreateCase,
    InvestorError,
    InvestorService,
)
from investment_agent.storage.database import session_scope

router = APIRouter(
    prefix="/investor", tags=["investor"], dependencies=[Depends(verify_api_key_or_session)]
)


service = session_service(InvestorService)
Service = Annotated[InvestorService, Depends(service, scope="function")]


class MandateCreate(Contract):
    candidate: Mandate
    idempotency_key: UUID


class MandateRevise(Contract):
    candidate: Mandate
    mutation: Mutation


class PinRequest(Contract):
    pinned: bool
    mutation: Mutation


class PlanRequest(Contract):
    candidate: ResearchPlan
    mutation: Mutation


class ArtifactRequest(Contract):
    refs: tuple[ObjectRef, ...]
    mutation: Mutation


@router.get("/mandate")
async def mandate(svc: Service, revision: Annotated[int | None, Query(ge=1)] = None) -> Mandate:
    return await svc.get_mandate("local_owner", revision)


@router.post("/mandate", status_code=201)
async def create_mandate(body: MandateCreate, svc: Service) -> Mandate:
    return await svc.create_mandate("local_owner", body.candidate, body.idempotency_key)


@router.post("/mandate/revisions")
async def revise_mandate(body: MandateRevise, svc: Service) -> Mandate:
    return await svc.revise_mandate("local_owner", body.candidate, body.mutation)


@router.get("/cases")
async def cases(
    svc: Service,
    listing_id: Annotated[int | None, Query(ge=1)] = None,
    after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    status: CaseStatus | None = None,
) -> list[Case]:
    rows = await svc.repository.cases(
        "local_owner", listing_id, after=after, limit=limit, status=status.value if status else None
    )
    result = [Case.model_validate(row.payload) for row in rows]
    return [case for case in result if status is None or case.status == status]


@router.post("/cases", status_code=201)
async def create_case(body: CreateCase, svc: Service) -> Case:
    return await svc.create_case("local_owner", body)


@router.get("/cases/{case_id}")
async def case_detail(case_id: UUID, svc: Service) -> CaseDetail:
    return await svc.detail("local_owner", case_id)


@router.patch("/cases/{case_id}/pinned")
async def pin(case_id: UUID, body: PinRequest, svc: Service) -> Case:
    return await svc.set_pinned("local_owner", case_id, body.pinned, body.mutation)


@router.post("/cases/{case_id}/plans")
async def plan(case_id: UUID, body: PlanRequest, svc: Service) -> ResearchPlan:
    return await svc.save_plan("local_owner", case_id, body.candidate, body.mutation)


@router.post("/cases/{case_id}/artifacts")
async def artifacts(case_id: UUID, body: ArtifactRequest, svc: Service) -> Episode:
    return await svc.attach_artifacts("local_owner", case_id, body.refs, body.mutation)


class SourceRequest(Contract):
    ref: ObjectRef
    mutation: Mutation


@router.post("/cases/{case_id}/sources")
async def source(case_id: UUID, body: SourceRequest, svc: Service) -> Case:
    return await svc.link_source("local_owner", case_id, body.ref, body.mutation)


@router.get("/cases/{case_id}/revisions/{revision}")
async def case_revision(case_id: UUID, revision: Annotated[int, Path(ge=1)], svc: Service) -> Case:
    return await svc.get_case("local_owner", case_id, revision)


@router.get("/evidence/{evidence_id}")
async def evidence(
    evidence_id: UUID,
    svc: Service,
    revision: Annotated[int, Query(ge=1)] = 1,
    as_of: AwareDatetime | None = None,
    visible_at: AwareDatetime | None = None,
) -> ToolResult[EvidenceRecord]:
    from investment_agent.evidence.store import EvidenceService

    result = await EvidenceService(svc.session).get_evidence(
        "local_owner",
        ObjectRef(kind="evidence", id=evidence_id, revision=revision),
        as_of or datetime.now(UTC),
        visible_at=visible_at,
    )
    if result.data is None:
        raise HTTPException(status_code=404, detail="Evidence does not exist or is not visible")
    return result


@router.get("/recommendations/{recommendation_id}")
async def recommendation(recommendation_id: UUID, svc: Service, revision: Annotated[int, Query(ge=1)] = 1, at: AwareDatetime | None = None) -> RecommendationView:
    from investment_agent.investor.publication import PublicationService
    return await PublicationService(svc.session).get_recommendation(ObjectRef(kind="recommendation", id=recommendation_id, revision=revision), at=at)


@router.get("/theses/{thesis_id}")
async def thesis(thesis_id: UUID, svc: Service, revision: Annotated[int, Query(ge=1)] = 1) -> ThesisRevision:
    from investment_agent.investor.publication import PublicationService
    return await PublicationService(svc.session).get_thesis(ObjectRef(kind="thesis", id=thesis_id, revision=revision))


@router.get("/drafts/{draft_id}")
async def publication_draft(draft_id: UUID, svc: Service) -> dict[str, object]:
    from investment_agent.investor.publication import PublicationService
    saved = await PublicationService(svc.session).artifact(ObjectRef(kind="draft", id=draft_id, revision=1))
    return saved.payload


@router.get("/publication-checks/{check_id}")
async def publication_check(check_id: UUID, svc: Service) -> dict[str, object]:
    from investment_agent.investor.publication import PublicationService
    saved = await PublicationService(svc.session).artifact(ObjectRef(kind="publication_check", id=check_id, revision=1))
    return saved.payload


# UserControlService is reached only after router authentication and CSRF.
# No actor/authorization source/grant is accepted in these wire requests.
from investment_agent.investor.contracts import Memory, UserDecision  # noqa: E402
from investment_agent.investor.controls import (  # noqa: E402
    ExecuteRequest,
    PrepareRequest,
    Proposal,
    ResolveRequest,
    UserControlService,
    authenticated_request,
)


@router.post("/actions")
async def user_action(body: ExecuteRequest, svc: Service) -> dict[str, object]:
    result = await UserControlService(svc.session).execute(body, authenticated_request(body.parameters))
    return result.model_dump(mode="json")


@router.post("/cases/{case_id}/actions")
async def case_action(case_id: UUID, body: ExecuteRequest, svc: Service) -> dict[str, object]:
    from investment_agent.investor.controls import CaseAction
    if not isinstance(body.parameters, CaseAction) or body.parameters.case_ref.id != case_id:
        raise HTTPException(status_code=422, detail="Action must target this case")
    return await user_action(body, svc)


@router.post("/decisions", status_code=201)
async def save_decision(body: ExecuteRequest, svc: Service) -> UserDecision:
    from investment_agent.investor.controls import DecisionAction
    if not isinstance(body.parameters, DecisionAction):
        raise HTTPException(status_code=422, detail="Expected decision operation")
    result = await UserControlService(svc.session).execute(body, authenticated_request(body.parameters))
    return UserDecision.model_validate(result.model_dump())


@router.get("/decisions")
async def decisions(svc: Service, limit: Annotated[int, Query(ge=1, le=100)] = 100,
    after: UUID | None = None, case_id: UUID | None = None, recommendation_id: UUID | None = None) -> list[dict[str, object]]:
    return await UserControlService(svc.session).list_objects("decision", limit=limit, after=after,
        case_id=case_id, recommendation_id=recommendation_id)


@router.post("/memories", status_code=201)
async def save_memory(body: ExecuteRequest, svc: Service) -> Memory:
    from investment_agent.investor.controls import MemoryAction
    if not isinstance(body.parameters, MemoryAction):
        raise HTTPException(status_code=422, detail="Expected memory_save operation")
    result = await UserControlService(svc.session).execute(body, authenticated_request(body.parameters))
    return Memory.model_validate(result.model_dump())


@router.get("/memories")
async def memories(svc: Service, limit: Annotated[int, Query(ge=1, le=100)] = 100,
    case_id: UUID | None = None, listing_id: Annotated[int | None, Query(ge=1)] = None,
    active_only: bool = False, after: UUID | None = None,
    status: Literal["active", "revoked", "expired"] | None = None) -> tuple[Memory, ...]:
    refs: tuple[ObjectRef, ...] = ()
    if case_id is not None:
        case = await svc.get_case("local_owner", case_id)
        refs = (ObjectRef(kind="case", id=case.id, revision=case.revision), case.listing_ref)
    if listing_id is not None:
        refs = (*refs, ObjectRef(kind="listing", id=listing_id, revision=1))
    return await UserControlService(svc.session).get_memories(scope_refs=refs, active_only=active_only, status=status, after=after, limit=limit)


@router.get("/memories/{memory_id:uuid}")
async def memory_detail(memory_id: UUID, svc: Service, revision: Annotated[int, Query(ge=1)] = 1) -> Memory:
    control = UserControlService(svc.session)
    return Memory.model_validate(await control._object(
        ObjectRef(kind="memory", id=memory_id, revision=revision), "memory"))


@router.post("/memories/{memory_id}/revoke")
async def revoke_memory(memory_id: UUID, body: ExecuteRequest, svc: Service) -> Memory:
    from investment_agent.investor.controls import RevokeAction
    if not isinstance(body.parameters, RevokeAction) or body.parameters.memory_ref.id != memory_id:
        raise HTTPException(status_code=422, detail="Expected revoke for this memory")
    result = await UserControlService(svc.session).execute(body, authenticated_request(body.parameters))
    return Memory.model_validate(result.model_dump())


@router.post("/proposals", status_code=201)
async def prepare_proposal(body: PrepareRequest, svc: Service) -> Proposal:
    return await UserControlService(svc.session).prepare(body)


@router.get("/proposals")
async def proposals(svc: Service, limit: Annotated[int, Query(ge=1, le=100)] = 100,
    after: UUID | None = None, status: Literal["pending", "approved", "declined"] | None = None) -> list[dict[str, object]]:
    return await UserControlService(svc.session).list_objects("proposal", limit=limit, after=after, status=status)


@router.get("/proposals/{proposal_id}")
async def proposal(proposal_id: UUID, svc: Service, revision: Annotated[int | None, Query(ge=1)] = None) -> Proposal:
    return await UserControlService(svc.session).get_proposal(proposal_id, revision)


@router.post("/proposals/{proposal_id}/resolve")
async def resolve_proposal(proposal_id: UUID, body: ResolveRequest, svc: Service) -> Proposal:
    return await UserControlService(svc.session).resolve(proposal_id, body, authenticated_request(body.parameters))


class ManualReviewRequest(Contract):
    mutation: Mutation
    reason: str = "手动复核"


class NotificationReadRequest(Contract):
    read: bool = True
    mutation: Mutation


@router.post("/cases/{case_id}/reviews", status_code=202)
async def manual_review(case_id: UUID, body: ManualReviewRequest, svc: Service, base: Annotated["Settings", Depends(get_bootstrap_settings)]) -> dict[str, Any]:
    from investment_agent.investor.monitoring import MonitoringService
    return await MonitoringService(svc.session, base).manual(case_id, body.mutation.idempotency_key, expected_revision=body.mutation.expected_revision, reason=body.reason)


@router.get("/notifications")
async def notifications(svc: Service, base: Annotated["Settings", Depends(get_bootstrap_settings)], after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50, unread: bool = False) -> list[dict[str, Any]]:
    from investment_agent.investor.monitoring import MonitoringService
    return await MonitoringService(svc.session, base).notifications(after=after, limit=limit, unread=unread)


@router.patch("/notifications/{sequence}/read")
async def notification_read(sequence: int, body: NotificationReadRequest, svc: Service, base: Annotated["Settings", Depends(get_bootstrap_settings)]) -> dict[str, Any]:
    from investment_agent.investor.monitoring import MonitoringService
    return await MonitoringService(svc.session, base).read(sequence, expected_revision=body.mutation.expected_revision, read=body.read, key=body.mutation.idempotency_key)


@router.get("/cases/{case_id}/reviews")
async def reviews(case_id: UUID, svc: Service, after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from investment_agent.investor.monitoring import MonitoringService
    from investment_agent.storage.models.monitoring import ReviewCycle
    await svc.get_case("local_owner", case_id)
    stmt = select(ReviewCycle).where(ReviewCycle.case_id == str(case_id))
    if after:
        stmt = stmt.where(ReviewCycle.id > str(after))
    rows = (await svc.session.scalars(stmt.order_by(ReviewCycle.id).limit(limit))).all()
    return [MonitoringService.cycle_view(row) for row in rows]


@router.get("/discovery/batches")
async def discovery_batches(svc: Service, base: Annotated[Settings, Depends(get_bootstrap_settings)],
    after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
    from investment_agent.investor.discovery import DiscoveryService
    return await DiscoveryService(svc.session, base).batches(after=after, limit=limit)


@router.get("/discovery/batches/{batch_id}")
async def discovery_batch(batch_id: UUID, svc: Service, base: Annotated[Settings, Depends(get_bootstrap_settings)]) -> dict[str, Any]:
    from sqlalchemy import select

    from investment_agent.agent.tasks import TaskService
    from investment_agent.investor.discovery import DiscoveryService
    from investment_agent.storage.models.discovery import DiscoveryBatch
    row = await svc.session.scalar(select(DiscoveryBatch).where(DiscoveryBatch.id == str(batch_id), DiscoveryBatch.owner_id == "local_owner"))
    if row is None:
        raise InvestorError("not_found", "Discovery batch does not exist")
    result = DiscoveryService.view(row)
    states = {}
    for task_id in [row.task_id, *[lead.get("research_task_id") for lead in row.payload["leads"]]]:
        if task_id:
            task = await TaskService(svc.session).get_task(UUID(task_id))
            states[task_id] = {"status": task.status, "stop_reason": task.stop_reason, "revision": task.revision}
    return {**result, "current_task_states": states}


@router.get("/recommendations/{recommendation_id}/body")
async def recommendation_render(recommendation_id: UUID, svc: Service,
    revision: Annotated[int, Query(ge=1)] = 1) -> dict[str, Any]:
    from investment_agent.investor.presentation import recommendation_body
    return await recommendation_body(svc, ObjectRef(kind="recommendation", id=recommendation_id, revision=revision))


@router.get("/calculations/{calculation_id}")
async def calculation_detail(calculation_id: UUID, svc: Service,
    revision: Annotated[int, Query(ge=1)] = 1) -> dict[str, Any]:
    from investment_agent.evidence.contracts import CalculationRecord
    from investment_agent.storage.models.evidence import SavedCalculation
    row = await svc.session.get(SavedCalculation, (str(calculation_id), revision))
    if row is None or row.owner_id != "local_owner":
        raise InvestorError("not_found", "Calculation does not exist")
    return CalculationRecord.model_validate(row.payload).model_dump(mode="json")


@router.get("/cases/{case_id}/recommendations")
async def case_recommendations(case_id: UUID, svc: Service,
    after: UUID | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from investment_agent.storage.models.publication import PublicationArtifact
    await svc.get_case("local_owner", case_id)
    stmt = select(PublicationArtifact).where(PublicationArtifact.case_id == str(case_id),
        PublicationArtifact.owner_id == "local_owner", PublicationArtifact.kind == "recommendation")
    if after:
        stmt = stmt.where(PublicationArtifact.id > str(after))
    rows = (await svc.session.scalars(stmt.order_by(PublicationArtifact.id, PublicationArtifact.revision).limit(limit))).all()
    return [{"ref": ObjectRef(kind="recommendation", id=UUID(row.id), revision=row.revision).model_dump(mode="json"),
        "created_at": row.payload["created_at"], "as_of": row.payload["as_of"]} for row in rows]


@router.get("/cases/{case_id}/research")
async def case_research(case_id: UUID, svc: Service,
    after: UUID | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
    """Committed public progress; immutable saved text already used NumericResolver.

    Never rerender progress using a later task observation window or current facts.
    No prompts, checkpoints, provider responses or model history are projected.
    """
    from sqlalchemy import and_, func, or_, select

    from investment_agent.agent.contracts import Task
    from investment_agent.storage.models.agent import AgentTask, ToolAction
    case = await svc.get_case("local_owner", case_id)
    matching_scope = []
    for field in ("context_refs", "base_refs"):
        scope = func.json_each(AgentTask.payload["binding"][field]).table_valued("value")
        matching_scope.append(select(scope.c.value).where(or_(
            and_(func.json_extract(scope.c.value, "$.kind") == "case", func.json_extract(scope.c.value, "$.id") == str(case_id)),
            and_(func.json_extract(scope.c.value, "$.kind") == "listing", func.json_extract(scope.c.value, "$.id") == case.listing_ref.id),
        )).correlate(AgentTask).exists())
    stmt = select(ToolAction, AgentTask).join(AgentTask, AgentTask.id == ToolAction.task_id).where(
        AgentTask.owner_id == "local_owner", ToolAction.state == "committed",
        ToolAction.parameters["tool"].as_string() == "save_research_progress", or_(*matching_scope))
    if after:
        stmt = stmt.where(ToolAction.operation_id > str(after))
    rows = (await svc.session.execute(stmt.order_by(ToolAction.operation_id).limit(limit))).all()
    result = []
    for action, saved_task in rows:
        task = Task.model_validate(saved_task.payload)
        bound_refs = (*task.binding.context_refs, *task.binding.base_refs)
        if not any((ref.kind == "case" and ref.id == case_id) or ref == case.listing_ref for ref in bound_refs):
            continue
        if action.result is None:
            continue
        result.append({"id": action.operation_id, "task_id": saved_task.id, "task_status": task.status,
            "admission_as_of": task.binding.as_of.isoformat(), "progress": action.result.get("progress", {}),
            "text": action.result.get("text"), "refs": action.result.get("refs", [])})
        if len(result) >= limit:
            break
    return result


@router.get("/monitors/{monitor_id}")
async def monitor_detail(monitor_id: UUID, svc: Service,
    revision: Annotated[int, Query(ge=1)] = 1) -> dict[str, Any]:
    from investment_agent.investor.publication import PublicationService
    saved = await PublicationService(svc.session).artifact(ObjectRef(kind="monitor", id=monitor_id, revision=revision))
    return saved.payload


@router.get("/reviews/{review_id}")
async def review_detail(review_id: UUID, svc: Service,
    revision: Annotated[int, Query(ge=1)] = 1) -> dict[str, Any]:
    from investment_agent.investor.publication import PublicationService
    saved = await PublicationService(svc.session).artifact(ObjectRef(kind="review", id=review_id, revision=revision))
    return saved.payload


@router.get("/cases/{case_id}/publication-events")
async def publication_events(case_id: UUID, svc: Service,
    after: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from investment_agent.storage.models.publication import PublicationArtifact, PublicationEvent
    await svc.get_case("local_owner", case_id)
    stmt = select(PublicationEvent).join(PublicationArtifact,
        PublicationArtifact.id == PublicationEvent.recommendation_id).where(
        PublicationArtifact.case_id == str(case_id), PublicationArtifact.kind == "recommendation",
        PublicationArtifact.owner_id == "local_owner", PublicationEvent.owner_id == "local_owner",
        PublicationEvent.sequence > after).order_by(PublicationEvent.sequence).limit(limit)
    return [{"sequence": row.sequence, **row.payload} for row in (await svc.session.scalars(stmt)).all()]


def _positive_usd(quantity: Any) -> bool:
    return bool(quantity is not None and quantity.value is not None and quantity.value > 0
        and quantity.unit in {"USD", "currency"} and quantity.currency in {None, "USD"}
        and (quantity.unit == "USD" or quantity.currency == "USD"))


@router.get("/readiness")
async def readiness(svc: Service, base: Annotated[Settings, Depends(get_bootstrap_settings)]) -> dict[str, Any]:
    """Read-only configuration diagnostics; no import, credential projection or network."""
    from investment_agent.agent.budget import BudgetService
    from investment_agent.agent.model import AgentConfigurationError, AgentModelConfig
    from investment_agent.settings.loader import load_settings

    settings = await load_settings(svc.session, base=base)
    config = AgentModelConfig.from_settings(settings, 0)
    issues: list[str] = []
    for key, label in (("agent_base_url", "独立 Agent 端点"), ("agent_model", "独立 Agent 模型")):
        if not config.values[key]:
            issues.append(f"{label}未配置")
    if not config.credential.get_secret_value():
        issues.append("独立 Agent API Key 未配置或不可解密；在加密设置页修正")
    if not issues:
        try:
            config.validate_ready()
        except AgentConfigurationError as exc:
            issues.append(str(exc))
    if BudgetService.prices(settings)[0] is None:
        issues.append("价格未知：须核验 USD 输入/输出价格，并绑定报价版本、模型和完整配置指纹")
    if not settings.investor_agent_enabled:
        issues.append("Agent 未启用；历史仍可读取")
    try:
        mandate = await svc.get_mandate("local_owner")
    except InvestorError as exc:
        if exc.detail.code != "not_found":
            raise
        mandate = None
    interactive = list(issues)
    background = list(issues)
    if not settings.investor_agent_background_enabled:
        background.append("运行设置后台开关未启用")
    if mandate is None:
        # Existing interactive execution can run without a mandate using other
        # bounded request/token/time limits. This diagnostic does not invent a USD cap.
        interactive.append("尚未设置投资约定：交互金额上限未知，仍受请求/token/时间限额")
        background.append("尚无投资约定与后台授权")
        review, exploration = list(background), list(background)
    else:
        auth = mandate.authorization
        for name, quantity in (("每日共享总额", auth.daily_budget), ("单任务全生命周期金额", auth.per_task_budget)):
            if not _positive_usd(quantity):
                message = f"{name}缺少已知正数 USD 上限；不推测汇率"
                interactive.append(message)
                background.append(message)
        if not mandate.scope_refs:
            interactive.append("尚未明确交互证券范围")
        if not auth.background_enabled:
            background.append("投资约定后台授权未启用")
        if not auth.exploration_scope_refs:
            background.append("尚未明确后台证券范围")
        review, exploration = list(background), list(background)
        if auth.review_daily_budget is None:
            if auth.exploration_daily_budget is not None:
                review.append("review_budget_unavailable：须独立设置复核日金额")
            else:
                review.append("复核日金额未单独配置：旧约定仅受共享总额，建议明确独立上限")
        elif not _positive_usd(auth.review_daily_budget):
            review.append("复核日金额缺少已知正数 USD 上限")
        if not _positive_usd(auth.exploration_daily_budget):
            exploration.append("探索未授权或额度不可用：须明确正数 USD 探索日金额")
        if min(auth.max_leads_per_batch, settings.investor_agent_max_leads_per_batch) == 0:
            exploration.append("当前线索上限为0，探索关闭")
        if min(auth.max_new_tasks_per_batch, settings.investor_agent_max_new_tasks_per_batch) == 0:
            exploration.append("当前新研究任务上限为0，不创建新研究任务")
    return {"enabled": settings.investor_agent_enabled,
        "portfolio_ledger_enabled": settings.portfolio_ledger_enabled,
        "runtime_background_enabled": settings.investor_agent_background_enabled,
        "mandate_background_enabled": mandate.authorization.background_enabled if mandate else False,
        "model_config_fingerprint": config.version, "network_verified": False,
        "local_configuration_issues": issues, "interactive_issues": interactive,
        "review_issues": review, "exploration_issues": exploration,
        "runtime_max_leads": settings.investor_agent_max_leads_per_batch,
        "runtime_max_new_tasks": settings.investor_agent_max_new_tasks_per_batch,
        "effective_max_leads": min(settings.investor_agent_max_leads_per_batch,
            mandate.authorization.max_leads_per_batch) if mandate else 0,
        "effective_max_new_tasks": min(settings.investor_agent_max_new_tasks_per_batch,
            mandate.authorization.max_new_tasks_per_batch) if mandate else 0,
        "output_mode": settings.agent_output_mode,
        "supports_json_object": settings.agent_supports_json_object,
        "supports_json_schema": settings.agent_supports_json_schema}


@router.get("/operations")
async def operations(settings: Annotated[Settings, Depends(get_bootstrap_settings)]) -> dict[str, Any]:
    """Local readiness and operating counts; no credential/destination disclosure."""
    from sqlalchemy import func, select

    from investment_agent.agent.operations import metrics
    from investment_agent.investor.notifications import configuration
    from investment_agent.storage.models.agent import BudgetEntry
    from investment_agent.storage.models.monitoring import NotificationDelivery, ReviewCycle
    async with session_scope() as session:
        result = await metrics(session)
        from investment_agent.agent.operations import supplemental_update_metrics
        result["supplemental_updates"] = await supplemental_update_metrics(session)
        result["external_notifications_ready"] = await configuration(session, settings) is not None
        result["delivery_states"] = dict((await session.execute(select(NotificationDelivery.state, func.count()).group_by(NotificationDelivery.state))).tuples().all())
        result["budget_blocked"] = await session.scalar(select(func.count()).select_from(ReviewCycle).where(ReviewCycle.state == "blocked", ReviewCycle.payload["pending_reason"].as_string().like("%budget%")))
        result["unknown_usage"] = await session.scalar(select(func.count()).select_from(BudgetEntry).where(BudgetEntry.state == "unknown"))
        return result

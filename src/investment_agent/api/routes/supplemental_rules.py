"""Authenticated API for the independent P2 supplemental-rule workflow."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import (
    get_bootstrap_settings,
    get_settings,
    verify_api_key_or_session,
)
from investment_agent.api.deps import session_service
from investment_agent.api.schemas.supplemental_rules import (
    EvaluationAdmissionResponse,
    EventMutationResponse,
    ResearchEventCreateRequest,
    ResearchEventRevisionRequest,
    RuleCreateRequest,
    RuleDecisionRequest,
    RuleMutationResponse,
    RuleRevisionRequest,
    RuleValidationRequest,
    ValidationResponse,
)
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.supplemental_rules import (
    ResearchEventRevision,
    SupplementalRuleEvent,
    SupplementalRuleHead,
)
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.supplemental_rules.admission import SupplementalRuleAdmission
from investment_agent.supplemental_rules.contracts import (
    Context,
    EvaluationRequest,
    MutationReceipt,
    ResearchEvent,
)
from investment_agent.supplemental_rules.events import SupplementalEventService
from investment_agent.supplemental_rules.lifecycle import SupplementalRuleLifecycle
from investment_agent.supplemental_rules.read_snapshot import ensure_read_snapshot
from investment_agent.supplemental_rules.run_views import SupplementalRunView, supplemental_run_view
from investment_agent.supplemental_rules.service import SupplementalRuleService
from investment_agent.supplemental_rules.views import (
    ApplicationView,
    CurrentApplicationsView,
    ResearchEventListView,
    RuleHeadView,
    RuleHistoryView,
    RuleListView,
    SupplementalRuleContextView,
)

router = APIRouter(
    tags=["supplemental-rules"],
    dependencies=[Depends(verify_api_key_or_session)],
)


def _utc_db_timestamp(value: datetime) -> datetime:
    """SQLite drops timezone metadata; persisted aware timestamps are UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


session_dep = session_service(lambda session: session)
Session = Annotated[AsyncSession, Depends(session_dep, scope="function")]
EffectiveSettings = Annotated[Settings, Depends(get_settings)]
BootstrapSettings = Annotated[Settings, Depends(get_bootstrap_settings)]
PageLimit = Annotated[int, Query(ge=1, le=100)]


@router.get("/supplemental-rule-context")
async def supplemental_context(
    session: Session, settings: EffectiveSettings
) -> SupplementalRuleContextView:
    """Return safe capabilities and registry state without requiring an account."""
    await ensure_read_snapshot(session)
    registry = await SupplementalRulesRepository(session).get_registry_head("local_owner")
    from investment_agent.storage.models.reference import Market

    listings = list(
        (
            await session.scalars(
                select(Listing.id)
                .join(Market, Market.code == Listing.market_code)
                .where(Market.code.in_({"SH", "SZ", "BJ", "SSE", "SZSE", "BSE"}))
                .order_by(Listing.id)
                .limit(101)
            )
        ).all()
    )
    from investment_agent.factors.catalog import DEFAULT_REGISTRY

    return SupplementalRuleContextView.model_validate(
        {
            "enabled": settings.supplemental_rules_enabled,
            "registry_revision": registry.registry_revision,
            "rules_generation": registry.rules_generation,
            "events_generation": registry.events_generation,
            "contexts": ["company_research", "candidate", "account_fit", "holding_review"],
            "rule_types": ["metric_condition", "research_requirement", "event_impact"],
            "effects": ["annotate", "require_evidence", "require_review", "limit_conclusion"],
            "factors": DEFAULT_REGISTRY.catalogue(),
            "listing_refs": [
                ObjectRef(kind="listing", id=value, revision=1) for value in listings[:100]
            ],
            "listing_scan_truncated": len(listings) > 100,
            "company_identity_available": await session.scalar(select(Company.id).limit(1))
            is not None,
            "source_capabilities": {
                "saved_evidence": True,
                "trusted_research_events": False,
                "user_report_events": True,
            },
        }
    )


@router.post("/supplemental-rules", status_code=201)
async def create_rule(
    body: RuleCreateRequest, session: Session, settings: BootstrapSettings
) -> RuleMutationResponse:
    return await SupplementalRuleLifecycle(session, settings=settings).create(body)


@router.post("/supplemental-rules/{rule_id}/revisions", status_code=201)
async def revise_rule(
    rule_id: UUID, body: RuleRevisionRequest, session: Session, settings: BootstrapSettings
) -> RuleMutationResponse:
    return await SupplementalRuleLifecycle(session, settings=settings).create_revision(
        rule_id, body
    )


@router.post("/supplemental-rules/{rule_id}/validations")
async def validate_rule(
    rule_id: UUID, body: RuleValidationRequest, session: Session, settings: BootstrapSettings
) -> ValidationResponse:
    return await SupplementalRuleLifecycle(session, settings=settings).validate(rule_id, body)


@router.post("/supplemental-rules/{rule_id}/decisions")
async def decide_rule(
    rule_id: UUID, body: RuleDecisionRequest, session: Session, settings: BootstrapSettings
) -> RuleMutationResponse:
    return await SupplementalRuleLifecycle(session, settings=settings).decide(rule_id, body)


@router.post("/research-events", status_code=201)
async def create_event(
    body: ResearchEventCreateRequest, session: Session, settings: BootstrapSettings
) -> EventMutationResponse:
    return await SupplementalEventService(session, settings=settings).create(body)


@router.post("/research-events/{event_id}/revisions", status_code=201)
async def revise_event(
    event_id: UUID,
    body: ResearchEventRevisionRequest,
    session: Session,
    settings: BootstrapSettings,
) -> EventMutationResponse:
    return await SupplementalEventService(session, settings=settings).revise(event_id, body)


@router.post("/supplemental-rule-evaluations", status_code=202)
async def admit_evaluation(
    body: EvaluationRequest, session: Session, settings: BootstrapSettings
) -> EvaluationAdmissionResponse:
    admission = await SupplementalRuleAdmission(session, settings=settings).accept_evaluation(body)
    from investment_agent.storage.models.investor import InvestorMutation

    saved = await session.get(InvestorMutation, ("local_owner", str(admission.operation_id)))
    receipt = MutationReceipt.model_validate(saved.result["receipt"]) if saved is not None else None
    if receipt is None:
        raise InvestorError("historical_manifest_unavailable", "评估收据保存失败")
    return EvaluationAdmissionResponse(
        receipt=receipt,
        admission=admission,
    )


@router.get("/supplemental-rule-evaluations/runs/{run_id}")
async def get_supplemental_run(
    run_id: int,
    session: Session,
    cursor: str | None = None,
    limit: PageLimit = 100,
) -> SupplementalRunView:
    await ensure_read_snapshot(session)
    return await supplemental_run_view(session, run_id, cursor=cursor, limit=limit)


@router.get("/supplemental-rule-applications/current")
async def current_applications(
    listing_id: Annotated[int, Query(ge=1)],
    context: Context,
    session: Session,
    cursor: str | None = None,
    limit: PageLimit = 100,
) -> CurrentApplicationsView:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise InvestorError("not_found", "证券标的不存在")
    return await SupplementalRuleService(session).current(
        ObjectRef(kind="listing", id=listing_id, revision=1),
        context,
        cursor=cursor,
        limit=limit,
    )


@router.get("/supplemental-rule-applications/{application_id}")
async def exact_application(
    application_id: UUID, session: Session, revision: Annotated[int, Query(ge=1)] = 1
) -> ApplicationView:
    return await SupplementalRuleService(session).exact(
        ObjectRef(kind="rule_application", id=application_id, revision=revision)
    )


@router.get("/supplemental-rules")
async def list_rules(
    session: Session,
    limit: PageLimit = 100,
    state: Annotated[str, Query(pattern="^(all|active|draft)$")] = "all",
    cursor: str | None = None,
) -> RuleListView:
    await ensure_read_snapshot(session)
    repo = SupplementalRulesRepository(session)
    registry = await repo.get_registry_head("local_owner")
    after = ""
    if cursor:
        try:
            raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
            if (
                raw.get("v") != 1
                or raw.get("registry_revision") != registry.registry_revision
                or raw.get("state") != state
            ):
                raise InvestorError("revision_conflict", "规则列表已变化，请刷新")
            after = str(UUID(raw["after_rule_id"]))
        except InvestorError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise InvestorError("invalid_cursor", "规则列表分页标识无效") from exc
    statement = select(SupplementalRuleHead).where(
        SupplementalRuleHead.owner_id == "local_owner",
        SupplementalRuleHead.rule_id > after,
    )
    if state == "active":
        statement = statement.where(SupplementalRuleHead.active_state == "active")
    elif state == "draft":
        statement = statement.where(SupplementalRuleHead.draft_state.in_(("draft", "validated")))
    heads = list(
        (
            await session.scalars(statement.order_by(SupplementalRuleHead.rule_id).limit(limit + 1))
        ).all()
    )
    page = heads[:limit]
    items = []
    for head in page:
        active = (
            await repo.get_rule_revision(
                "local_owner",
                ObjectRef(
                    kind="supplemental_rule", id=UUID(head.rule_id), revision=head.active_revision
                ),
            )
            if head.active_revision is not None
            else None
        )
        draft = (
            await repo.get_rule_revision(
                "local_owner",
                ObjectRef(
                    kind="supplemental_rule", id=UUID(head.rule_id), revision=head.draft_revision
                ),
            )
            if head.draft_revision is not None
            else None
        )
        items.append(
            {
                "rule_id": head.rule_id,
                "head_revision": head.head_revision,
                "active_state": head.active_state,
                "active_rule": active.model_dump(mode="json") if active else None,
                "draft_state": head.draft_state,
                "draft_rule": draft.model_dump(mode="json") if draft else None,
                "last_validation": head.last_validation_payload,
            }
        )
    next_cursor = None
    if len(heads) > limit and page:
        encoded = json.dumps(
            {
                "v": 1,
                "registry_revision": registry.registry_revision,
                "state": state,
                "after_rule_id": page[-1].rule_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        next_cursor = base64.urlsafe_b64encode(encoded.encode()).decode().rstrip("=")
    return RuleListView.model_validate(
        {
            "registry_revision": registry.registry_revision,
            "items": items,
            "next_cursor": next_cursor,
        }
    )


@router.get("/supplemental-rules/{rule_id}")
async def get_rule(rule_id: UUID, session: Session) -> RuleHeadView:
    await ensure_read_snapshot(session)
    repo = SupplementalRulesRepository(session)
    head = await repo.get_rule_head("local_owner", rule_id)
    if head is None:
        raise InvestorError("not_found", "追加规则不存在")
    active = (
        await repo.get_rule_revision(
            "local_owner",
            ObjectRef(kind="supplemental_rule", id=rule_id, revision=head.active_revision),
        )
        if head.active_revision is not None
        else None
    )
    draft = (
        await repo.get_rule_revision(
            "local_owner",
            ObjectRef(kind="supplemental_rule", id=rule_id, revision=head.draft_revision),
        )
        if head.draft_revision is not None
        else None
    )
    return RuleHeadView.model_validate(
        {
            "head_revision": head.head_revision,
            "active_state": head.active_state,
            "active": active.model_dump(mode="json") if active else None,
            "draft_state": head.draft_state,
            "draft": draft.model_dump(mode="json") if draft else None,
            "last_validation": head.last_validation_payload,
        }
    )


@router.get("/supplemental-rules/{rule_id}/history")
async def rule_history(
    rule_id: UUID,
    session: Session,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    before_sequence: Annotated[int | None, Query(ge=1)] = None,
    limit: PageLimit = 100,
) -> RuleHistoryView:
    await ensure_read_snapshot(session)
    repo = SupplementalRulesRepository(session)
    if await repo.get_rule_head("local_owner", rule_id) is None:
        raise InvestorError("not_found", "追加规则不存在")
    upper = before_sequence
    if upper is None:
        upper = int(
            await session.scalar(
                select(func.coalesce(func.max(SupplementalRuleEvent.sequence), 0)).where(
                    SupplementalRuleEvent.owner_id == "local_owner",
                    SupplementalRuleEvent.rule_id == str(rule_id),
                )
            )
            or 0
        )
    statement = (
        select(SupplementalRuleEvent)
        .where(
            SupplementalRuleEvent.owner_id == "local_owner",
            SupplementalRuleEvent.rule_id == str(rule_id),
            SupplementalRuleEvent.sequence > after_sequence,
            SupplementalRuleEvent.sequence <= upper,
        )
        .order_by(SupplementalRuleEvent.sequence)
        .limit(limit + 1)
    )
    rows = list((await session.scalars(statement)).all())
    page = rows[:limit]
    return RuleHistoryView.model_validate(
        {
            "rule_id": str(rule_id),
            "through_sequence": upper,
            "events": [
                {
                    "sequence": row.sequence,
                    "command": row.command,
                    "rule_revision": row.rule_revision,
                    "operation_id": row.operation_id,
                    "created_at": _utc_db_timestamp(row.created_at).isoformat(),
                    "payload": row.payload,
                }
                for row in page
            ],
            "next_after_sequence": page[-1].sequence if len(rows) > limit and page else None,
        }
    )


@router.get("/research-events/{event_id}")
async def get_event(
    event_id: UUID, session: Session, revision: Annotated[int, Query(ge=1)] = 1
) -> ResearchEvent:
    await ensure_read_snapshot(session)
    value = await SupplementalRulesRepository(session).get_event_revision(
        "local_owner", ObjectRef(kind="research_event", id=event_id, revision=revision)
    )
    if value is None:
        raise InvestorError("not_found", "研究事件不存在")
    return value


@router.get("/research-events")
async def list_research_events(
    session: Session, limit: PageLimit = 100, cursor: str | None = None
) -> ResearchEventListView:
    await ensure_read_snapshot(session)
    registry = await SupplementalRulesRepository(session).get_registry_head("local_owner")
    after_event_id: str | None = None
    if cursor:
        try:
            raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
            if raw.get("v") != 1 or raw.get("registry_revision") != registry.registry_revision:
                raise InvestorError("revision_conflict", "事件列表已变化，请刷新")
            after_event_id = str(UUID(raw["after_event_id"]))
        except InvestorError:
            raise
        except (ValueError, TypeError, KeyError) as exc:
            raise InvestorError("invalid_cursor", "事件列表分页标识无效") from exc
    maximum = (
        select(ResearchEventRevision.id, func.max(ResearchEventRevision.revision).label("revision"))
        .where(ResearchEventRevision.owner_id == "local_owner")
        .group_by(ResearchEventRevision.id)
        .subquery()
    )
    statement = (
        select(ResearchEventRevision)
        .join(
            maximum,
            (ResearchEventRevision.id == maximum.c.id)
            & (ResearchEventRevision.revision == maximum.c.revision),
        )
        .where(ResearchEventRevision.owner_id == "local_owner")
    )
    if after_event_id is not None:
        statement = statement.where(ResearchEventRevision.id > after_event_id)
    rows = list(
        (await session.scalars(statement.order_by(ResearchEventRevision.id).limit(limit + 1))).all()
    )
    page = rows[:limit]
    events = []
    for row in page:
        value = await SupplementalRulesRepository(session).get_event_revision(
            "local_owner",
            ObjectRef(kind="research_event", id=UUID(row.id), revision=row.revision),
        )
        if value is None:
            raise InvestorError("historical_manifest_unavailable", "保存的研究事件修订不可读")
        events.append(value.model_dump(mode="json"))
    next_cursor = None
    if len(rows) > limit and page:
        encoded = json.dumps(
            {
                "v": 1,
                "registry_revision": registry.registry_revision,
                "events_generation": registry.events_generation,
                "after_event_id": page[-1].id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        next_cursor = base64.urlsafe_b64encode(encoded.encode()).decode().rstrip("=")
    return ResearchEventListView.model_validate(
        {
            "registry_revision": registry.registry_revision,
            "events_generation": registry.events_generation,
            "events": events,
            "next_cursor": next_cursor,
        }
    )

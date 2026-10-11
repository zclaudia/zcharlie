"""Authenticated user method proposals; no model approval or paid shadow route."""
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.methods import (
    CreateProposal,
    EvaluateProposal,
    MethodDecision,
    MethodRollback,
    MethodService,
    ProspectiveRequest,
    ShadowRequest,
    run_offline_shadow,
)
from investment_agent.investor.publication import FIXED_METHOD
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.investor import MethodArtifact, MethodHead, MethodProposal
from investment_agent.storage.models.reference import Company, Listing

router = APIRouter(prefix="/investor/methods", tags=["methods"], dependencies=[Depends(verify_api_key_or_session)])


async def service() -> AsyncIterator[MethodService]:
    try:
        async with session_scope() as session:
            yield MethodService(session)
    except InvestorError as exc:
        code = 404 if exc.detail.code == "not_found" else 422 if exc.detail.code == "invalid_reference" else 409
        raise HTTPException(code, detail=exc.detail.model_dump(mode="json")) from exc


Service = Annotated[MethodService, Depends(service, scope="function")]


@router.get("")
async def list_methods(svc: Service, offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=20)] = 20) -> dict[str, Any]:
    proposals = (await svc.session.scalars(select(MethodProposal).where(MethodProposal.owner_id == svc.owner).order_by(MethodProposal.payload["created_at"].as_string().desc(), MethodProposal.id.desc()).offset(offset).limit(limit + 1))).all()
    reviews = (await svc.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == svc.owner, MethodArtifact.kind == "process_review").order_by(MethodArtifact.payload["created_at"].as_string().desc(), MethodArtifact.id.desc()).offset(offset).limit(limit + 1))).all()
    versions = (await svc.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == svc.owner, MethodArtifact.kind == "version").order_by(MethodArtifact.payload["created_at"].as_string().desc(), MethodArtifact.id.desc()).offset(offset).limit(limit + 1))).all()
    heads = (await svc.session.scalars(select(MethodHead).where(MethodHead.owner_id == svc.owner).order_by(MethodHead.revision.desc(), MethodHead.listing_id.desc()).offset(offset).limit(limit + 1))).all()
    tasks = (await svc.session.scalars(select(AgentTask).where(AgentTask.owner_id == svc.owner, AgentTask.status.in_(("succeeded", "partial", "failed"))).order_by(AgentTask.payload["created_at"].as_string().desc(), AgentTask.id.desc()).offset(offset).limit(limit + 1))).all()
    more = any(len(rows) > limit for rows in (proposals, reviews, versions, heads, tasks))
    proposals, reviews, versions, heads, tasks = proposals[:limit], reviews[:limit], versions[:limit], heads[:limit], tasks[:limit]
    listing_ids = {p.payload["request"]["listing_ref"]["id"] for p in proposals} | {h.listing_id for h in heads}
    names = (await svc.session.execute(select(Listing.id, Listing.ticker, Company.canonical_name).join(Company, Listing.company_id == Company.id).where(Listing.id.in_(listing_ids)))).all()
    return {"scope_names": {str(identifier): name + " · " + ticker for identifier, ticker, name in names}, "baseline_method": FIXED_METHOD.model_dump(mode="json"), "proposals": [svc.summary(p) for p in proposals], "reviews": [svc.review_summary(r.payload) for r in reviews], "versions": [{"id": r.id, "binding": r.payload["binding"]} for r in versions], "heads": [{"revision": h.revision, "binding": h.payload} for h in heads], "completed_tasks": [{"id": t.id, "status": t.status, "created_at": t.payload["created_at"], "base_refs": t.payload["binding"]["base_refs"][:16]} for t in tasks], "page": {"offset": offset, "limit": limit, "truncated": more, "next_offset": offset + limit if more else None, "ordering": "Newest task/proposal/review first; offsets may shift after writes; refresh to latest"}, "limits": "One listing only; offline replays cannot authorize activation. Prospective gate-shadow observes new actual outputs; no semantic quality acceptance. Existing Tasks keep frozen methods. Comparison/portfolio use baseline."}


@router.get("/proposals/{proposal_id}")
async def proposal_detail(proposal_id: UUID, svc: Service) -> dict[str, Any]:
    return await svc.evidence(await svc.proposal(proposal_id))


@router.get("/artifacts/{kind}/{artifact_id}")
async def method_artifact(kind: Literal["process_review", "evaluation", "shadow", "prospective_shadow", "version", "event", "proposal_version", "shadow_claim", "shadow_result"], artifact_id: UUID, svc: Service) -> dict[str, Any]:
    import json
    payload = await svc.artifact(artifact_id, kind)
    if len(json.dumps(payload, ensure_ascii=False)) > 2_000_000:
        raise HTTPException(413, "Artifact exceeds bounded response; use exact proposal evidence summary and immutable hash")
    return payload


@router.post("/reviews/{task_id}")
async def freeze_review(task_id: UUID, svc: Service) -> dict[str, Any]:
    return await svc.process_review(task_id)


@router.post("/proposals")
async def create_proposal(body: CreateProposal, svc: Service) -> dict[str, Any]:
    return await svc.create(body)


@router.post("/evaluations")
async def evaluate(body: EvaluateProposal, svc: Service) -> dict[str, Any]:
    return await svc.evaluate(body)


@router.post("/decisions")
async def decision(body: MethodDecision, svc: Service) -> dict[str, Any]:
    return await svc.decide(body, actor="authenticated_user")


@router.post("/rollback")
async def rollback(body: MethodRollback, svc: Service) -> dict[str, Any]:
    return await svc.rollback(body, actor="authenticated_user")


@router.post("/offline-shadow")
async def offline_shadow(body: ShadowRequest) -> dict[str, Any]:
    import json
    def replay(messages: Any, info: Any) -> ModelResponse:
        # Actual native model boundary, exact saved progress. No user/model
        # supplied success flag is accepted; controller recomputes every gate.
        prompt = next(part.content for message in reversed(messages) for part in message.parts if getattr(part, "part_kind", None) == "user-prompt")
        progress = json.loads(prompt)["frozen_progress"]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, progress)])
    try:
        return await run_offline_shadow(session_scope, body, FunctionModel(replay))
    except InvestorError as exc:
        raise HTTPException(422 if exc.detail.code == "invalid_reference" else 409, detail=exc.detail.model_dump(mode="json")) from exc


@router.post("/prospective-shadow")
async def prospective_shadow(body: ProspectiveRequest, svc: Service) -> dict[str, Any]:
    return await svc.prospective(body)


@router.get("/task-bindings/{task_id}")
async def frozen_task_method(task_id: UUID, svc: Service) -> dict[str, Any]:
    from investment_agent.agent.tasks import TaskService
    from investment_agent.investor.methods import task_method
    try:
        await TaskService(svc.session).get_task(task_id)
    except LookupError as exc:
        raise InvestorError("not_found", "Task unavailable") from exc
    return (await task_method(svc.session, task_id)).model_dump(mode="json")


@router.get("/proposals/{proposal_id}/history")
async def proposal_history(proposal_id: UUID, svc: Service, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    from investment_agent.agent.tasks import fingerprint
    await svc.proposal(proposal_id)
    rows = (await svc.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == svc.owner, MethodArtifact.payload["proposal_id"].as_string() == str(proposal_id)).order_by(MethodArtifact.payload["created_at"].as_string().desc(), MethodArtifact.id.desc()).offset(offset).limit(21))).all()
    return {"items": [{"id": r.id, "kind": r.kind, "created_at": r.payload.get("created_at"), "hash": fingerprint(r.payload)} for r in rows[:20]], "next_offset": offset + 20 if len(rows) > 20 else None}

"""Authenticated messages, task snapshots and resumable public event transport."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select, text
from starlette.types import Receive, Scope, Send

from investment_agent.agent.contracts import (
    Conversation,
    ConversationPage,
    ConversationTitleState,
    ConversationTitleUpdate,
    CursorExpired,
    Message,
    MessageAccepted,
    MessageInput,
    Task,
    TaskProgress,
)
from investment_agent.agent.conversations import (
    ConversationCreate,
    ConversationService,
    ConversationTitleConflictError,
    InvalidContextError,
    SummaryCursorInvalidError,
)
from investment_agent.agent.execution import ExecutionDeniedError
from investment_agent.agent.progress import task_progress as task_progress_view
from investment_agent.agent.tasks import (
    TERMINAL,
    EventCursorExpiredError,
    EventCursorInvalidError,
    TaskBoundaryError,
    TaskService,
)
from investment_agent.api.auth import get_bootstrap_settings, verify_api_key_or_session
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import MessageRow, PublicTaskEvent
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.repositories.pipeline_run import IdempotencyPayloadMismatchError

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Depends(verify_api_key_or_session)])
BaseSettings = Annotated[Settings, Depends(get_bootstrap_settings)]
_active_streams: set[UUID] = set()
MAX_STREAMS = 8
POLL_SECONDS = 1.0


@router.get("/listings")
async def listings(q: Annotated[str, Query(max_length=32)] = "") -> list[dict[str, object]]:
    """Read canonical identities only; directory-only hits cannot grant a subject."""
    query = q.strip()
    if not query:
        return []
    parsed = parse_a_share_ticker(query)
    stmt = select(Listing, Company.canonical_name).join(Company)
    if parsed:
        symbol, market = parsed
        stmt = stmt.where(Listing.ticker == symbol, Listing.market_code == market.market_code)
    else:
        stmt = stmt.where(Listing.ticker.startswith(query, autoescape=True) |
            Company.canonical_name.contains(query, autoescape=True))
    async with session_scope() as session:
        rows = (await session.execute(stmt.order_by(Listing.id).limit(25))).all()
        return [{"ref": ObjectRef(kind="listing", id=row.id, revision=1).model_dump(mode="json"),
            "ticker": format_listing_ticker(ticker=row.ticker, market_code=row.market_code),
            "name": name} for row, name in rows]


class EventStreamResponse(StreamingResponse):
    """Release admission even if disconnect precedes generator iteration."""

    def __init__(self, request: Request, task_id: UUID, cursor: int, token: UUID) -> None:
        self.token = token
        super().__init__(event_stream(request, task_id, cursor, token), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            _active_streams.discard(self.token)


def expired_detail(exc: EventCursorExpiredError) -> dict[str, object]:
    return {**CursorExpired(code="cursor_expired", reload_snapshot=True,
        task_ref=ObjectRef(kind="task", id=exc.task_id, revision=1)).model_dump(mode="json"),
        "snapshot_url": f"/agent/tasks/{exc.task_id}/snapshot"}


async def service(base: BaseSettings) -> AsyncIterator[ConversationService]:
    """Deliberately not ``deps.session_service``: this dependency takes an
    injected settings sub-dependency and maps its own, disjoint exception
    taxonomy.  ``ConversationService`` never raises ``InvestorError``, so adding
    the canonical InvestorError status table here would turn today's 500 into a
    4xx - an API contract change.  Keep the two maps separate."""

    try:
        async with session_scope() as session:
            yield ConversationService(session, base)
    except InvalidContextError as exc:
        raise HTTPException(422, detail={"code": "invalid_reference", "message": str(exc)}) from exc
    except IdempotencyPayloadMismatchError as exc:
        raise HTTPException(409, detail={"code": "idempotency_conflict"}) from exc
    except ExecutionDeniedError as exc:
        raise HTTPException(409, detail={"code": "execution_denied"}) from exc
    except TaskBoundaryError as exc:
        raise HTTPException(409, detail={"code": "task_conflict"}) from exc
    except LookupError as exc:
        raise HTTPException(404, detail={"code": "not_found"}) from exc


Service = Annotated[ConversationService, Depends(service, scope="function")]


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate, svc: Service) -> Conversation:
    return await svc.create(body)


@router.get("/conversations")
async def conversations(svc: Service, after: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100) -> tuple[Conversation, ...]:
    return await svc.list(after=after, limit=limit)


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: UUID, svc: Service) -> Conversation:
    return await svc.get(conversation_id)


@router.get("/conversation-summaries")
async def conversation_summaries(svc: Service,
    before: Annotated[str, Query(max_length=512)] | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    attention: Literal["needs_you", "running", "none"] | None = None) -> ConversationPage:
    # Same short real read snapshot as /tasks/{id}/snapshot: activity order,
    # attention states and identities must come from one consistent read.
    await svc.session.execute(text("BEGIN"))
    try:
        return await svc.summaries(before=before, limit=limit, attention=attention)
    except SummaryCursorInvalidError as exc:
        raise HTTPException(422, detail={"code": "invalid_cursor"}) from exc


@router.patch("/conversations/{conversation_id}")
async def rename_conversation(conversation_id: UUID, body: ConversationTitleUpdate,
    svc: Service) -> ConversationTitleState:
    try:
        return await svc.rename(conversation_id, body)
    except ConversationTitleConflictError as exc:
        raise HTTPException(409, detail={"code": "conversation_title_conflict"}) from exc


@router.get("/candidate-context/{candidate_id}")
async def candidate_context(candidate_id: int, svc: Service) -> dict[str, object]:
    from investment_agent.storage.models.candidates import CandidateRecord
    row = await svc.session.get(CandidateRecord, candidate_id)
    if row is None:
        raise HTTPException(404, detail={"code": "not_found"})
    ref = ObjectRef(kind="candidate", id=row.id, revision=getattr(row, "version", 1))
    refs = await svc.resolve_context((ref,))
    return {"context_refs": [ref.model_dump(mode="json"),
        *[item.model_dump(mode="json") for item in refs if item.kind == "listing"]]}


@router.get("/listings/{listing_id}")
async def listing_identity(listing_id: Annotated[int, Path(ge=1)]) -> dict[str, object]:
    async with session_scope() as session:
        result = (await session.execute(select(Listing, Company.canonical_name).join(Company)
            .where(Listing.id == listing_id))).one_or_none()
        if result is None:
            raise HTTPException(404, detail={"code": "not_found"})
        row, name = result
        return {"ref": ObjectRef(kind="listing", id=row.id, revision=1).model_dump(mode="json"),
            "ticker": format_listing_ticker(ticker=row.ticker, market_code=row.market_code), "name": name}


@router.get("/conversations/{conversation_id}/messages")
async def messages(conversation_id: UUID, svc: Service,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100) -> tuple[Message, ...]:
    return await svc.messages(conversation_id, after=after, limit=limit)


@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def submit_message(conversation_id: UUID, body: MessageInput, svc: Service) -> MessageAccepted:
    return await svc.submit(conversation_id, body)


@router.get("/tasks/{task_id}")
async def task(task_id: UUID, svc: Service) -> Task:
    return await TaskService(svc.session).get_task(task_id)


@router.get("/tasks/{task_id}/research")
async def research_progress(task_id: UUID, svc: Service) -> tuple[dict[str, object], ...]:
    from investment_agent.agent.research import research_artifacts
    return await research_artifacts(svc.session, task_id)


@router.get("/tasks/{task_id}/progress")
async def read_progress(task_id: UUID, svc: Service) -> TaskProgress:
    # Same real read snapshot as /snapshot; saved plans and usage outlive a
    # disabled Agent, an expired grant or a replaced mandate.
    await svc.session.execute(text("BEGIN"))
    progress = await task_progress_view(svc.session, task_id, svc.base)
    if progress is None:
        raise HTTPException(404, detail={"code": "not_found"})
    return progress


class CancelInput(Contract):
    # Optional and ignored: the cancellation transition is intrinsically
    # idempotent, so no client-supplied key is needed; the web client still
    # sends one (web/static/agent.js), so the field stays tolerated.
    idempotency_key: UUID | None = None


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: UUID, svc: Service, body: CancelInput | None = None) -> Task:
    # State transition is intrinsically idempotent across every cancellation key;
    # the body is accepted (and ignored) for contract continuity.
    return await TaskService(svc.session).cancel(task_id)


@router.get("/tasks/{task_id}/snapshot")
async def snapshot(task_id: UUID, svc: Service,
    include_progress: Annotated[bool, Query()] = False) -> dict[str, object]:
    # A logical SQLAlchemy transaction does not emit BEGIN before SQLite SELECT.
    # Own a real, short read snapshot for task/messages/cursor consistency.
    await svc.session.execute(text("BEGIN"))
    task = await TaskService(svc.session).get_task(task_id)
    cursor = await svc.session.scalar(select(func.max(PublicTaskEvent.event_id)).where(
        PublicTaskEvent.task_id == str(task_id))) or 0
    saved = list(await svc.session.scalars(select(MessageRow).where(
        MessageRow.payload["task_ref"]["id"].as_string() == str(task_id)).order_by(MessageRow.sequence)))
    # cursor=0 explicitly starts from retained history after loading this snapshot.
    result: dict[str, object] = {"task": task.model_dump(mode="json"),
        "messages": [row.payload for row in saved],
        "event_cursor": cursor, "events_url": f"/agent/tasks/{task_id}/events"}
    if include_progress:
        # Same transaction as task/messages/cursor; a task without a research
        # grant still returns with progress=null instead of a 404.
        progress = await task_progress_view(svc.session, task_id, svc.base)
        result["progress"] = progress.model_dump(mode="json") if progress is not None else None
    return result


async def read_events(task_id: UUID, cursor: int) -> tuple[Task, list[dict[str, object]]]:
    # Session is closed BEFORE any yield, heartbeat or await sleep.
    try:
        async with session_scope() as session:
            await session.execute(text("BEGIN"))
            svc = TaskService(session)
            events = await svc.get_events(task_id, cursor)
            task = await svc.get_task(task_id)
            return task, [event.model_dump(mode="json") for event in events]
    except LookupError as exc:
        raise HTTPException(404, detail={"code": "not_found"}) from exc
    except EventCursorInvalidError as exc:
        raise HTTPException(422, detail={"code": "invalid_cursor"}) from exc


async def event_stream(request: Request, task_id: UUID, cursor: int,
    token: UUID) -> AsyncIterator[str]:
    try:
        while not await request.is_disconnected():
            try:
                task, events = await read_events(task_id, cursor)
            except EventCursorExpiredError as exc:
                yield f"event: cursor_expired\ndata: {json.dumps(expired_detail(exc))}\n\n"
                return
            except HTTPException as exc:
                # The 200 text/event-stream response is already committed: an
                # HTTPException here can only truncate the stream. Report the
                # failure as one structured in-band event, then end (the client
                # recovers through the snapshot URL, as with cursor_expired).
                detail = exc.detail if isinstance(exc.detail, dict) else {"code": str(exc.detail)}
                yield ("event: task_error\ndata: "
                    + json.dumps({**detail, "status_code": exc.status_code,
                        "snapshot_url": f"/agent/tasks/{task_id}/snapshot"}, ensure_ascii=False)
                    + "\n\n")
                return
            for event in events:
                cursor = int(str(event["event_id"]))
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if len(events) < 200 and task.status.value in TERMINAL:
                return
            if len(events) == 200:
                continue
            yield ": heartbeat\n\n"
            await asyncio.sleep(POLL_SECONDS)
    finally:
        _active_streams.discard(token)


@router.get("/tasks/{task_id}/events", response_model=None)
async def events(task_id: UUID, request: Request,
    after_event_id: Annotated[int, Query(ge=0, le=2**63-1)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    transport: Literal["sse", "poll"] = "sse") -> StreamingResponse | JSONResponse:
    cursor = after_event_id
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdigit() or len(last_event_id) > 19:
            raise HTTPException(422, detail={"code": "invalid_cursor"})
        cursor = int(last_event_id)
        if cursor > 2**63-1:
            raise HTTPException(422, detail={"code": "invalid_cursor"})
    try:
        task, batch = await read_events(task_id, cursor)
    except EventCursorExpiredError as exc:
        raise HTTPException(410, detail=expired_detail(exc)) from exc
    if transport == "poll":
        return JSONResponse({"events": batch, "next_cursor": batch[-1]["event_id"] if batch else cursor,
            "task": task.model_dump(mode="json"), "snapshot_url": f"/agent/tasks/{task_id}/snapshot"},
            headers={"Cache-Control": "no-store"})
    if len(_active_streams) >= MAX_STREAMS:
        raise HTTPException(429, detail={"code": "stream_limit"}, headers={"Retry-After": "1"})
    from uuid import uuid4

    token = uuid4()
    _active_streams.add(token)
    return EventStreamResponse(request, task_id, cursor, token)

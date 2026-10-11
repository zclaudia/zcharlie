"""Authenticated conversation application service; caller owns the transaction.

Public messages contain only user input or a deliverable selected by trusted
server completion code. Provider history, prompts and raw output stay private.
"""

import base64
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, Exists, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.contracts import (
    ContextBinding,
    Conversation,
    ConversationAttention,
    ConversationPage,
    ConversationSubject,
    ConversationSummary,
    ConversationTaskView,
    ConversationTitleState,
    ConversationTitleUpdate,
    Message,
    MessageAccepted,
    MessageInput,
)
from investment_agent.agent.execution import ExecutionGrant, admit, check_boundary
from investment_agent.agent.tasks import TaskClaim, TaskService, fingerprint
from investment_agent.config import Settings
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.contracts import ContentNode
from investment_agent.investor.enums import StopReason, TaskStatus
from investment_agent.storage.models.agent import AgentTask, ConversationRow, MessageRow
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.earnings_events import (
    BusinessSegmentRecord,
    DisclosureScheduleRecord,
    EarningsForecastRecord,
)
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.investor import (
    CaseRevision,
    EpisodeRevision,
    InvestorCase,
    MandateHead,
    MandateRevision,
    PlanRevision,
)
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.models.publication import PublicationArtifact
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.pipeline_run import IdempotencyPayloadMismatchError


class InvalidContextError(ValueError):
    pass


class SummaryCursorInvalidError(ValueError):
    pass


class ConversationTitleConflictError(ValueError):
    pass


class ConversationCreate(Contract):
    title: str | None = None
    context_refs: tuple[ObjectRef, ...] = ()


# Task statuses aggregate to "running"; waiting_user alone means needs_you.
RUNNING_STATUSES = ("queued", "running", "waiting_data")
_CURSOR_VERSION = 1
_CURSOR_MAX_LENGTH = 512


def as_utc(value: datetime) -> datetime:
    """SQLite DateTime drops the offset: stored values are UTC wall time."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def bump_activity(conversation: ConversationRow, at: datetime) -> None:
    """Real new messages only; replays, task events and renames never call this."""
    stored = conversation.last_activity_at
    current = as_utc(stored) if stored is not None else None
    conversation.last_activity_at = at if current is None or at > current else current


@dataclass(frozen=True)
class SummaryCursor:
    time: datetime
    conversation_id: str
    attention: ConversationAttention | None


def encode_cursor(at: datetime, conversation_id: str, attention: ConversationAttention | None) -> str:
    raw = json.dumps({"v": _CURSOR_VERSION, "t": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
        "id": conversation_id, "a": attention}, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def decode_cursor(value: str, attention: ConversationAttention | None) -> SummaryCursor:
    try:
        if not value or len(value) > _CURSOR_MAX_LENGTH:
            raise ValueError("cursor length")
        data = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION:
            raise ValueError("cursor version")
        at = datetime.fromisoformat(str(data["t"]))
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("cursor time must include a timezone")
        if data.get("a") != attention:
            raise ValueError("cursor attention differs from the requested filter")
        raw_id = str(data["id"])
        if str(UUID(raw_id)) != raw_id:
            raise ValueError("cursor id must be a canonical UUID")
        return SummaryCursor(time=at.astimezone(UTC), conversation_id=raw_id, attention=attention)
    except (KeyError, TypeError, ValueError) as exc:
        raise SummaryCursorInvalidError("invalid conversation cursor") from exc


def _attention_matches(owner: str, attention: ConversationAttention) -> ColumnElement[bool]:
    """EXISTS aggregation over every task referenced by the conversation's messages."""
    task_id = MessageRow.payload["task_ref"]["id"].as_string()

    def exists(statuses: tuple[str, ...]) -> Exists:
        return select(AgentTask.id).where(MessageRow.conversation_id == ConversationRow.id,
            AgentTask.id == task_id, AgentTask.owner_id == owner,
            AgentTask.status.in_(statuses)).exists()

    if attention == "needs_you":
        return exists(("waiting_user",))
    if attention == "running":
        return exists(RUNNING_STATUSES)
    return and_(~exists(("waiting_user",)), ~exists(RUNNING_STATUSES))


def normalized(message: MessageInput) -> MessageInput:
    text = unicodedata.normalize("NFC", message.text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not text or len(text) > 32000 or len(message.context_refs) > 32:
        raise InvalidContextError("message exceeds limits or is empty")
    # Context is a set; deterministic order makes equivalent retransmissions equal.
    refs = tuple(sorted(set(message.context_refs), key=lambda ref: (ref.kind, str(ref.id), ref.revision, ref.field_path or "")))
    return message.model_copy(update={"text": text, "context_refs": refs})


class ConversationService:
    def __init__(self, session: AsyncSession, base: Settings) -> None:
        self.session, self.base = session, base
        self.owner = "local_owner"

    async def _conversation(self, conversation_id: UUID, *, lock: bool = False) -> ConversationRow:
        if lock:
            # First acquire a SQLite write reservation, before reading receipts.
            # Separate sessions then serialize and see the winning committed key.
            result = await self.session.execute(update(ConversationRow)
                .where(ConversationRow.id == str(conversation_id), ConversationRow.owner_id == self.owner)
                .values(next_sequence=ConversationRow.next_sequence))
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise LookupError("conversation not found")
        row = await self.session.scalar(select(ConversationRow).where(
            ConversationRow.id == str(conversation_id), ConversationRow.owner_id == self.owner
        ).execution_options(populate_existing=True))
        if row is None:
            raise LookupError("conversation not found")
        return row

    async def resolve_context(self, refs: tuple[ObjectRef, ...], *, depth: int = 0) -> tuple[ObjectRef, ...]:
        """Validate exact stored versions and derive listing scope from DB identities.

        No text-to-ticker guessing or empty-context pool authorization. Unsupported
        future object types fail closed until their owning service exists.
        """
        if len(refs) > 32 or depth > 5:
            raise InvalidContextError("too many references")
        scope: set[ObjectRef] = set()
        legacy: dict[str, Any] = {
            "listing": Listing, "candidate": CandidateRecord, "watchlist_item": WatchlistItem,
            "financial_snapshot": FinancialSnapshot, "market_snapshot": MarketSnapshot,
            "earnings_forecast": EarningsForecastRecord, "disclosure_schedule": DisclosureScheduleRecord,
            "business_segment": BusinessSegmentRecord,
        }
        versions: dict[str, Any] = {"case": CaseRevision, "episode": EpisodeRevision,
            "plan": PlanRevision, "mandate": MandateRevision,
            "evidence": SavedEvidence, "calculation": SavedCalculation}
        for ref in refs:
            if ref.field_path is not None and ref.kind not in {"evidence", "calculation"}:
                raise InvalidContextError("context must reference an object")
            if ref.kind in {"portfolio_snapshot", "portfolio_plan"}:
                from investment_agent.portfolio.contracts import Snapshot
                from investment_agent.portfolio.plans import PortfolioPlanService
                from investment_agent.portfolio.service import PortfolioService
                from investment_agent.storage.models.portfolio import PortfolioSnapshot
                if ref.kind == "portfolio_plan":
                    plan_row = await PortfolioPlanService(self.session, self.owner).artifact(ref)
                    plan_snapshot_ref = ObjectRef.model_validate(plan_row.payload["snapshot_ref"])
                    saved = await self.session.get(PortfolioSnapshot, str(plan_snapshot_ref.id))
                else:
                    saved = await self.session.get(PortfolioSnapshot, str(ref.id))
                if saved is None:
                    raise InvalidContextError("portfolio snapshot unavailable")
                await PortfolioService(self.session, self.owner)._account(UUID(saved.account_id))
                snapshot = Snapshot.model_validate(saved.payload)
                if ref.kind == "portfolio_snapshot" and snapshot.reference != ref:
                    raise InvalidContextError("portfolio snapshot version differs")
                scope.add(snapshot.reference)
                scope.update(h.listing_ref for h in snapshot.holdings)
                continue
            if ref.kind in {"thesis", "recommendation"}:
                publication = await self.session.get(PublicationArtifact, (str(ref.id), ref.revision))
                if publication is None or publication.owner_id != self.owner or publication.kind != ref.kind:
                    raise InvalidContextError("publication reference version unavailable")
                case = await self.session.get(InvestorCase, publication.case_id)
                if case is None or case.owner_id != self.owner or case.payload["status"] == "archived":
                    raise InvalidContextError("publication subject unavailable")
                if ref.kind == "recommendation" and publication.payload.get("case_ids") != [case.id]:
                    raise InvalidContextError("publication subject differs")
                if ref.kind == "thesis" and publication.payload.get("case_ref", {}).get("id") != case.id:
                    raise InvalidContextError("publication subject differs")
                scope.add(ObjectRef(kind="listing", id=case.listing_id, revision=1))
                scope.add(ref)
            elif ref.kind in legacy:
                row = await self.session.get(legacy[ref.kind], ref.id)
                if row is None or ref.revision != getattr(row, "version", 1):
                    raise InvalidContextError("reference version unavailable")
                listing_id = row.id if ref.kind == "listing" else row.listing_id
                if ref.kind == "watchlist_item" and not row.enabled:
                    raise InvalidContextError("watchlist item disabled")
                if await self.session.get(Listing, listing_id) is None:
                    raise InvalidContextError("reference subject unavailable")
                scope.add(ObjectRef(kind="listing", id=listing_id, revision=1))
            elif ref.kind in versions:
                row = await self.session.get(versions[ref.kind], (str(ref.id), ref.revision))
                if row is None:
                    raise InvalidContextError("reference version unavailable")
                if ref.field_path is not None:
                    if ref.kind == "evidence":
                        valid = ref.field_path in {"value", "text"} and row.payload.get(ref.field_path) is not None
                    else:
                        valid = ref.field_path.startswith("results.") and ref.field_path[8:] in row.payload.get("results", {})
                    if not valid:
                        raise InvalidContextError("reference field unavailable")
                if ref.kind in {"case", "episode", "plan"}:
                    case_id = str(ref.id) if ref.kind == "case" else row.case_id
                    case = await self.session.get(InvestorCase, case_id)
                    if case is None or case.owner_id != self.owner or case.payload["status"] == "archived":
                        raise InvalidContextError("reference subject unavailable")
                    scope.add(ObjectRef(kind="listing", id=case.listing_id, revision=1))
                    scope.add(ref)
                else:
                    if row.owner_id != self.owner:
                        raise InvalidContextError("reference unavailable")
                    if ref.kind in {"evidence", "calculation"}:
                        subjects: tuple[ObjectRef, ...]
                        if ref.kind == "evidence":
                            subject = row.payload.get("subject_ref")
                            if subject is None:
                                raise InvalidContextError("reference subject unavailable")
                            subjects = (ObjectRef.model_validate(subject),)
                        else:
                            subjects = tuple(ObjectRef.model_validate(fact) for fact in row.payload.get("fact_refs", ()))

                        scope.update(await self.resolve_context(subjects, depth=depth + 1))
                    scope.add(ref)
            else:
                raise InvalidContextError("unsupported context reference")
        return tuple(sorted(scope, key=lambda ref: (ref.kind, str(ref.id), ref.revision)))

    async def create(self, body: ConversationCreate) -> Conversation:
        scope = await self.resolve_context(body.context_refs)
        if body.title is not None and len(body.title) > 256:
            raise InvalidContextError("title too long")
        value = Conversation(id=uuid4(), revision=1, created_at=datetime.now(UTC), actor=self.owner,
            origin_ref=None, title=body.title, context_refs=body.context_refs)
        # Internal create-time listing projection; summaries prefer the latest
        # user message's task binding once messages exist.
        subjects = [ref.model_dump(mode="json") for ref in scope if ref.kind == "listing"]
        self.session.add(ConversationRow(id=str(value.id), owner_id=self.owner, next_sequence=1,
            last_activity_at=value.created_at, title_revision=1, subject_refs=subjects,
            payload=value.model_dump(mode="json")))
        await self.session.flush()
        return value

    async def get(self, conversation_id: UUID) -> Conversation:
        return Conversation.model_validate((await self._conversation(conversation_id)).payload)

    async def list(self, *, after: UUID | None = None, limit: int = 100) -> tuple[Conversation, ...]:
        query = select(ConversationRow).where(ConversationRow.owner_id == self.owner)
        if after:
            query = query.where(ConversationRow.id > str(after))
        rows = await self.session.scalars(query.order_by(ConversationRow.id).limit(limit))
        return tuple(Conversation.model_validate(row.payload) for row in rows)

    async def messages(self, conversation_id: UUID, *, after: int = 0, limit: int = 100) -> tuple[Message, ...]:
        await self._conversation(conversation_id)
        rows = await self.session.scalars(select(MessageRow).where(
            MessageRow.conversation_id == str(conversation_id), MessageRow.sequence > after
        ).order_by(MessageRow.sequence).limit(limit))
        return tuple(Message.model_validate(row.payload) for row in rows)

    async def submit(self, conversation_id: UUID, body: MessageInput) -> MessageAccepted:
        body = normalized(body)
        digest = fingerprint(body.model_dump(mode="json", exclude={"client_message_id"} if body.disclosure_access else {"client_message_id", "disclosure_access"}))
        async with self.session.begin_nested():
            conversation = await self._conversation(conversation_id, lock=True)
            existing = await self.session.scalar(select(MessageRow).where(
                MessageRow.conversation_id == str(conversation_id),
                MessageRow.client_message_id == str(body.client_message_id)))
            if existing is not None:
                if existing.input_hash != digest:
                    raise IdempotencyPayloadMismatchError("message key reused")
                message = Message.model_validate(existing.payload)
                assert message.task_ref is not None
                task = await TaskService(self.session).get_task(UUID(str(message.task_ref.id)))
                return MessageAccepted(message=message, task=task, events_url=f"/agent/tasks/{task.id}/events")
            scope = await self.resolve_context(body.context_refs)
            if body.reply_to is not None:
                reply = await self.session.get(MessageRow, str(body.reply_to.id))
                if body.reply_to.kind != "message" or body.reply_to.revision != 1 or body.reply_to.field_path is not None or reply is None or reply.conversation_id != str(conversation_id):
                    raise InvalidContextError("reply must belong to this conversation")
            head = await self.session.get(MandateHead, self.owner, populate_existing=True)
            mandate_ref = () if head is None else (ObjectRef(kind="mandate", id=UUID(head.mandate_id), revision=head.revision),)
            binding_refs = tuple(dict.fromkeys((*body.context_refs, *scope, *mandate_ref)))
            securities = tuple(ref for ref in scope if ref.kind == "listing")
            from investment_agent.agent.research import classify
            from investment_agent.investor.common import utc_timestamp
            kind = classify(body.text, body.research_kind)
            cutoff = utc_timestamp(body.research_at) if body.research_at else datetime.now(UTC)
            if cutoff > datetime.now(UTC):
                raise InvalidContextError("research cutoff cannot be future")
            from investment_agent.evidence.disclosures import DisclosurePermission
            disclosure_permissions: tuple[DisclosurePermission, ...] = ()
            if body.disclosure_access:
                from investment_agent.settings.loader import load_settings
                selected = await load_settings(self.session, base=self.base)
                if not selected.disclosure_originals_enabled:
                    raise InvalidContextError("disclosure originals disabled")
                requested = [(access.listing_id, access.source) for access in body.disclosure_access]
                if len(set(requested)) != len(requested):
                    raise InvalidContextError("duplicate disclosure access")
                for access in body.disclosure_access:
                    listing = await self.session.get(Listing, access.listing_id)
                    if access.listing_id not in {ref.id for ref in securities} or listing is None or listing.market_code != "SZSE":
                        raise InvalidContextError("disclosure access requires exact authorized Shenzhen listing")
                disclosure_permissions = tuple(DisclosurePermission(**access.model_dump(), cutoff=cutoff) for access in body.disclosure_access)
            case_refs = tuple(ref for ref in scope if ref.kind == "case")
            if kind in {"company_research", "review"} and len(securities) == 1 and not case_refs:
                from investment_agent.investor.foundations import CreateCase, InvestorService
                case = await InvestorService(self.session).create_case("local_owner", CreateCase(
                    listing_ref=securities[0], question=body.text, idempotency_key=body.client_message_id))
                case_refs = (ObjectRef(kind="case", id=case.id, revision=case.revision),)
                binding_refs = tuple(dict.fromkeys((*binding_refs, *case_refs)))
            portfolio_snapshots = tuple(r for r in scope if r.kind == "portfolio_snapshot")
            portfolio_valuations = tuple(r for r in scope if r.kind == "portfolio_valuation")
            portfolio_account_id = None
            portfolio_snapshot_ref = None
            portfolio_valuation_ref = None
            if kind == "portfolio_action":
                if not self.base.portfolio_ledger_enabled or len(portfolio_snapshots) != 1:
                    raise InvalidContextError("Portfolio research requires enabled ledger and one exact snapshot")
                from investment_agent.storage.models.portfolio import (
                    PortfolioSnapshot,
                    PortfolioValuation,
                )
                saved_snapshot = await self.session.get(PortfolioSnapshot, str(portfolio_snapshots[0].id))
                assert saved_snapshot is not None
                portfolio_account_id = UUID(saved_snapshot.account_id)
                portfolio_snapshot_ref = portfolio_snapshots[0]
                # An explicit price basis in the message context (plan 2.5); the
                # calculation tool still re-binds the current valuation explicitly.
                if len(portfolio_valuations) > 1:
                    raise InvalidContextError("One exact valuation context at most")
                if portfolio_valuations:
                    saved_valuation = await self.session.get(PortfolioValuation, str(portfolio_valuations[0].id))
                    if saved_valuation is None or saved_valuation.account_id != saved_snapshot.account_id:
                        raise InvalidContextError("Valuation context must belong to the exact snapshot account")
                    portfolio_valuation_ref = portfolio_valuations[0]
            tools: tuple[str, ...] = ("save_research_progress", "prepare_user_operation")
            if kind == "portfolio_action":
                tools += ("get_portfolio_context", "calculate_portfolio_options", "prepare_portfolio_plan", "publish_portfolio_plan")
            if disclosure_permissions:
                tools += ("discover_disclosures", "fetch_disclosure_original", "parse_disclosure_original", "read_disclosure_original")
            if securities:
                tools += ("compare_saved_sources", "search_saved_sources", "get_snapshot", "get_evidence", "get_event", "compare_stocks", "capture_snapshot", "reverse_dcf_fcfe", "historical_valuation_percentile")
            if case_refs and kind in {"company_research", "review"}:
                tools += ("prepare_company_view", "publish_recommendation", "extend_recommendation")
            # Existing collectors collect the WHOLE watchlist. Grant only when
            # every enabled member is explicitly included in this message scope.
            watch_members = set((await self.session.scalars(select(WatchlistItem.listing_id).where(WatchlistItem.enabled.is_(True)))).all())
            watchlist = bool(watch_members) and watch_members <= {r.id for r in securities}
            if watchlist:
                tools += ("request_data_refresh",)
            message_id = uuid4()
            task = await admit(self.session, base=self.base,
                input_key=f"message:{message_id}",
                binding=ContextBinding(context_refs=body.context_refs, base_refs=binding_refs, as_of=cutoff),
                message_ref=ObjectRef(kind="message", id=message_id, revision=1),
                grant=ExecutionGrant(mode="interactive", source="authenticated_message", prompt=body.text,
                    mandate_id=UUID(head.mandate_id) if head else None,
                    scope_refs=(*securities, *case_refs, *portfolio_snapshots, *portfolio_valuations), tool_names=tools, watchlist=watchlist,
                    portfolio_account_id=portfolio_account_id, portfolio_snapshot_ref=portfolio_snapshot_ref,
                    portfolio_valuation_ref=portfolio_valuation_ref,
                    current_research=body.research_at is None, research_kind=kind, disclosure_permissions=disclosure_permissions))
            message = Message(id=message_id, revision=1, created_at=datetime.now(UTC), actor=self.owner,
                origin_ref=None, conversation_ref=ObjectRef(kind="conversation", id=conversation_id, revision=1),
                sequence=conversation.next_sequence, role="user", text=body.text, context_refs=body.context_refs,
                reply_to=body.reply_to, client_message_id=body.client_message_id,
                task_ref=ObjectRef(kind="task", id=task.id, revision=task.revision), disclosure_access=body.disclosure_access)
            self.session.add(MessageRow(id=str(message.id), conversation_id=str(conversation_id),
                client_message_id=str(body.client_message_id), completion_key=None, input_hash=digest,
                sequence=message.sequence, payload=message.model_dump(mode="json")))
            conversation.next_sequence += 1
            bump_activity(conversation, message.created_at)
            await self.session.flush()
            from investment_agent.agent.user_commands import execute_clear_command
            if await execute_clear_command(self.session, message, self.base, scope):
                task = await TaskService(self.session).get_task(task.id)
            return MessageAccepted(message=message, task=task, events_url=f"/agent/tasks/{task.id}/events")

    async def save_assistant(self, claim: TaskClaim, *, text: str,
        content: tuple[ContentNode, ...] = (), artifact_refs: tuple[ObjectRef, ...] = ()) -> Message:
        """Trusted AG13 completion callback seam, NEVER an HTTP/model tool.

        Call inside RunSpecification.completion's existing transaction. The
        executor fences again and checkpoints terminal state before committing;
        rollback removes this message together with completion. AG13 is responsible
        for certifying the actual deliverable, refs and publication correctness.
        """
        await check_boundary(self.session, claim, self.base)
        service = TaskService(self.session)
        row = await service.fence(claim)
        task = await service.get_task(claim.task_id)
        if task.message_ref is None or not text.strip() or len(text) > 64000:
            raise InvalidContextError("deliverable requires a message source and text")
        source = await self.session.get(MessageRow, str(task.message_ref.id))
        if source is None:
            raise InvalidContextError("message source unavailable")
        user_message = Message.model_validate(source.payload)
        conversation = await self._conversation(UUID(source.conversation_id), lock=True)
        digest = fingerprint({"text": text, "content": [item.model_dump(mode="json") for item in content],
            "artifacts": [ref.model_dump(mode="json") for ref in artifact_refs]})
        existing = await self.session.scalar(select(MessageRow).where(MessageRow.completion_key == row.id))
        if existing is not None:
            if existing.input_hash != digest:
                raise IdempotencyPayloadMismatchError("completion reused")
            return Message.model_validate(existing.payload)
        message = Message(id=uuid4(), revision=1, created_at=datetime.now(UTC), actor=self.owner,
            origin_ref=task.message_ref, conversation_ref=user_message.conversation_ref,
            sequence=conversation.next_sequence, role="assistant", text=text, content=content,
            context_refs=task.binding.context_refs, reply_to=task.message_ref, client_message_id=None,
            task_ref=ObjectRef(kind="task", id=task.id, revision=task.revision), artifact_refs=artifact_refs)
        self.session.add(MessageRow(id=str(message.id), conversation_id=conversation.id,
            client_message_id=None, completion_key=row.id, input_hash=digest, sequence=message.sequence,
            payload=message.model_dump(mode="json")))
        conversation.next_sequence += 1
        bump_activity(conversation, message.created_at)
        await self.session.flush()
        return message

    async def rename(self, conversation_id: UUID, body: ConversationTitleUpdate) -> ConversationTitleState:
        conversation = await self._conversation(conversation_id, lock=True)
        title = body.title
        if title is not None:
            title = unicodedata.normalize("NFC", title).strip()
            if not title or len(title) > 256:
                raise InvalidContextError("title must contain 1 to 256 characters")
        if body.expected_title_revision != conversation.title_revision:
            raise ConversationTitleConflictError("conversation title revision conflict")
        if title == conversation.payload.get("title"):
            return ConversationTitleState(id=UUID(conversation.id), title=title,
                title_revision=conversation.title_revision)
        value = Conversation.model_validate(conversation.payload).model_copy(update={"title": title})
        # The editable title is versioned separately; Conversation.revision and
        # existing message conversation_refs must never move.
        conversation.payload = value.model_dump(mode="json")
        conversation.title_revision += 1
        await self.session.flush()
        return ConversationTitleState(id=UUID(conversation.id), title=title,
            title_revision=conversation.title_revision)

    async def summaries(self, *, before: str | None = None, limit: int = 50,
        attention: ConversationAttention | None = None) -> ConversationPage:
        """Keyset page over activity time; attention filters before pagination."""
        cursor = decode_cursor(before, attention) if before is not None else None
        query = select(ConversationRow).where(ConversationRow.owner_id == self.owner)
        if attention is not None:
            query = query.where(_attention_matches(self.owner, attention))
        if cursor is not None:
            query = query.where(or_(ConversationRow.last_activity_at < cursor.time,
                and_(ConversationRow.last_activity_at == cursor.time,
                    ConversationRow.id < cursor.conversation_id)))
        rows = (await self.session.scalars(query.order_by(
            ConversationRow.last_activity_at.desc(), ConversationRow.id.desc()).limit(limit + 1))).all()
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            edge_at = rows[-1].last_activity_at
            if edge_at is not None:
                next_cursor = encode_cursor(as_utc(edge_at), rows[-1].id, attention)
        return ConversationPage(items=await self._summary_items(rows), next_cursor=next_cursor)

    async def _summary_items(self, rows: Sequence[ConversationRow]) -> tuple[ConversationSummary, ...]:
        if not rows:
            return ()
        ids = [row.id for row in rows]
        count_rows = (await self.session.execute(select(MessageRow.conversation_id, func.count())
            .where(MessageRow.conversation_id.in_(ids)).group_by(MessageRow.conversation_id))).all()
        counts: dict[str, int] = {conversation_id: total for conversation_id, total in count_rows}
        task_ref_id = MessageRow.payload["task_ref"]["id"].as_string()
        pairs = (await self.session.execute(select(MessageRow.conversation_id, task_ref_id)
            .where(MessageRow.conversation_id.in_(ids), task_ref_id.isnot(None)).distinct())).all()
        task_ids = {task_id for _, task_id in pairs}
        task_rows: dict[str, AgentTask] = {}
        if task_ids:
            task_rows = {task_row.id: task_row for task_row in (await self.session.scalars(
                select(AgentTask).where(
                    AgentTask.id.in_(task_ids), AgentTask.owner_id == self.owner)))}
        by_conversation: dict[str, list[str]] = {}
        for conversation_id, task_id in pairs:
            by_conversation.setdefault(conversation_id, []).append(task_id)
        head = (select(MessageRow.conversation_id.label("conversation_id"),
                func.max(MessageRow.sequence).label("sequence"))
            .where(MessageRow.conversation_id.in_(ids), MessageRow.payload["role"].as_string() == "user",
                task_ref_id.isnot(None)).group_by(MessageRow.conversation_id).subquery())
        latest_rows = (await self.session.scalars(select(MessageRow).join(head, and_(
            MessageRow.conversation_id == head.c.conversation_id,
            MessageRow.sequence == head.c.sequence)))).all()
        latest_task: dict[str, AgentTask] = {}
        for latest_row in latest_rows:
            task_row = task_rows.get(str(latest_row.payload["task_ref"]["id"]))
            if task_row is not None:
                latest_task[latest_row.conversation_id] = task_row

        subjects_by_conversation: dict[str, tuple[ObjectRef, ...]] = {}
        for conversation_row in rows:
            chosen = latest_task.get(conversation_row.id)
            refs = () if chosen is None else tuple(
                ObjectRef.model_validate(ref) for ref in chosen.payload["binding"]["base_refs"])
            listings = [ref for ref in refs if ref.kind == "listing"]
            if not listings:
                listings = [ObjectRef.model_validate(ref) for ref in conversation_row.subject_refs]
            seen: set[int] = set()
            ordered = []
            for ref in listings:
                if isinstance(ref.id, int) and ref.id not in seen:
                    seen.add(ref.id)
                    ordered.append(ref)
            subjects_by_conversation[conversation_row.id] = tuple(ordered)
        listing_ids = {int(ref.id) for refs in subjects_by_conversation.values() for ref in refs}
        identities: dict[int, tuple[str, str]] = {}
        if listing_ids:
            from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
            found = (await self.session.execute(select(Listing, Company.canonical_name)
                .join(Company).where(Listing.id.in_(listing_ids)))).all()
            for listing, name in found:
                ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
                parsed = parse_a_share_ticker(ticker)
                if parsed and parsed[1].market_code == listing.market_code:
                    identities[listing.id] = (name, ticker)

        def aggregate(conversation_id: str) -> ConversationAttention:
            statuses = [task_rows[task_id].status for task_id in by_conversation.get(conversation_id, ())
                if task_id in task_rows]
            if any(status == "waiting_user" for status in statuses):
                return "needs_you"
            if any(status in RUNNING_STATUSES for status in statuses):
                return "running"
            return "none"

        def subject_view(ref: ObjectRef) -> ConversationSubject:
            known = identities.get(int(ref.id))
            return (ConversationSubject(ref=ref, status="available", name=known[0], ticker=known[1])
                if known else ConversationSubject(ref=ref, status="unavailable"))

        items = []
        for conversation_row in rows:
            value = Conversation.model_validate(conversation_row.payload)
            latest = latest_task.get(conversation_row.id)
            view = None
            if latest is not None:
                view = ConversationTaskView(id=UUID(latest.id), status=TaskStatus(latest.status),
                    cancel_requested=bool(latest.payload["cancel_requested"]),
                    stop_reason=StopReason(latest.payload["stop_reason"])
                    if latest.payload["stop_reason"] else None)
            activity = conversation_row.last_activity_at
            items.append(ConversationSummary(id=value.id, title=value.title, created_at=value.created_at,
                last_activity_at=as_utc(activity) if activity is not None else value.created_at,
                title_revision=conversation_row.title_revision,
                subjects=tuple(subject_view(ref) for ref in subjects_by_conversation[conversation_row.id]),
                message_count=counts.get(conversation_row.id, 0), attention=aggregate(conversation_row.id),
                latest_task=view))
        return tuple(items)

"""User-owned operations and trustworthy revocable personalization.

HTTP calls mint authority after the existing authentication/CSRF dependency.
Trusted application policy may bind an exact operation to an authenticated
message. No model tool accepts/mints authority or approves its own proposal.
Caller owns the transaction; receipts, control changes and withdrawals roll
back together. Context APIs are internal, never raw checkpoint HTTP APIs.
"""
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.contracts import Message
from investment_agent.agent.tasks import TaskClaim
from investment_agent.config import Settings
from investment_agent.investor.common import Contract, Mutation, ObjectRef, OwnerId, Record, Text
from investment_agent.investor.contracts import (
    Case,
    Episode,
    Mandate,
    Memory,
    UserDecision,
    WatchlistChange,
)
from investment_agent.investor.enums import CaseStatus, DecisionKind
from investment_agent.investor.foundations import InvestorError, InvestorService, storage_conflicts
from investment_agent.investor.publication import PublicationService
from investment_agent.storage.models.agent import ConversationRow, MessageRow
from investment_agent.storage.models.investor import (
    EpisodeRevision,
    MemoryState,
    UserObject,
    UserObjectRevision,
)


class CaseAction(Contract):
    operation: Literal["case_action"] = "case_action"
    case_ref: ObjectRef
    action: Literal["archive", "reopen", "pause", "resume", "mute", "unmute"]
    reason: Text


class MandateAction(Contract):
    operation: Literal["mandate_revision"] = "mandate_revision"
    candidate: Mandate
    expected_revision: Annotated[int, Field(ge=1)]


class DecisionAction(Contract):
    portfolio_plan_ref: ObjectRef | None = None
    option_id: UUID | None = None
    action_id: UUID | None = None
    operation: Literal["decision"] = "decision"
    recommendation_ref: ObjectRef | None = None
    case_ref: ObjectRef | None = None
    decision: DecisionKind
    user_text: Text
    reason: str | None = None


class MemoryAction(Contract):
    operation: Literal["memory_save"] = "memory_save"
    kind: Literal["explicit_preference", "unverified_observation"]
    text: Text
    scope_refs: tuple[ObjectRef, ...] = ()
    source_refs: tuple[ObjectRef, ...] = ()
    valid_until: datetime | None = None


class RevokeAction(Contract):
    operation: Literal["memory_revoke"] = "memory_revoke"
    memory_ref: ObjectRef
    reason: Text


class WatchlistAddAction(Contract):
    """Authenticated watchlist add; the ticker must be canonical A-share."""

    operation: Literal["watchlist_add"] = "watchlist_add"
    ticker: str
    company_name: str | None = None


Operation = Annotated[CaseAction | MandateAction | DecisionAction | MemoryAction | RevokeAction | WatchlistAddAction, Field(discriminator="operation")]


class ExecuteRequest(Contract):
    parameters: Operation
    idempotency_key: UUID


class Proposal(Record):
    parameters: Operation
    expires_at: datetime
    source: Literal["authenticated_request", "trusted_task"]
    status: Literal["pending", "approved", "declined"]
    authorization_ref: ObjectRef | None = None
    resolution_reason: str | None = None
    result: dict[str, Any] | None = None


class PrepareRequest(ExecuteRequest):
    expires_at: datetime


class ResolveRequest(Contract):
    parameters: Operation
    mutation: Mutation
    approve: bool
    reason: Text


class ContextStamp(Contract):
    revision: Annotated[int, Field(ge=0)]
    built_at: datetime
    valid_until: datetime | None


class MemoryContext(Contract):
    revision: Annotated[int, Field(ge=0)]
    memories: tuple[Memory, ...]
    messages: tuple[Message, ...]
    # Opaque private history and summaries have no complete provenance in AG08.
    # A revocation means they must be discarded, including derived assistant text.
    private_history_allowed: bool
    stamp: ContextStamp


_AUTHORITY = object()


class UserAuthority:
    """Opaque server capability, intentionally outside all Pydantic/tool schemas."""
    def __init__(self, token: object, source: str, parameters: Operation, message_ref: ObjectRef | None = None) -> None:
        if token is not _AUTHORITY:
            raise InvestorError("authorization_required", "Authenticated user instruction required")
        self.source, self.parameters, self.message_ref = source, parameters, message_ref


def authenticated_request(parameters: Operation) -> UserAuthority:
    """Only call in a route after verify_api_key_or_session; never register as tool."""
    return UserAuthority(_AUTHORITY, "authenticated_request", parameters)


async def authenticated_message(session: AsyncSession, ref: ObjectRef, parameters: Operation) -> UserAuthority:
    """Trusted parser/controller binds a CLEAR user instruction to exact parameters.

    A model's suggested operation alone is not an instruction. AG13 must use
    prepare() when semantic intent/parameters are uncertain, then await HTTP
    user resolve. This function validates provenance, not natural language.
    """
    await _user_message(session, ref)
    return UserAuthority(_AUTHORITY, "authenticated_message", parameters, ref)


async def _user_message(session: AsyncSession, ref: ObjectRef) -> Message:
    row = await session.get(MessageRow, str(ref.id))
    if ref.kind != "message" or ref.revision != 1 or ref.field_path is not None or row is None:
        raise InvestorError("invalid_reference", "Source must be an exact authenticated user message")
    conversation = await session.get(ConversationRow, row.conversation_id)
    message = Message.model_validate(row.payload)
    if conversation is None or conversation.owner_id != "local_owner" or message.role != "user":
        raise InvestorError("authorization_required", "Model text cannot authorize a user operation")
    return message


async def memory_revision(session: AsyncSession, owner: str = "local_owner") -> int:
    value = await session.scalar(select(MemoryState.revision).where(MemoryState.owner_id == owner))
    return value or 0


async def memory_deadline(session: AsyncSession, owner: str = "local_owner") -> datetime | None:
    rows = await session.scalars(select(UserObject).where(UserObject.owner_id == owner, UserObject.kind == "memory"))
    now = datetime.now(UTC)
    deadlines = [memory.valid_until for row in rows if (memory := Memory.model_validate(row.payload)).status == "active"
        and memory.valid_until is not None and memory.valid_until > now]
    return min(deadlines) if deadlines else None


class UserControlService(InvestorService):
    owner: OwnerId = "local_owner"

    async def _reserve(self) -> None:
        # Reserve a writer before nested savepoints/read-modify-write on SQLite.
        # This also establishes a physical outer transaction for caller rollback.
        await self.session.execute(insert(MemoryState).values(owner_id=self.owner, revision=0)
            .on_conflict_do_nothing(index_elements=[MemoryState.owner_id]))
        await self.session.execute(update(MemoryState).where(MemoryState.owner_id == self.owner)
            .values(revision=MemoryState.revision))

    def _save(self, kind: str, value: Record) -> None:
        payload = value.model_dump(mode="json")
        self.session.add(UserObject(id=str(value.id), owner_id=self.owner, kind=kind,
            revision=value.revision, payload=payload))
        self.session.add(UserObjectRevision(id=str(value.id), revision=value.revision, payload=payload))

    async def _object(self, ref: ObjectRef, kind: str, *, current: bool = False) -> dict[str, Any]:
        row = await self.session.get(UserObject, str(ref.id), populate_existing=True)
        if ref.kind != kind or ref.field_path is not None or row is None or row.kind != kind or row.owner_id != self.owner:
            raise InvestorError("not_found", f"{kind} does not exist")
        if current and row.revision != ref.revision:
            raise InvestorError("revision_conflict", f"{kind} revision changed")
        saved = await self.session.get(UserObjectRevision, (str(ref.id), ref.revision))
        if saved is None:
            raise InvestorError("invalid_reference", f"{kind} revision does not exist")
        return saved.payload

    async def _advance(self, ref: ObjectRef, value: Record) -> None:
        result = await self.session.execute(update(UserObject).where(UserObject.id == str(ref.id),
            UserObject.owner_id == self.owner, UserObject.revision == ref.revision)
            .values(revision=value.revision, payload=value.model_dump(mode="json")))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise InvestorError("revision_conflict", "Object revision changed")
        self.session.add(UserObjectRevision(id=str(value.id), revision=value.revision,
            payload=value.model_dump(mode="json")))

    async def notifications_allowed(self, case_id: UUID) -> bool:
        """AG15 delivery checks live mute state, without touching validity."""
        return not (await self.get_case(self.owner, case_id)).muted

    async def automatic_reopen_allowed(self, case_id: UUID) -> bool:
        """AG16 must check this before preparing any automatic research episode."""
        case = await self.get_case(self.owner, case_id)
        return case.status == CaseStatus.ARCHIVED and not case.user_archived

    async def get_proposal(self, proposal_id: UUID, revision: int | None = None) -> Proposal:
        row = await self.session.get(UserObject, str(proposal_id), populate_existing=True)
        if row is None or row.owner_id != self.owner or row.kind != "proposal":
            raise InvestorError("not_found", "Proposal does not exist")
        return Proposal.model_validate(await self._object(ObjectRef(kind="proposal", id=proposal_id, revision=revision or row.revision), "proposal"))

    async def list_objects(self, kind: Literal["decision", "memory", "proposal"], *, limit: int = 100,
        after: UUID | None = None, case_id: UUID | None = None, recommendation_id: UUID | None = None,
        scope_refs: tuple[ObjectRef, ...] = (), status: str | None = None) -> list[dict[str, Any]]:
        # UUID order is stable. Apply semantic filters BEFORE limit; callers
        # continue with the last returned id, never a hidden first-100 subset.
        query = select(UserObject).where(UserObject.owner_id == self.owner, UserObject.kind == kind)
        if after is not None:
            query = query.where(UserObject.id > str(after))
        rows = await self.session.scalars(query.order_by(UserObject.id))
        identities = {(ref.kind, str(ref.id)) for ref in scope_refs}
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = row.payload
            if kind == "memory":
                memory = Memory.model_validate(payload)
                if memory.status == "active" and memory.valid_until is not None and memory.valid_until <= datetime.now(UTC):
                    memory = memory.model_copy(update={"status": "expired"})
                payload = memory.model_dump(mode="json")
                if identities and memory.scope_refs and not ({(ref.kind, str(ref.id)) for ref in memory.scope_refs} & identities):
                    continue
            if status is not None and payload.get("status") != status:
                continue
            if case_id is not None and (payload.get("case_ref") or {}).get("id") != str(case_id):
                continue
            if recommendation_id is not None and (payload.get("recommendation_ref") or {}).get("id") != str(recommendation_id):
                continue
            result.append(payload)
            if len(result) >= limit:
                break
        return result

    async def get_memories(self, *, scope_refs: tuple[ObjectRef, ...] = (), active_only: bool = False,
        status: Literal["active", "revoked", "expired"] | None = None, limit: int = 100, after: UUID | None = None) -> tuple[Memory, ...]:
        if active_only and status not in {None, "active"}:
            raise InvestorError("invalid_action", "active_only conflicts with status")
        return tuple(Memory.model_validate(payload) for payload in await self.list_objects("memory", limit=limit,
            after=after, scope_refs=scope_refs, status="active" if active_only else status))

    async def _validate(self, parameters: Operation) -> None:
        if isinstance(parameters, CaseAction):
            if parameters.case_ref.kind != "case" or parameters.case_ref.field_path is not None:
                raise InvestorError("invalid_reference", "Action requires an exact case")
            case = await self.get_case(self.owner, UUID(str(parameters.case_ref.id)))
            if case.revision != parameters.case_ref.revision:
                raise InvestorError("revision_conflict", "Case revision changed")
            if parameters.action == "reopen" and case.status != CaseStatus.ARCHIVED:
                raise InvestorError("invalid_action", "Only archived cases can reopen")
        elif isinstance(parameters, MandateAction):
            current = await self.get_mandate(self.owner)
            if current.id != parameters.candidate.id or current.revision != parameters.expected_revision or parameters.candidate.revision != current.revision + 1:
                raise InvestorError("revision_conflict", "Mandate revision changed")
            # Same reference binding and business validation as direct AG04 writes.
            if parameters.candidate.scope_refs or parameters.candidate.authorization.exploration_scope_refs:
                from investment_agent.agent.conversations import ConversationService
                await ConversationService(self.session, Settings.model_construct()).resolve_context(tuple(dict.fromkeys((*parameters.candidate.scope_refs, *parameters.candidate.authorization.exploration_scope_refs))))
        elif isinstance(parameters, DecisionAction):
            if parameters.portfolio_plan_ref is not None:
                from investment_agent.portfolio.plans import PortfolioPlanService
                if parameters.recommendation_ref is not None or parameters.case_ref is not None or parameters.option_id is None:
                    raise InvestorError("invalid_reference", "Portfolio decision requires exact plan/option and no company target")
                view = await PortfolioPlanService(self.session, self.owner).get(parameters.portfolio_plan_ref)
                option = next((o for o in (view.plan.no_action, *view.plan.options) if o.id == parameters.option_id), None)
                if option is None or (parameters.action_id is not None and not any(a.id == parameters.action_id for a in option.actions)):
                    raise InvestorError("invalid_reference", "Decision option/action is absent from exact plan")
                if parameters.decision.value == "accept" and view.status != "published":
                    raise InvestorError("revision_conflict", "Old plan requires review before acceptance")
                return
            if parameters.option_id is not None or parameters.action_id is not None:
                raise InvestorError("invalid_reference", "Option/action requires exact portfolio plan")
            if parameters.case_ref is None and parameters.recommendation_ref is None:
                raise InvestorError("invalid_reference", "Decision needs a recommendation or case")
            case_id: UUID | None = None
            if parameters.recommendation_ref is not None:
                artifact = await PublicationService(self.session).artifact(parameters.recommendation_ref)
                if artifact.kind != "recommendation":
                    raise InvestorError("invalid_reference", "Decision requires recommendation")
                case_id = UUID(artifact.case_id)
            if parameters.case_ref is not None:
                ref = parameters.case_ref
                if ref.kind != "case" or ref.field_path is not None:
                    raise InvestorError("invalid_reference", "Decision requires case")
                case = await self.get_case(self.owner, UUID(str(ref.id)), ref.revision)
                if case_id is not None and case_id != case.id:
                    raise InvestorError("invalid_reference", "Recommendation and case differ")
        elif isinstance(parameters, MemoryAction):
            if parameters.kind == "unverified_observation" and parameters.valid_until is None:
                raise InvestorError("invalid_action", "Observation requires expiry")
            if parameters.valid_until is not None and parameters.valid_until <= datetime.now(UTC):
                raise InvestorError("invalid_action", "Memory expiry must be in the future")
            for ref in parameters.source_refs:
                await _user_message(self.session, ref)
                if await self._blocked_source(ref):
                    raise InvestorError("source_revoked", "Revoked source cannot yield a new preference")
            for ref in parameters.scope_refs:
                if ref.kind == "case":
                    await self.get_case(self.owner, UUID(str(ref.id)), ref.revision)
                elif ref.kind != "listing":
                    raise InvestorError("invalid_reference", "Memory scope must be a case or listing")
                else:
                    from investment_agent.storage.models.reference import Listing
                    if ref.revision != 1 or await self.session.get(Listing, ref.id) is None:
                        raise InvestorError("invalid_reference", "Listing scope unavailable")
        elif isinstance(parameters, WatchlistAddAction):
            from investment_agent.domain.a_share import parse_a_share_ticker
            from investment_agent.storage.repositories.listing import ListingRepository

            parsed = parse_a_share_ticker(parameters.ticker)
            if parsed is None:
                raise InvestorError("invalid_reference", "Watchlist add requires a canonical A-share ticker (600519.SH)")
            symbol, market = parsed
            listing = await ListingRepository(self.session).get_by_ticker(market.market_code, symbol)
            if listing is None and not (parameters.company_name or "").strip():
                raise InvestorError("invalid_reference", "Unknown ticker requires the directory company name")
        else:
            await self._object(parameters.memory_ref, "memory", current=True)

    async def _blocked_source(self, ref: ObjectRef) -> bool:
        rows = await self.session.scalars(select(UserObject).where(UserObject.owner_id == self.owner, UserObject.kind == "memory"))
        return any((row.payload["status"] == "revoked" or row.payload.get("valid_until") is not None and datetime.fromisoformat(row.payload["valid_until"]) <= datetime.now(UTC)) and any(
            source["kind"] == ref.kind and source["id"] == str(ref.id) for source in row.payload["source_refs"]
        ) for row in rows)

    def _authorize(self, authority: UserAuthority, parameters: Operation) -> None:
        if not isinstance(authority, UserAuthority) or authority.parameters != parameters:
            raise InvestorError("authorization_required", "Exact authenticated instruction required")

    async def _instruction(self, authority: UserAuthority, parameters: Operation) -> ObjectRef:
        self._authorize(authority, parameters)
        if authority.message_ref is not None:
            await _user_message(self.session, authority.message_ref)
            if isinstance(parameters, MemoryAction) and await self._blocked_source(authority.message_ref):
                raise InvestorError("source_revoked", "Old revoked message cannot restore a preference")
        value = Record(id=uuid4(), revision=1, created_at=datetime.now(UTC), actor="user", origin_ref=authority.message_ref)
        payload = value.model_dump(mode="json") | {"source": authority.source, "parameters": parameters.model_dump(mode="json")}
        self.session.add(UserObject(id=str(value.id), owner_id=self.owner, kind="instruction", revision=1, payload=payload))
        self.session.add(UserObjectRevision(id=str(value.id), revision=1, payload=payload))
        await self.session.flush()
        return ObjectRef(kind="instruction", id=value.id, revision=1)

    @storage_conflicts
    async def execute(self, body: ExecuteRequest, authority: UserAuthority) -> Record:
        self._authorize(authority, body.parameters)
        await self._reserve()
        async with self.session.begin_nested():
            request = {"operation": "user_execute", **body.model_dump(mode="json"), "source": authority.source,
                "message_ref": authority.message_ref.model_dump(mode="json") if authority.message_ref else None}
            replay = await self._replay(self.owner, body.idempotency_key, request)
            if replay is not None:
                return self._parse_result(body.parameters, replay)
            await self._validate(body.parameters)
            source = await self._instruction(authority, body.parameters)
            value = await self._apply(body.parameters, source, body.idempotency_key, authority.message_ref)
            self._receipt(self.owner, body.idempotency_key, request, value)
            await self.session.flush()
            return value

    @staticmethod
    def _parse_result(parameters: Operation, payload: dict[str, Any]) -> Record:
        model: type[Record] = Case if isinstance(parameters, CaseAction) else Mandate if isinstance(parameters, MandateAction) else UserDecision if isinstance(parameters, DecisionAction) else WatchlistChange if isinstance(parameters, WatchlistAddAction) else Memory
        return model.model_validate(payload)

    async def _apply(self, parameters: Operation, source: ObjectRef, key: UUID, message_ref: ObjectRef | None = None) -> Record:
        now = datetime.now(UTC)
        if isinstance(parameters, CaseAction):
            case = await self.get_case(self.owner, UUID(str(parameters.case_ref.id)))
            row = await self.repository.case(self.owner, case.id)
            assert row is not None
            changes: dict[str, Any] = {"revision": case.revision + 1, "actor": "user", "origin_ref": source}
            if parameters.action == "archive":
                if case.current_recommendation_ref:
                    await PublicationService(self.session).restrict(case.current_recommendation_ref,
                        reason=parameters.reason, withdrawn=True)
                changes.update(status=CaseStatus.ARCHIVED, user_archived=True, current_recommendation_ref=None, current_thesis_ref=None)
            elif parameters.action == "reopen":
                changes.update(status=CaseStatus.RESEARCHING, user_archived=False, episode=case.episode + 1)
            elif parameters.action in {"pause", "resume"}:
                changes["background_paused"] = parameters.action == "pause"
            else:
                changes["muted"] = parameters.action == "mute"
            value = case.model_copy(update=changes)
            if not await self.repository.advance_case(row, parameters.case_ref.revision, value.model_dump(mode="json")):
                raise InvestorError("revision_conflict", "Case revision changed")
            if parameters.action == "reopen":
                episode = Episode(id=uuid4(), revision=1, created_at=now, actor="user", origin_ref=source,
                    case_ref=ObjectRef(kind="case", id=case.id, revision=value.revision), number=value.episode,
                    question=parameters.reason, plan_refs=(), artifact_refs=())
                self.session.add(EpisodeRevision(id=str(episode.id), revision=1, case_id=str(case.id),
                    number=episode.number, payload=episode.model_dump(mode="json")))
            return value
        if isinstance(parameters, MandateAction):
            # Separate derived key prevents collision with the enclosing receipt.
            from uuid import uuid5
            return await self.revise_mandate(self.owner, parameters.candidate.model_copy(update={"actor": "user", "origin_ref": source, "authorization": parameters.candidate.authorization.model_copy(update={"source_refs": (source,)})}),
                Mutation(expected_revision=parameters.expected_revision, idempotency_key=uuid5(key, "mandate")))
        if isinstance(parameters, DecisionAction):
            decision_case_ref = parameters.case_ref
            if decision_case_ref is None and parameters.recommendation_ref is not None:
                artifact = await PublicationService(self.session).artifact(parameters.recommendation_ref)
                decision_case = await self.get_case(self.owner, UUID(artifact.case_id))
                decision_case_ref = ObjectRef(kind="case", id=decision_case.id, revision=decision_case.revision)
            decision = UserDecision(id=uuid4(), revision=1, created_at=now, actor="user", origin_ref=source,
                recommendation_ref=parameters.recommendation_ref, case_ref=decision_case_ref,
                portfolio_plan_ref=parameters.portfolio_plan_ref, option_id=parameters.option_id, action_id=parameters.action_id,
                decision=parameters.decision, user_text=parameters.user_text, reason=parameters.reason, decided_at=now)
            self._save("decision", decision)
            return decision
        if isinstance(parameters, WatchlistAddAction):
            # The watchlist lives outside the versioned investor objects; the
            # same receipt/instruction provenance still covers this write.
            from investment_agent.clock import shanghai_today
            from investment_agent.domain.a_share import parse_a_share_ticker
            from investment_agent.storage.repositories.listing import ListingRepository
            from investment_agent.storage.repositories.watchlist import WatchlistRepository
            from investment_agent.watchlist.drafts import sync_add_state

            parsed = parse_a_share_ticker(parameters.ticker)
            assert parsed is not None  # _validate rejected non-canonical tickers
            symbol, market = parsed
            listings = ListingRepository(self.session)
            listing = await listings.get_by_ticker(market.market_code, symbol)
            if listing is None:
                listing = await listings.ensure_a_share_listing(parameters.ticker, company_name=parameters.company_name)
            watchlist = WatchlistRepository(self.session)
            already = await watchlist.get_by_listing(listing.id) is not None
            item = await watchlist.add(listing.id, None)
            await sync_add_state(self.session, item, template_id=None, as_of=shanghai_today(), triggered_by="agent_command")
            return WatchlistChange(id=uuid4(), revision=1, created_at=now, actor="user", origin_ref=source,
                listing_ref=ObjectRef(kind="listing", id=listing.id, revision=1), watchlist_id=item.id,
                ticker=parameters.ticker, already_on_watchlist=already, draft_state=item.draft_state)
        if isinstance(parameters, MemoryAction):
            sources = tuple(dict.fromkeys((*parameters.source_refs, *((message_ref,) if message_ref else ()), source)))
            memory = Memory(id=uuid4(), revision=1, created_at=now, actor="user", origin_ref=source,
                kind=parameters.kind, text=parameters.text, scope_refs=parameters.scope_refs,
                source_refs=sources, status="active", valid_until=parameters.valid_until, revoked_at=None, supersedes_ref=None)
            self._save("memory", memory)
        else:
            original = Memory.model_validate(await self._object(parameters.memory_ref, "memory", current=True))
            if original.status == "revoked":
                raise InvestorError("invalid_action", "Memory is already revoked")
            memory = original.model_copy(update={"revision": original.revision + 1, "status": "revoked",
                "revoked_at": now, "actor": "user", "origin_ref": source})
            await self._advance(parameters.memory_ref, memory)
        await self.session.execute(update(MemoryState).where(MemoryState.owner_id == self.owner)
            .values(revision=MemoryState.revision + 1))
        return memory

    @storage_conflicts
    async def prepare(self, body: PrepareRequest, *, claim: TaskClaim | None = None, base: Settings | None = None) -> Proposal:
        """Prepare only. Trusted TaskClaim binds a model suggestion to its task."""
        await self._reserve()
        async with self.session.begin_nested():
            if claim is not None:
                from investment_agent.agent.execution import check_boundary
                if base is None:
                    raise InvestorError("authorization_required", "Trusted settings and task boundary required")
                required = (body.parameters.case_ref,) if isinstance(body.parameters, CaseAction) else ()
                await check_boundary(self.session, claim, base, tool="prepare_user_operation", required_refs=required)
            request = {"operation": "proposal_prepare", **body.model_dump(mode="json"),
                "task_id": str(claim.task_id) if claim else None}
            replay = await self._replay(self.owner, body.idempotency_key, request)
            if replay is not None:
                return Proposal.model_validate(replay)
            if body.expires_at <= datetime.now(UTC):
                raise InvestorError("proposal_expired", "Proposal expiry must be in the future")
            await self._validate(body.parameters)
            value = Proposal(id=uuid4(), revision=1, created_at=datetime.now(UTC), actor="agent" if claim else "user",
                origin_ref=ObjectRef(kind="task", id=claim.task_id, revision=1) if claim else None,
                parameters=body.parameters, expires_at=body.expires_at,
                source="trusted_task" if claim else "authenticated_request", status="pending")
            self._save("proposal", value)
            self._receipt(self.owner, body.idempotency_key, request, value)
            await self.session.flush()
            return value

    @storage_conflicts
    async def resolve(self, proposal_id: UUID, body: ResolveRequest, authority: UserAuthority) -> Proposal:
        self._authorize(authority, body.parameters)
        await self._reserve()
        async with self.session.begin_nested():
            request = {"operation": "proposal_resolve", "proposal_id": str(proposal_id), **body.model_dump(mode="json"),
                "source": authority.source, "message_ref": authority.message_ref.model_dump(mode="json") if authority.message_ref else None}
            replay = await self._replay(self.owner, body.mutation.idempotency_key, request)
            if replay is not None:
                return Proposal.model_validate(replay)
            ref = ObjectRef(kind="proposal", id=proposal_id, revision=body.mutation.expected_revision)
            value = Proposal.model_validate(await self._object(ref, "proposal", current=True))
            if value.status != "pending":
                raise InvestorError("proposal_consumed", "Proposal has already been consumed")
            if value.expires_at <= datetime.now(UTC):
                raise InvestorError("proposal_expired", "Proposal has expired; prepare a new proposal")
            if value.parameters != body.parameters:
                raise InvestorError("proposal_parameters_changed", "Approval must bind exact prepared parameters")
            await self._validate(body.parameters)
            source = await self._instruction(authority, body.parameters)
            result = await self._apply(body.parameters, source, body.mutation.idempotency_key, authority.message_ref) if body.approve else None
            value = value.model_copy(update={"revision": value.revision + 1,
                "status": "approved" if body.approve else "declined", "authorization_ref": source, "resolution_reason": body.reason,
                "result": result.model_dump(mode="json") if result else {"reason": body.reason}})
            await self._advance(ref, value)
            self._receipt(self.owner, body.mutation.idempotency_key, request, value)
            await self.session.flush()
            return value

    @storage_conflicts
    async def save_observation(self, claim: TaskClaim, parameters: MemoryAction, key: UUID, *, base: Settings) -> Memory:
        """Trusted AG13 tool implementation; ordinary observations need no approval.

        AG13 grants save_observation separately. Only expiring unverified facts
        are allowed; task provenance and execution fences remain mandatory.
        """
        from investment_agent.agent.conversations import ConversationService
        from investment_agent.agent.execution import check_boundary
        if parameters.kind != "unverified_observation" or not parameters.source_refs:
            raise InvestorError("authorization_required", "Only sourced, expiring unverified observations may be saved by a task")
        await self._reserve()
        async with self.session.begin_nested():
            await check_boundary(self.session, claim, base, tool="save_observation", required_refs=parameters.scope_refs)
            request = {"operation": "task_observation", "task_id": str(claim.task_id), "parameters": parameters.model_dump(mode="json")}
            replay = await self._replay(self.owner, key, request)
            if replay is not None:
                return Memory.model_validate(replay)
            await self._validate(parameters.model_copy(update={"source_refs": ()}))
            for ref in parameters.source_refs:
                if ref.kind == "message":
                    await _user_message(self.session, ref)
                    if await self._blocked_source(ref):
                        raise InvestorError("source_revoked", "Observation source was revoked")
                elif ref.kind in {"evidence", "calculation", "financial_snapshot", "market_snapshot"}:
                    resolved = await ConversationService(self.session, base).resolve_context((ref,))
                    await check_boundary(self.session, claim, base, tool="save_observation", required_refs=tuple(ref for ref in resolved if ref.kind == "listing"))
                else:
                    raise InvestorError("invalid_reference", "Observation needs original message or saved evidence")
            task_ref = ObjectRef(kind="task", id=claim.task_id, revision=1)
            value = Memory(id=uuid4(), revision=1, created_at=datetime.now(UTC), actor="agent", origin_ref=task_ref,
                kind="unverified_observation", text=parameters.text, scope_refs=parameters.scope_refs,
                source_refs=(*parameters.source_refs, task_ref), status="active", valid_until=parameters.valid_until,
                revoked_at=None, supersedes_ref=None)
            self._save("memory", value)
            # New observations do not invalidate the producing run. Existing
            # context is unchanged; deadline is rechecked dynamically below.
            self._receipt(self.owner, key, request, value)
            await self.session.flush()
            await check_boundary(self.session, claim, base, tool="save_observation")
            return value

    async def context(self, *, conversation_id: UUID | None = None, scope_refs: tuple[ObjectRef, ...] = (),
        private_history_revision: int | None = None, private_history_stamp: ContextStamp | None = None) -> MemoryContext:
        """Trusted AG13 assembly: never use public messages() as model history.

        Once any revocation exists, all derived assistant messages are excluded.
        Whole source messages are excluded (including still-active sibling
        preferences) because fragment provenance is not yet implemented.
        Old private summaries/history are reusable only at the current revision.
        """
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            active_transaction = await connection.run_sync(lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False)))
            if not active_transaction:
                await connection.exec_driver_sql("BEGIN")
        revision = await memory_revision(self.session)
        rows = await self.session.scalars(select(UserObject).where(UserObject.owner_id == self.owner, UserObject.kind == "memory"))
        memories = [Memory.model_validate(row.payload) for row in rows]
        invalid = [value for value in memories if value.status == "revoked" or value.valid_until is not None and value.valid_until <= datetime.now(UTC)]
        blocked = {str(ref.id) for value in invalid for ref in value.source_refs if ref.kind == "message"}
        identities = {(ref.kind, str(ref.id)) for ref in scope_refs}
        now = datetime.now(UTC)
        invalidation_times = [at for value in invalid if (at := value.revoked_at or value.valid_until) is not None]
        latest_revocation = max(invalidation_times, default=None)
        expired_at = max((value.valid_until for value in memories if value.valid_until and value.valid_until <= now), default=None)
        active = tuple(value for value in memories if value.status == "active" and
            not (value.actor == "agent" and latest_revocation is not None and value.created_at <= latest_revocation) and
            (value.valid_until is None or value.valid_until > datetime.now(UTC)) and
            not any(str(ref.id) in blocked for ref in value.source_refs if ref.kind == "message") and
            (not value.scope_refs or bool({(ref.kind, str(ref.id)) for ref in value.scope_refs} & identities)))
        messages: tuple[Message, ...] = ()
        if conversation_id is not None:
            conversation = await self.session.get(ConversationRow, str(conversation_id))
            if conversation is None or conversation.owner_id != self.owner:
                raise InvestorError("not_found", "Conversation does not exist")
            saved = await self.session.scalars(select(MessageRow).where(MessageRow.conversation_id == str(conversation_id)).order_by(MessageRow.sequence))
            messages = tuple(message for row in saved if (message := Message.model_validate(row.payload)).id.__str__() not in blocked and
                (not invalid or message.role == "user"))
        deadline = min((value.valid_until for value in active if value.valid_until), default=None)
        stamp = ContextStamp(revision=revision, built_at=now, valid_until=deadline)
        history_allowed = private_history_revision == revision and not expired_at
        if private_history_stamp is not None:
            history_allowed = private_history_stamp.revision == revision and private_history_stamp.built_at <= now and (
                private_history_stamp.valid_until is None or private_history_stamp.valid_until > now) and (
                expired_at is None or private_history_stamp.built_at >= expired_at)
        return MemoryContext(revision=revision, memories=active, messages=messages,
            private_history_allowed=history_allowed, stamp=stamp)

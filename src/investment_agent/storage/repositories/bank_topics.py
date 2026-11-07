"""Exact owner-scoped history and caller-owned mutation/CAS transactions.

Parsing and storage never promote a fact to trusted original evidence.
"""

import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.bank_topics.contracts import (
    ConditionRef,
    ExistingFactSource,
    FactBinding,
    TopicFactSource,
    TopicRef,
    TopicRevisionRef,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.bank_topics import (
    TopicCase,
    TopicCheckRevision,
    TopicConditionHead,
    TopicConditionRevision,
    TopicFactRevision,
    TopicMutationReceipt,
    TopicRevision,
)


class BankTopicReferenceError(ValueError):
    """Safe read failure: no inaccessible IDs or payloads are included."""

    def __init__(self, code: Literal["not_found", "invalid_reference"]) -> None:
        self.code = code
        super().__init__("bank topic reference unavailable" if code == "not_found" else "bank topic reference invalid")


class BankTopicMutationError(ValueError):
    def __init__(self, code: Literal["idempotency_mismatch", "revision_conflict", "invalid_reference"]) -> None:
        self.code = code
        super().__init__("bank topic mutation rejected")


@dataclass(frozen=True)
class MutationResponse:
    status: int
    body: dict[str, Any]


@dataclass(frozen=True)
class StoredMutation:
    status: int
    body: dict[str, Any]
    replayed: bool


AuthorizeMutation = Callable[[AsyncSession], Awaitable[None]]
ApplyMutation = Callable[[AsyncSession], Awaitable[MutationResponse]]


@dataclass(frozen=True)
class BoundFact:
    binding: FactBinding
    local_fact: TopicFactRevision | None
    external_ref: ObjectRef | None
    resolution: Literal["requires_proof_resolution"] = "requires_proof_resolution"


@dataclass(frozen=True)
class ResolvedTopicReference:
    case: TopicCase
    revision: TopicRevision | None = None
    condition: TopicConditionRevision | None = None
    check: TopicCheckRevision | None = None
    fact: BoundFact | None = None


def _positive(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BankTopicReferenceError("invalid_reference")


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise BankTopicReferenceError("invalid_reference") from exc


class BankTopicRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_case(self, owner_id: str, case_id: UUID) -> TopicCase | None:
        return (await self.session.scalars(select(TopicCase).where(
            TopicCase.owner_id == owner_id, TopicCase.id == str(case_id),
        ).execution_options(populate_existing=True, autoflush=False))).one_or_none()

    async def get_revision(
        self, owner_id: str, case_id: UUID, revision: int,
    ) -> TopicRevision | None:
        _positive(revision)
        return (await self.session.scalars(select(TopicRevision).join(
            TopicCase, TopicCase.id == TopicRevision.case_id,
        ).where(TopicCase.owner_id == owner_id, TopicCase.id == str(case_id),
                TopicRevision.revision == revision, TopicRevision.revision <= TopicCase.head_revision)
            .execution_options(populate_existing=True, autoflush=False))).one_or_none()

    async def history(
        self, owner_id: str, case_id: UUID, *, after_revision: int = 0, limit: int = 100,
    ) -> list[TopicRevision]:
        _positive(limit)
        if limit > 100 or isinstance(after_revision, bool) or not isinstance(after_revision, int) or after_revision < 0:
            raise BankTopicReferenceError("invalid_reference")
        return list(await self.session.scalars(select(TopicRevision).join(
            TopicCase, TopicCase.id == TopicRevision.case_id,
        ).where(TopicCase.owner_id == owner_id, TopicCase.id == str(case_id),
                TopicRevision.revision > after_revision, TopicRevision.revision <= TopicCase.head_revision)
            .order_by(TopicRevision.revision).limit(limit).execution_options(populate_existing=True, autoflush=False)))

    async def resolve_reference(
        self, owner_id: str, reference: TopicRef | ObjectRef, *, listing_id: int | None = None,
    ) -> ResolvedTopicReference:
        try:
            ref = TopicRef.model_validate(reference.model_dump())
        except ValidationError as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        if listing_id is not None:
            _positive(listing_id)
        if ref.kind == "topic_case":
            parent = await self.get_case(owner_id, ref.id)
            revision = await self.get_revision(owner_id, ref.id, ref.revision)
            if parent is None or revision is None:
                raise BankTopicReferenceError("not_found")
            result = ResolvedTopicReference(case=parent, revision=revision)
        elif ref.kind == "topic_revision":
            revision = await self.session.scalar(select(TopicRevision).join(
                TopicCase, TopicCase.id == TopicRevision.case_id,
            ).where(TopicCase.owner_id == owner_id, TopicRevision.id == str(ref.id),
                    TopicRevision.revision <= TopicCase.head_revision)
                .execution_options(populate_existing=True, autoflush=False))
            if revision is None:
                raise BankTopicReferenceError("not_found")
            parent = await self.get_case(owner_id, _uuid(revision.case_id))
            if parent is None:
                raise BankTopicReferenceError("not_found")
            fact = None
            if ref.field_path is not None:
                fact = await self._bound_fact(owner_id, revision, ref.field_path.removeprefix("facts."))
            result = ResolvedTopicReference(case=parent, revision=revision, fact=fact)
        elif ref.kind == "topic_condition":
            condition = await self._condition(owner_id, ref.id, ref.revision)
            if condition is None:
                raise BankTopicReferenceError("not_found")
            parent = await self.get_case(owner_id, _uuid(condition.case_id))
            if parent is None:
                raise BankTopicReferenceError("not_found")
            result = ResolvedTopicReference(case=parent, condition=condition)
        else:
            check = await self.session.scalar(select(TopicCheckRevision).where(
                TopicCheckRevision.owner_id == owner_id, TopicCheckRevision.id == str(ref.id),
            ).execution_options(populate_existing=True, autoflush=False))
            if check is None:
                raise BankTopicReferenceError("not_found")
            condition = await self._condition(owner_id, _uuid(check.condition_id), check.condition_revision)
            if condition is None:
                raise BankTopicReferenceError("not_found")
            try:
                condition_ref = ConditionRef.model_validate(check.condition_ref)
            except ValidationError as exc:
                raise BankTopicReferenceError("invalid_reference") from exc
            if str(condition_ref.id) != check.condition_id or condition_ref.revision != check.condition_revision:
                raise BankTopicReferenceError("invalid_reference")
            parent = await self.get_case(owner_id, _uuid(condition.case_id))
            if parent is None:
                raise BankTopicReferenceError("not_found")
            result = ResolvedTopicReference(case=parent, condition=condition, check=check)
        if listing_id is not None and result.case.listing_id != listing_id:
            raise BankTopicReferenceError("not_found")
        return result

    async def _condition(
        self, owner_id: str, condition_id: UUID, revision: int,
    ) -> TopicConditionRevision | None:
        _positive(revision)
        row = (await self.session.scalars(select(TopicConditionRevision).join(
            TopicCase, TopicCase.id == TopicConditionRevision.case_id,
        ).join(
            TopicConditionHead, TopicConditionHead.condition_id == TopicConditionRevision.condition_id,
        ).where(TopicConditionRevision.owner_id == owner_id, TopicCase.owner_id == owner_id,
                TopicConditionHead.owner_id == owner_id,
                TopicConditionHead.case_id == TopicConditionRevision.case_id,
                TopicConditionHead.head_revision >= TopicConditionRevision.revision,
                TopicConditionRevision.condition_id == str(condition_id), TopicConditionRevision.revision == revision)
            .execution_options(populate_existing=True, autoflush=False))).one_or_none()
        if row is not None:
            try:
                parent_ref = TopicRevisionRef.model_validate(row.topic_revision_ref)
            except ValidationError as exc:
                raise BankTopicReferenceError("invalid_reference") from exc
            parent = await self.session.scalar(select(TopicRevision).join(
                TopicCase, TopicCase.id == TopicRevision.case_id,
            ).where(TopicRevision.id == str(parent_ref.id), TopicCase.owner_id == owner_id,
                    TopicRevision.revision <= TopicCase.head_revision)
                .execution_options(populate_existing=True, autoflush=False))
            if parent is None:
                raise BankTopicReferenceError("not_found")
            if parent.case_id != row.case_id:
                raise BankTopicReferenceError("invalid_reference")
        return row

    async def _bound_fact(
        self, owner_id: str, parent: TopicRevision, stable_fact_id: str,
    ) -> BoundFact:
        if not isinstance(parent.fact_bindings, list) or len(parent.fact_bindings) > 100:
            raise BankTopicReferenceError("invalid_reference")
        try:
            bindings = [FactBinding.model_validate(value) for value in parent.fact_bindings]
        except ValidationError as exc:
            raise BankTopicReferenceError("invalid_reference") from exc
        ids = [binding.stable_fact_id for binding in bindings]
        if len(ids) != len(set(ids)):
            raise BankTopicReferenceError("invalid_reference")
        binding = next((value for value in bindings if value.stable_fact_id == stable_fact_id), None)
        if binding is None:
            raise BankTopicReferenceError("invalid_reference")
        if isinstance(binding.source, ExistingFactSource):
            # BT-013 must resolve exact external evidence and its original proof.
            return BoundFact(binding=binding, local_fact=None, external_ref=binding.source.source_ref)
        assert isinstance(binding.source, TopicFactSource)
        fact_ref = binding.source.fact_ref
        fact = await self.session.scalar(select(TopicFactRevision).where(
            TopicFactRevision.id == str(fact_ref.id), TopicFactRevision.revision == fact_ref.revision,
            TopicFactRevision.owner_id == owner_id, TopicFactRevision.case_id == parent.case_id,
            TopicFactRevision.stable_fact_id == binding.stable_fact_id, TopicFactRevision.metric == binding.metric,
        ).execution_options(populate_existing=True, autoflush=False))
        if fact is None:
            raise BankTopicReferenceError("not_found")
        return BoundFact(binding=binding, local_fact=fact, external_ref=None)

    async def run_mutation(
        self, *, owner_id: str, mutation_id: UUID, route: str, object_identity: str,
        request_hash: str, authorize: AuthorizeMutation, apply: ApplyMutation,
        case_id: UUID | None = None,
    ) -> StoredMutation:
        """Internal service unit of work; callbacks are trusted code, never request scripts.

        ``authorize`` must read current access for the exact request object and
        raise before receipt lookup if access has been revoked. It is called on
        every replay as well. Feature/new-write gates and DTO/proof validation
        belong in ``apply`` so a currently authorized receipt can replay after a
        head or feature change. This method neither commits nor opens a session.
        """
        if not route or len(route) > 160 or not object_identity or len(object_identity) > 160:
            raise BankTopicMutationError("invalid_reference")
        if not re.fullmatch(r"[0-9a-f]{64}", request_hash):
            raise BankTopicMutationError("invalid_reference")
        if self.session.new or self.session.dirty or self.session.deleted:
            raise BankTopicMutationError("invalid_reference")
        # A real SQLite DML statement reserves the writer before any receipt or
        # access reads. Crucially it precedes SAVEPOINT, so RELEASE cannot commit
        # a deferred outer transaction. Zero matched rows still reserves SQLite.
        await self.session.execute(update(TopicCase).where(
            TopicCase.owner_id == owner_id, TopicCase.id == (str(case_id) if case_id else ""),
        ).values(head_revision=TopicCase.head_revision)
            .execution_options(synchronize_session=False, autoflush=False))
        async with self.session.begin_nested():
            await authorize(self.session)
            if self.session.new or self.session.dirty or self.session.deleted:
                raise BankTopicMutationError("invalid_reference")
            previous = await self.session.scalar(select(TopicMutationReceipt).where(
                TopicMutationReceipt.owner_id == owner_id,
                TopicMutationReceipt.mutation_id == str(mutation_id),
            ).execution_options(populate_existing=True, autoflush=False))
            if previous is not None:
                if (previous.route, previous.object_identity, previous.request_hash) != (
                    route, object_identity, request_hash,
                ):
                    raise BankTopicMutationError("idempotency_mismatch")
                return StoredMutation(previous.status, deepcopy(previous.response), True)
            response = await apply(self.session)
            if isinstance(response.status, bool) or not isinstance(response.status, int) or not 100 <= response.status <= 599:
                raise BankTopicMutationError("invalid_reference")
            self.session.add(TopicMutationReceipt(
                owner_id=owner_id, mutation_id=str(mutation_id), route=route,
                object_identity=object_identity, request_hash=request_hash,
                response=deepcopy(response.body), status=response.status, created_at=datetime.now(UTC),
            ))
            await self.session.flush()
            return StoredMutation(response.status, deepcopy(response.body), False)

    async def cas_case_head(
        self, owner_id: str, case_id: UUID, *, expected_revision: int,
        expected_control_revision: int | None = None,
    ) -> None:
        _positive(expected_revision)
        filters = [TopicCase.owner_id == owner_id, TopicCase.id == str(case_id),
                   TopicCase.head_revision == expected_revision]
        if expected_control_revision is not None:
            _positive(expected_control_revision)
            filters.append(TopicCase.control_revision == expected_control_revision)
        result = await self.session.execute(update(TopicCase).where(*filters).values(
            head_revision=expected_revision + 1,
        ).execution_options(synchronize_session=False, autoflush=False))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise BankTopicMutationError("revision_conflict")

    async def cas_case_control(
        self, owner_id: str, case_id: UUID, *, expected_control_revision: int,
        state: Literal["draft", "ready", "archived"],
    ) -> None:
        _positive(expected_control_revision)
        if state not in {"draft", "ready", "archived"}:
            raise BankTopicMutationError("invalid_reference")
        result = await self.session.execute(update(TopicCase).where(
            TopicCase.owner_id == owner_id, TopicCase.id == str(case_id),
            TopicCase.control_revision == expected_control_revision,
        ).values(control_revision=expected_control_revision + 1, state=state)
            .execution_options(synchronize_session=False, autoflush=False))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise BankTopicMutationError("revision_conflict")

    async def cas_condition_head(
        self, owner_id: str, case_id: UUID, condition_id: UUID, *, expected_revision: int,
        expected_control_revision: int,
    ) -> None:
        _positive(expected_revision)
        _positive(expected_control_revision)
        result = await self.session.execute(update(TopicConditionHead).where(
            TopicConditionHead.owner_id == owner_id, TopicConditionHead.case_id == str(case_id),
            TopicConditionHead.condition_id == str(condition_id),
            TopicConditionHead.head_revision == expected_revision,
            TopicConditionHead.control_revision == expected_control_revision,
            exists(select(TopicCase.id).where(TopicCase.id == str(case_id), TopicCase.owner_id == owner_id)),
        ).values(head_revision=expected_revision + 1)
            .execution_options(synchronize_session=False, autoflush=False))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise BankTopicMutationError("revision_conflict")

    async def cas_condition_control(
        self, owner_id: str, case_id: UUID, condition_id: UUID, *, expected_control_revision: int,
        state: Literal["draft", "armed", "paused", "cancelled", "completed", "expired"],
        next_due_at: datetime | None,
    ) -> None:
        _positive(expected_control_revision)
        if state not in {"draft", "armed", "paused", "cancelled", "completed", "expired"}:
            raise BankTopicMutationError("invalid_reference")
        result = await self.session.execute(update(TopicConditionHead).where(
            TopicConditionHead.owner_id == owner_id, TopicConditionHead.case_id == str(case_id),
            TopicConditionHead.condition_id == str(condition_id),
            TopicConditionHead.control_revision == expected_control_revision,
            exists(select(TopicCase.id).where(TopicCase.id == str(case_id), TopicCase.owner_id == owner_id)),
        ).values(control_revision=expected_control_revision + 1, state=state, next_due_at=next_due_at)
            .execution_options(synchronize_session=False, autoflush=False))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise BankTopicMutationError("revision_conflict")

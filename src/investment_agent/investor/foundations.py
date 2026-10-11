"""AG04 application service. Writes are atomic; no recommendation/control side effects."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import (
    BusinessError,
    Contract,
    Mutation,
    ObjectRef,
    OwnerId,
    Text,
)
from investment_agent.investor.contracts import Case, Episode, Mandate, ResearchPlan
from investment_agent.investor.enums import CaseStatus
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.investor import (
    CaseRevision,
    EpisodeRevision,
    InvestorCase,
    InvestorMutation,
    MandateHead,
    MandateRevision,
    PlanRevision,
)
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.investor import InvestorRepository


class InvestorError(Exception):
    def __init__(self, code: str, message: str, refs: tuple[ObjectRef, ...] = ()) -> None:
        self.detail = BusinessError(
            code=code, message=message, retryable=code == "revision_conflict", current_refs=refs
        )
        super().__init__(message)


def storage_conflicts[**P, T](fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Savepoints roll back rejected writes; SQLite races expose a retryable 409."""

    @wraps(fn)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except IntegrityError as exc:
            raise InvestorError("revision_conflict", "Concurrent write; reload and retry") from exc
        except OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            raise InvestorError(
                "revision_conflict", "Concurrent SQLite write; reload and retry"
            ) from exc

    return wrapped


class CreateCase(Contract):
    listing_ref: ObjectRef
    question: Text
    idempotency_key: UUID
    pinned: bool = False
    source_refs: tuple[ObjectRef, ...] = ()


class CaseDetail(Contract):
    case: Case
    episodes: tuple[Episode, ...]
    plans: tuple[ResearchPlan, ...]
    source_refs: tuple[ObjectRef, ...]


class InvestorService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = InvestorRepository(session)

    async def get_mandate(self, user_id: OwnerId, revision: int | None = None) -> Mandate:
        row = await self.repository.mandate(user_id, revision)
        if row is None:
            raise InvestorError("not_found", "Investment mandate does not exist")
        return Mandate.model_validate(row.payload)

    async def get_case(self, user_id: OwnerId, case_id: UUID, revision: int | None = None) -> Case:
        row = await self.repository.case(user_id, case_id)
        if row is None:
            raise InvestorError("not_found", "Research case does not exist")
        if revision is not None:
            historical = await self.session.get(CaseRevision, (str(case_id), revision))
            if historical is None:
                raise InvestorError("not_found", "Case revision does not exist")
            return Case.model_validate(historical.payload)
        return Case.model_validate(row.payload)

    async def detail(self, user_id: OwnerId, case_id: UUID) -> CaseDetail:
        case = await self.get_case(user_id, case_id)
        row = await self.repository.case(user_id, case_id)
        assert row is not None
        episodes = await self.session.scalars(
            select(EpisodeRevision)
            .where(EpisodeRevision.case_id == str(case_id))
            .order_by(EpisodeRevision.number, EpisodeRevision.revision)
        )
        plans = await self.session.scalars(
            select(PlanRevision)
            .where(PlanRevision.case_id == str(case_id))
            .order_by(PlanRevision.id, PlanRevision.revision)
        )
        return CaseDetail(
            case=case,
            episodes=tuple(Episode.model_validate(r.payload) for r in episodes),
            plans=tuple(ResearchPlan.model_validate(r.payload) for r in plans),
            source_refs=tuple(ObjectRef.model_validate(r) for r in row.source_refs),
        )

    async def _replay(
        self, owner: OwnerId, key: UUID, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        digest = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        row = await self.session.get(InvestorMutation, (owner, str(key)))
        if row is None:
            return None
        if row.request_hash != digest:
            raise InvestorError(
                "idempotency_conflict", "Idempotency key was used for a different request"
            )
        return row.result

    def _receipt(
        self, owner: OwnerId, key: UUID, request: dict[str, Any], result: Contract
    ) -> None:
        digest = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.session.add(
            InvestorMutation(
                owner_id=owner,
                key=str(key),
                request_hash=digest,
                result=result.model_dump(mode="json"),
            )
        )

    @storage_conflicts
    async def create_mandate(self, user_id: OwnerId, candidate: Mandate, key: UUID) -> Mandate:
        request = {"operation": "create_mandate", "candidate": candidate.model_dump(mode="json")}
        async with self.session.begin_nested():
            replay = await self._replay(user_id, key, request)
            if replay is not None:
                return Mandate.model_validate(replay)
            if candidate.revision != 1 or await self.repository.mandate(user_id) is not None:
                raise InvestorError(
                    "revision_conflict", "Mandate already exists or initial revision is not 1"
                )
            result = candidate.model_copy(
                update={"id": uuid4(), "created_at": datetime.now(UTC), "actor": "user"}
            )
            self.session.add(MandateHead(owner_id=user_id, mandate_id=str(result.id), revision=1))
            self.session.add(
                MandateRevision(
                    owner_id=user_id,
                    mandate_id=str(result.id),
                    revision=1,
                    payload=result.model_dump(mode="json"),
                )
            )
            self._receipt(user_id, key, request, result)
            try:
                await self.session.flush()
            except IntegrityError as exc:
                raise InvestorError("revision_conflict", "Concurrent mandate creation") from exc
        return result

    @storage_conflicts
    async def revise_mandate(
        self, user_id: OwnerId, candidate: Mandate, mutation: Mutation
    ) -> Mandate:
        request = {
            "operation": "revise_mandate",
            "candidate": candidate.model_dump(mode="json"),
            "mutation": mutation.model_dump(mode="json"),
        }
        async with self.session.begin_nested():
            replay = await self._replay(user_id, mutation.idempotency_key, request)
            if replay is not None:
                return Mandate.model_validate(replay)
            current = await self.get_mandate(user_id)
            ref = ObjectRef(kind="mandate", id=current.id, revision=current.revision)
            if (
                candidate.id != current.id
                or candidate.revision != mutation.expected_revision + 1
                or not await self.repository.advance_mandate(user_id, mutation.expected_revision)
            ):
                raise InvestorError("revision_conflict", "Mandate revision changed", (ref,))
            result = candidate.model_copy(update={"created_at": datetime.now(UTC), "actor": "user"})
            self.session.add(
                MandateRevision(
                    owner_id=user_id,
                    mandate_id=str(result.id),
                    revision=result.revision,
                    payload=result.model_dump(mode="json"),
                )
            )
            self._receipt(user_id, mutation.idempotency_key, request, result)
            await self.session.flush()
        return result

    @storage_conflicts
    async def create_case(self, user_id: OwnerId, body: CreateCase, *, trusted_actor: str = "user") -> Case:
        request = {"operation": "create_case", "body": body.model_dump(mode="json")}
        if trusted_actor != "user":
            request["actor"] = trusted_actor
        async with self.session.begin_nested():
            replay = await self._replay(user_id, body.idempotency_key, request)
            if replay is not None:
                return Case.model_validate(replay)
            if (
                body.listing_ref.kind != "listing"
                or body.listing_ref.revision != 1
                or body.listing_ref.field_path is not None
                or await self.session.get(Listing, body.listing_ref.id) is None
            ):
                raise InvestorError("invalid_reference", "A real listing reference is required")
            for ref in body.source_refs:
                if (
                    ref.field_path is not None
                    or ref.revision != 1
                    or ref.kind not in {"candidate", "watchlist_item"}
                ):
                    raise InvestorError(
                        "invalid_reference", "Case source must be a candidate or watchlist item"
                    )
                source = (
                    await self.session.get(CandidateRecord, ref.id)
                    if ref.kind == "candidate"
                    else await self.session.get(WatchlistItem, ref.id)
                )
                if source is None or source.listing_id != body.listing_ref.id:
                    raise InvestorError(
                        "invalid_reference",
                        "Case source belongs to another listing or does not exist",
                    )
            existing = await self.repository.cases(user_id, int(body.listing_ref.id))
            if existing:
                row = existing[0]
                result = Case.model_validate(row.payload)
            else:
                now = datetime.now(UTC)
                result = Case(
                    id=uuid4(),
                    revision=1,
                    created_at=now,
                    actor=trusted_actor,
                    origin_ref=body.source_refs[0] if body.source_refs else None,
                    user_id=user_id,
                    listing_ref=body.listing_ref,
                    status=CaseStatus.LEAD,
                    episode=1,
                    pinned=body.pinned,
                    current_thesis_ref=None,
                    current_recommendation_ref=None,
                )
                self.session.add(
                    InvestorCase(
                        id=str(result.id),
                        owner_id=user_id,
                        listing_id=int(body.listing_ref.id),
                        revision=1,
                        payload=result.model_dump(mode="json"),
                        source_refs=[r.model_dump(mode="json") for r in body.source_refs],
                    )
                )
                try:
                    await self.session.flush()
                except IntegrityError as exc:
                    raise InvestorError("revision_conflict", "Concurrent case creation") from exc
                self.session.add(
                    CaseRevision(
                        case_id=str(result.id), revision=1, payload=result.model_dump(mode="json")
                    )
                )
                episode = Episode(
                    id=uuid4(),
                    revision=1,
                    created_at=now,
                    actor="user",
                    origin_ref=result.origin_ref,
                    case_ref=ObjectRef(kind="case", id=result.id, revision=1),
                    number=1,
                    question=body.question,
                    plan_refs=(),
                    artifact_refs=(),
                )
                self.session.add(
                    EpisodeRevision(
                        id=str(episode.id),
                        revision=1,
                        case_id=str(result.id),
                        number=1,
                        payload=episode.model_dump(mode="json"),
                    )
                )
            self._receipt(user_id, body.idempotency_key, request, result)
            try:
                await self.session.flush()
            except IntegrityError as exc:
                raise InvestorError(
                    "revision_conflict", "Concurrent case creation; retry against current case"
                ) from exc
        return result

    @storage_conflicts
    async def set_pinned(
        self, user_id: OwnerId, case_id: UUID, pinned: bool, mutation: Mutation
    ) -> Case:
        request = {
            "operation": "pin",
            "case_id": str(case_id),
            "pinned": pinned,
            "mutation": mutation.model_dump(mode="json"),
        }
        async with self.session.begin_nested():
            replay = await self._replay(user_id, mutation.idempotency_key, request)
            if replay is not None:
                return Case.model_validate(replay)
            current = await self.get_case(user_id, case_id)
            row = await self.repository.case(user_id, case_id)
            assert row is not None
            if current.revision != mutation.expected_revision:
                raise InvestorError(
                    "revision_conflict",
                    "Case revision changed",
                    (ObjectRef(kind="case", id=current.id, revision=current.revision),),
                )
            result = current.model_copy(
                update={"revision": mutation.expected_revision + 1, "pinned": pinned}
            )
            if not await self.repository.advance_case(
                row, mutation.expected_revision, result.model_dump(mode="json")
            ):
                raise InvestorError(
                    "revision_conflict",
                    "Case revision changed",
                    (ObjectRef(kind="case", id=current.id, revision=current.revision),),
                )
            self._receipt(user_id, mutation.idempotency_key, request, result)
            await self.session.flush()
        return result

    @storage_conflicts
    async def save_plan(
        self, user_id: OwnerId, case_id: UUID, candidate: ResearchPlan, mutation: Mutation
    ) -> ResearchPlan:
        request = {
            "operation": "save_plan",
            "case_id": str(case_id),
            "candidate": candidate.model_dump(mode="json"),
            "mutation": mutation.model_dump(mode="json"),
        }
        async with self.session.begin_nested():
            replay = await self._replay(user_id, mutation.idempotency_key, request)
            if replay is not None:
                return ResearchPlan.model_validate(replay)
            current = await self.get_case(user_id, case_id)
            episode_row = await self.session.scalar(
                select(EpisodeRevision)
                .where(
                    EpisodeRevision.case_id == str(case_id),
                    EpisodeRevision.number == current.episode,
                )
                .order_by(EpisodeRevision.revision.desc())
                .limit(1)
            )
            assert episode_row is not None
            episode = Episode.model_validate(episode_row.payload)
            if candidate.episode_ref != ObjectRef(
                kind="episode", id=episode.id, revision=episode.revision
            ):
                raise InvestorError(
                    "invalid_reference", "Plan must reference the current episode revision"
                )
            budget = await self.repository.mandate(user_id, candidate.budget_ref.revision)
            if (
                candidate.budget_ref.kind != "mandate"
                or candidate.budget_ref.field_path is not None
                or budget is None
                or budget.mandate_id != str(candidate.budget_ref.id)
            ):
                raise InvestorError(
                    "invalid_reference", "Plan budget must reference an owned mandate revision"
                )
            prior = await self.session.scalar(
                select(PlanRevision)
                .where(PlanRevision.id == str(candidate.id))
                .order_by(PlanRevision.revision.desc())
                .limit(1)
            )
            if (prior is None and candidate.revision != 1) or (
                prior is not None
                and (prior.case_id != str(case_id) or candidate.revision != prior.revision + 1)
            ):
                raise InvestorError("revision_conflict", "Plan revision changed")
            row = await self.repository.case(user_id, case_id)
            assert row is not None
            if current.revision != mutation.expected_revision:
                raise InvestorError(
                    "revision_conflict",
                    "Case revision changed",
                    (ObjectRef(kind="case", id=current.id, revision=current.revision),),
                )
            updated = current.model_copy(update={"revision": mutation.expected_revision + 1})
            if not await self.repository.advance_case(
                row, mutation.expected_revision, updated.model_dump(mode="json")
            ):
                raise InvestorError(
                    "revision_conflict",
                    "Case revision changed",
                    (ObjectRef(kind="case", id=current.id, revision=current.revision),),
                )
            result = candidate.model_copy(
                update={
                    "id": uuid4() if prior is None else candidate.id,
                    "created_at": datetime.now(UTC),
                    "actor": "user",
                }
            )
            self.session.add(
                PlanRevision(
                    id=str(result.id),
                    revision=result.revision,
                    case_id=str(case_id),
                    payload=result.model_dump(mode="json"),
                )
            )
            refs = tuple(r for r in episode.plan_refs if r.id != result.id) + (
                ObjectRef(kind="plan", id=result.id, revision=result.revision),
            )
            revised = episode.model_copy(
                update={
                    "revision": episode.revision + 1,
                    "created_at": datetime.now(UTC),
                    "plan_refs": refs,
                }
            )
            self.session.add(
                EpisodeRevision(
                    id=str(revised.id),
                    revision=revised.revision,
                    case_id=str(case_id),
                    number=revised.number,
                    payload=revised.model_dump(mode="json"),
                )
            )
            self._receipt(user_id, mutation.idempotency_key, request, result)
            await self.session.flush()
        return result

    @storage_conflicts
    async def attach_artifacts(
        self, user_id: OwnerId, case_id: UUID, refs: tuple[ObjectRef, ...], mutation: Mutation
    ) -> Episode:
        request = {
            "operation": "attach_artifacts",
            "case_id": str(case_id),
            "refs": [r.model_dump(mode="json") for r in refs],
            "mutation": mutation.model_dump(mode="json"),
        }
        async with self.session.begin_nested():
            replay = await self._replay(user_id, mutation.idempotency_key, request)
            if replay is not None:
                return Episode.model_validate(replay)
            current = await self.get_case(user_id, case_id)
            episode_row = await self.session.scalar(
                select(EpisodeRevision)
                .where(
                    EpisodeRevision.case_id == str(case_id),
                    EpisodeRevision.number == current.episode,
                )
                .order_by(EpisodeRevision.revision.desc())
                .limit(1)
            )
            assert episode_row is not None
            episode = Episode.model_validate(episode_row.payload)
            from investment_agent.evidence.store import EvidenceService

            evidence_store = EvidenceService(self.session)
            for ref in refs:
                valid = False
                if ref.field_path is None and ref.kind == "plan":
                    plan = await self.session.get(PlanRevision, (str(ref.id), ref.revision))
                    valid = plan is not None and plan.case_id == str(case_id)
                elif ref.field_path is None and ref.kind == "evidence":
                    material = await evidence_store.get_evidence(user_id, ref, datetime.now(UTC))
                    valid = (
                        material.data is not None
                        and material.data.subject_ref == current.listing_ref
                    )
                elif ref.field_path is None and ref.kind == "calculation":
                    calculation = await evidence_store.get_calculation(
                        user_id, ref, datetime.now(UTC)
                    )
                    if calculation.data is not None and calculation.data.fact_refs:
                        valid = True
                        for fact in calculation.data.fact_refs:
                            material = await evidence_store.get_evidence(
                                user_id, fact, datetime.now(UTC)
                            )
                            if (
                                material.data is None
                                or material.data.subject_ref != current.listing_ref
                            ):
                                valid = False
                if not valid:
                    raise InvestorError(
                        "invalid_reference",
                        "Artifact must resolve to an owned plan, evidence or calculation in this case",
                    )
            row = await self.repository.case(user_id, case_id)
            assert row is not None
            if current.revision != mutation.expected_revision:
                raise InvestorError(
                    "revision_conflict",
                    "Case revision changed",
                    (ObjectRef(kind="case", id=current.id, revision=current.revision),),
                )
            updated = current.model_copy(update={"revision": mutation.expected_revision + 1})
            if not await self.repository.advance_case(
                row, mutation.expected_revision, updated.model_dump(mode="json")
            ):
                raise InvestorError(
                    "revision_conflict",
                    "Case revision changed",
                    (ObjectRef(kind="case", id=current.id, revision=current.revision),),
                )
            result = episode.model_copy(
                update={
                    "revision": episode.revision + 1,
                    "created_at": datetime.now(UTC),
                    "artifact_refs": episode.artifact_refs
                    + tuple(r for r in refs if r not in episode.artifact_refs),
                }
            )
            self.session.add(
                EpisodeRevision(
                    id=str(result.id),
                    revision=result.revision,
                    case_id=str(case_id),
                    number=result.number,
                    payload=result.model_dump(mode="json"),
                )
            )
            self._receipt(user_id, mutation.idempotency_key, request, result)
            await self.session.flush()
        return result

    @storage_conflicts
    async def link_source(
        self, user_id: OwnerId, case_id: UUID, ref: ObjectRef, mutation: Mutation
    ) -> Case:
        request = {
            "operation": "link_source",
            "case_id": str(case_id),
            "ref": ref.model_dump(mode="json"),
            "mutation": mutation.model_dump(mode="json"),
        }
        async with self.session.begin_nested():
            replay = await self._replay(user_id, mutation.idempotency_key, request)
            if replay is not None:
                return Case.model_validate(replay)
            current = await self.get_case(user_id, case_id)
            if (
                ref.kind not in {"candidate", "watchlist_item"}
                or ref.field_path is not None
                or ref.revision != 1
            ):
                raise InvestorError(
                    "invalid_reference", "Source must be a candidate or watchlist item"
                )
            source = (
                await self.session.get(CandidateRecord, ref.id)
                if ref.kind == "candidate"
                else await self.session.get(WatchlistItem, ref.id)
            )
            if source is None or source.listing_id != current.listing_ref.id:
                raise InvestorError("invalid_reference", "Source must belong to the case listing")
            row = await self.repository.case(user_id, case_id)
            assert row is not None
            if current.revision != mutation.expected_revision:
                raise InvestorError(
                    "revision_conflict",
                    "Case revision changed",
                    (ObjectRef(kind="case", id=current.id, revision=current.revision),),
                )
            result = current.model_copy(update={"revision": mutation.expected_revision + 1})
            if not await self.repository.advance_case(
                row, mutation.expected_revision, result.model_dump(mode="json")
            ):
                raise InvestorError(
                    "revision_conflict",
                    "Case revision changed",
                    (ObjectRef(kind="case", id=current.id, revision=current.revision),),
                )
            payload = ref.model_dump(mode="json")
            if payload not in row.source_refs:
                row.source_refs = row.source_refs + [payload]
            self._receipt(user_id, mutation.idempotency_key, request, result)
            await self.session.flush()
        return result

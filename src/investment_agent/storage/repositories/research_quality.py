"""QT-006: owner-scoped append storage in the caller's short transaction.

No method commits, creates sessions, parses files or calls a model/provider.
Authorization callbacks must revalidate current access, before any replay.
"""

import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.research_events.canonical import normalize, payload_hash
from investment_agent.research_quality.contracts import (
    EXPERIMENT_TRANSITIONS,
    AssessmentInput,
    ExperimentState,
    MutationOperation,
    MutationReceipt,
    TrialState,
    _BlindMap,
)
from investment_agent.research_quality.refs import (
    PUBLIC_ARTIFACT_KINDS,
    QualityArtifactKind,
    QualityArtifactRef,
    _BlindMapRef,
    parse_public_ref,
)
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.investor import MethodArtifact
from investment_agent.storage.models.research_quality import (
    ResearchAssessment,
    ResearchExperiment,
    ResearchExperimentReceipt,
    ResearchExperimentTask,
    ResearchTrial,
)


class QualityStorageError(ValueError):
    def __init__(
        self,
        code: Literal[
            "not_found",
            "invalid_reference",
            "revision_conflict",
            "idempotency_mismatch",
            "artifact_hash_mismatch",
        ],
    ):
        self.code = code
        super().__init__("research quality storage request rejected")


@dataclass(frozen=True)
class StoredArtifact:
    ref: QualityArtifactRef
    payload_hash: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class StoredMutation:
    receipt: MutationReceipt
    replayed: bool


AuthorizeMutation = Callable[[AsyncSession], Awaitable[None]]
ApplyMutation = Callable[[AsyncSession], Awaitable[MutationReceipt]]


def _hash(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise QualityStorageError("invalid_reference")


def _positive(value: int) -> None:
    if type(value) is not int or value < 1:
        raise QualityStorageError("invalid_reference")


def _owner(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 32:
        raise QualityStorageError("invalid_reference")


class QualityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _reserve(self) -> None:
        # A zero-row SQLite DML starts the real outer write transaction before
        # any SAVEPOINT. RELEASE then cannot accidentally commit an outer read.
        await self.session.execute(
            update(ResearchExperiment)
            .where(
                ResearchExperiment.id == "",
            )
            .values(revision=ResearchExperiment.revision)
            .execution_options(
                synchronize_session=False,
                autoflush=False,
            )
        )

    async def get_experiment(self, owner_id: str, experiment_id: UUID) -> ResearchExperiment:
        _owner(owner_id)
        row = await self.session.scalar(
            select(ResearchExperiment)
            .where(
                ResearchExperiment.owner_id == owner_id,
                ResearchExperiment.id == str(experiment_id),
            )
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            raise QualityStorageError("not_found")
        return row

    async def get_trial(self, owner_id: str, trial_id: UUID) -> ResearchTrial:
        _owner(owner_id)
        row = await self.session.scalar(
            select(ResearchTrial)
            .join(
                ResearchExperiment,
                ResearchExperiment.id == ResearchTrial.experiment_id,
            )
            .where(ResearchExperiment.owner_id == owner_id, ResearchTrial.id == str(trial_id))
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            raise QualityStorageError("not_found")
        return row

    async def _append(
        self,
        owner_id: str,
        kind: str,
        payload: object,
        *,
        revision: int,
        artifact_id: UUID | None = None,
    ) -> tuple[UUID, str]:
        _owner(owner_id)
        _positive(revision)
        if kind not in PUBLIC_ARTIFACT_KINDS | {"quality_blind_map"}:
            raise QualityStorageError("invalid_reference")
        normalized = normalize(payload)
        if not isinstance(normalized, dict):
            raise QualityStorageError("invalid_reference")
        digest = payload_hash(normalized)
        artifact_id = artifact_id or uuid4()
        if not isinstance(artifact_id, UUID):
            raise QualityStorageError("invalid_reference")
        await self._reserve()
        # Check all kinds, not just quality: this path cannot take over a
        # package, original review, private map or another owner's UUID.
        if await self.session.get(MethodArtifact, str(artifact_id)) is not None:
            raise QualityStorageError("invalid_reference")
        self.session.add(
            MethodArtifact(
                id=str(artifact_id),
                owner_id=owner_id,
                kind=kind,
                payload=dict(
                    schema_version=1, revision=revision, payload_hash=digest, payload=normalized
                ),
            )
        )
        await self.session.flush()
        return artifact_id, digest

    async def append_artifact(
        self,
        owner_id: str,
        kind: QualityArtifactKind,
        payload: object,
        *,
        revision: int = 1,
        artifact_id: UUID | None = None,
    ) -> StoredArtifact:
        if kind not in PUBLIC_ARTIFACT_KINDS or isinstance(payload, _BlindMap):
            raise QualityStorageError("invalid_reference")
        key, digest = await self._append(
            owner_id, kind, payload, revision=revision, artifact_id=artifact_id
        )
        ref = QualityArtifactRef(kind=kind, id=key, revision=revision)
        return StoredArtifact(ref, digest, deepcopy(normalize(payload)))  # type: ignore[arg-type]

    async def _append_blind_map(self, owner_id: str, payload: _BlindMap) -> _BlindMapRef:
        # Deliberately separate from all public ref/read/receipt interfaces.
        if not isinstance(payload, _BlindMap):
            raise QualityStorageError("invalid_reference")
        await self._reserve()
        await self.get_experiment(owner_id, payload.experiment_id)
        for assignment in payload.assignments:
            trial = await self.get_trial(owner_id, assignment.trial_id)
            if trial.experiment_id != str(payload.experiment_id) or trial.arm != assignment.arm:
                raise QualityStorageError("invalid_reference")
        key, _ = await self._append(owner_id, "quality_blind_map", payload, revision=1)
        return _BlindMapRef(id=key, revision=1)

    async def read_artifact(
        self, owner_id: str, reference: QualityArtifactRef, *, expected_hash: str | None = None
    ) -> StoredArtifact:
        _owner(owner_id)
        try:
            ref = parse_public_ref(reference)
        except ValueError as exc:
            raise QualityStorageError("invalid_reference") from exc
        if expected_hash is not None:
            _hash(expected_hash)
        row = await self.session.scalar(
            select(MethodArtifact)
            .where(
                MethodArtifact.id == str(ref.id),
                MethodArtifact.owner_id == owner_id,
                MethodArtifact.kind == ref.kind,
            )
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            raise QualityStorageError("not_found")
        envelope = row.payload
        if (
            set(envelope) != {"schema_version", "revision", "payload_hash", "payload"}
            or type(envelope.get("revision")) is not int
            or envelope["revision"] != ref.revision
            or type(envelope.get("schema_version")) is not int
            or envelope["schema_version"] != 1
            or not isinstance(envelope.get("payload"), dict)
        ):
            raise QualityStorageError("invalid_reference")
        digest = payload_hash(envelope["payload"])
        if (
            digest != envelope["payload_hash"]
            or expected_hash is not None
            and digest != expected_hash
        ):
            raise QualityStorageError("artifact_hash_mismatch")
        return StoredArtifact(ref, digest, deepcopy(envelope["payload"]))

    async def create_experiment(
        self, owner_id: str, *, experiment_id: UUID | None = None
    ) -> ResearchExperiment:
        _owner(owner_id)
        await self._reserve()
        row = ResearchExperiment(
            id=str(experiment_id or uuid4()),
            owner_id=owner_id,
            revision=1,
            state="draft",
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def cas_experiment(
        self,
        owner_id: str,
        experiment_id: UUID,
        *,
        expected_revision: int,
        state: ExperimentState,
        spec: QualityArtifactRef | None = None,
        spec_hash: str | None = None,
    ) -> ResearchExperiment:
        _positive(expected_revision)
        await self._reserve()
        current = await self.get_experiment(owner_id, experiment_id)
        if current.revision != expected_revision:
            raise QualityStorageError("revision_conflict")
        if state != current.state and state not in EXPERIMENT_TRANSITIONS.get(current.state, ()):
            raise QualityStorageError("invalid_reference")
        values: dict[str, Any] = dict(revision=expected_revision + 1, state=state)
        if (spec is None) != (spec_hash is None):
            raise QualityStorageError("invalid_reference")
        if spec is not None:
            if spec.kind != "quality_spec" or current.spec_artifact_id is not None:
                raise QualityStorageError("invalid_reference")
            await self.read_artifact(owner_id, spec, expected_hash=spec_hash)
            values.update(spec_artifact_id=str(spec.id), spec_hash=spec_hash)
        if (
            state != "draft"
            and (current.spec_artifact_id is None and spec is None)
            and state not in {"cancelling", "cancelled"}
        ):
            raise QualityStorageError("invalid_reference")
        result = await self.session.execute(
            update(ResearchExperiment)
            .where(
                ResearchExperiment.owner_id == owner_id,
                ResearchExperiment.id == str(experiment_id),
                ResearchExperiment.revision == expected_revision,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise QualityStorageError("revision_conflict")
        return await self.get_experiment(owner_id, experiment_id)

    async def create_trial(
        self,
        owner_id: str,
        experiment_id: UUID,
        *,
        arm: Literal["baseline", "candidate"],
        repeat_index: int,
        trial_id: UUID | None = None,
    ) -> ResearchTrial:
        await self._reserve()
        await self.get_experiment(owner_id, experiment_id)
        if (
            arm not in {"baseline", "candidate"}
            or type(repeat_index) is not int
            or repeat_index not in {1, 2}
        ):
            raise QualityStorageError("invalid_reference")
        row = ResearchTrial(
            id=str(trial_id or uuid4()),
            experiment_id=str(experiment_id),
            arm=arm,
            repeat_index=repeat_index,
            state="planned",
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def bind_task(
        self,
        owner_id: str,
        trial_id: UUID,
        task_id: UUID,
        *,
        envelope: QualityArtifactRef,
        envelope_hash: str,
        method_binding_hash: str,
        model_config_hash: str,
        profile_version: str,
    ) -> ResearchExperimentTask:
        await self._reserve()
        trial = await self.get_trial(owner_id, trial_id)
        task = await self.session.scalar(
            select(AgentTask)
            .where(
                AgentTask.id == str(task_id),
                AgentTask.owner_id == owner_id,
            )
            .execution_options(autoflush=False)
        )
        if task is None:
            raise QualityStorageError("not_found")
        if (
            trial.task_id is not None
            or trial.state != "planned"
            or envelope.kind != "quality_envelope"
        ):
            raise QualityStorageError("invalid_reference")
        await self.read_artifact(owner_id, envelope, expected_hash=envelope_hash)
        _hash(method_binding_hash)
        _hash(model_config_hash)
        if not profile_version.strip() or len(profile_version) > 80:
            raise QualityStorageError("invalid_reference")
        row = ResearchExperimentTask(
            task_id=str(task_id),
            trial_id=str(trial_id),
            envelope_artifact_id=str(envelope.id),
            envelope_hash=envelope_hash,
            method_binding_hash=method_binding_hash,
            model_config_hash=model_config_hash,
            profile_version=profile_version,
        )
        trial.task_id, trial.state = str(task_id), "queued"
        self.session.add(row)
        await self.session.flush()
        return row

    async def finish_trial(
        self,
        owner_id: str,
        trial_id: UUID,
        *,
        state: TrialState,
        result: QualityArtifactRef | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._reserve()
        trial = await self.get_trial(owner_id, trial_id)
        if state not in {"completed", "failed", "cancelled", "blocked"} or trial.state in {
            "completed",
            "failed",
            "cancelled",
            "blocked",
        }:
            raise QualityStorageError("revision_conflict")
        if result is not None:
            if result.kind != "quality_result":
                raise QualityStorageError("invalid_reference")
            await self.read_artifact(owner_id, result)
        if state == "completed" and (trial.task_id is None or result is None):
            raise QualityStorageError("invalid_reference")
        trial.state, trial.error_code = state, error_code
        trial.result_artifact_id = str(result.id) if result is not None else None
        await self.session.flush()

    async def append_assessment(
        self, owner_id: str, trial_id: UUID, reviewer_id: str, payload: AssessmentInput
    ) -> ResearchAssessment:
        _owner(reviewer_id)
        await self._reserve()
        trial = await self.get_trial(owner_id, trial_id)
        if trial.result_artifact_id is None:
            raise QualityStorageError("invalid_reference")
        artifact = await self.session.get(MethodArtifact, trial.result_artifact_id)
        if artifact is None:
            raise QualityStorageError("not_found")
        artifact_revision = artifact.payload.get("revision")
        if type(artifact_revision) is not int:
            raise QualityStorageError("invalid_reference")
        await self.read_artifact(
            owner_id,
            QualityArtifactRef(
                kind="quality_result",
                id=UUID(artifact.id),
                revision=artifact_revision,
            ),
            expected_hash=payload.output_hash,
        )
        latest = (
            await self.session.scalar(
                select(func.max(ResearchAssessment.revision)).where(
                    ResearchAssessment.trial_id == str(trial_id),
                    ResearchAssessment.reviewer_id == reviewer_id,
                )
            )
            or 0
        )
        if latest != payload.expected_assessment_revision:
            raise QualityStorageError("revision_conflict")
        data = normalize(payload)
        row = ResearchAssessment(
            id=str(uuid4()),
            experiment_id=trial.experiment_id,
            trial_id=str(trial_id),
            reviewer_id=reviewer_id,
            revision=latest + 1,
            payload_hash=payload_hash(data),
            payload=data,
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def _validate_receipt(self, owner_id: str, receipt: MutationReceipt) -> None:
        if receipt.experiment_id is not None:
            await self.get_experiment(owner_id, receipt.experiment_id)
        for ref in receipt.result_refs:
            await self.read_artifact(owner_id, ref)

    async def read_assessment(
        self, owner_id: str, assessment_id: UUID, *, revision: int, expected_hash: str
    ) -> ResearchAssessment:
        _positive(revision)
        _hash(expected_hash)
        row = await self.session.scalar(
            select(ResearchAssessment)
            .where(
                ResearchAssessment.id == str(assessment_id),
                ResearchAssessment.revision == revision,
            )
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            raise QualityStorageError("not_found")
        trial = await self.get_trial(owner_id, UUID(row.trial_id))
        if trial.experiment_id != row.experiment_id:
            raise QualityStorageError("invalid_reference")
        if payload_hash(row.payload) != row.payload_hash or row.payload_hash != expected_hash:
            raise QualityStorageError("artifact_hash_mismatch")
        return row

    async def get_task_binding(self, owner_id: str, task_id: UUID) -> ResearchExperimentTask:
        row = await self.session.scalar(
            select(ResearchExperimentTask)
            .join(
                AgentTask,
                AgentTask.id == ResearchExperimentTask.task_id,
            )
            .where(ResearchExperimentTask.task_id == str(task_id), AgentTask.owner_id == owner_id)
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            raise QualityStorageError("not_found")
        trial = await self.get_trial(owner_id, UUID(row.trial_id))
        if trial.task_id != str(task_id):
            raise QualityStorageError("invalid_reference")
        return row

    async def get_receipt(
        self, owner_id: str, mutation_id: UUID, *, authorize: AuthorizeMutation
    ) -> MutationReceipt | None:
        _owner(owner_id)
        await authorize(self.session)
        row = await self.session.scalar(
            select(ResearchExperimentReceipt)
            .where(
                ResearchExperimentReceipt.owner_id == owner_id,
                ResearchExperimentReceipt.mutation_id == str(mutation_id),
            )
            .execution_options(populate_existing=True, autoflush=False)
        )
        if row is None:
            return None  # Not proof that an in-flight mutation was not accepted.
        if row.request_object_id is not None:
            await self.get_experiment(owner_id, UUID(row.request_object_id))
        receipt = MutationReceipt.model_validate(deepcopy(row.payload))
        await self._validate_receipt(owner_id, receipt)
        return receipt

    async def mutate(
        self,
        owner_id: str,
        mutation_id: UUID,
        *,
        operation: MutationOperation,
        request_hash: str,
        request_object_id: UUID | None,
        authorize: AuthorizeMutation,
        apply: ApplyMutation,
    ) -> StoredMutation:
        _owner(owner_id)
        _hash(request_hash)
        if (operation in {"create_case", "create_experiment"}) != (request_object_id is None):
            raise QualityStorageError("invalid_reference")
        if self.session.new or self.session.dirty or self.session.deleted:
            raise QualityStorageError("invalid_reference")
        await self._reserve()
        async with self.session.begin_nested():
            await authorize(self.session)
            if request_object_id is not None:
                await self.get_experiment(owner_id, request_object_id)
            if self.session.new or self.session.dirty or self.session.deleted:
                raise QualityStorageError("invalid_reference")
            prior = await self.session.get(
                ResearchExperimentReceipt, (owner_id, str(mutation_id)), populate_existing=True
            )
            if prior is not None:
                receipt = MutationReceipt.model_validate(deepcopy(prior.payload))
                if (prior.request_hash, prior.request_object_id, receipt.operation) != (
                    request_hash,
                    str(request_object_id) if request_object_id else None,
                    operation,
                ):
                    raise QualityStorageError("idempotency_mismatch")
                await self._validate_receipt(owner_id, receipt)
                return StoredMutation(receipt, True)
            receipt = MutationReceipt.model_validate((await apply(self.session)).model_dump())
            if receipt.mutation_id != mutation_id or receipt.operation != operation:
                raise QualityStorageError("invalid_reference")
            if request_object_id is not None and receipt.experiment_id != request_object_id:
                raise QualityStorageError("invalid_reference")
            await self._validate_receipt(owner_id, receipt)
            self.session.add(
                ResearchExperimentReceipt(
                    owner_id=owner_id,
                    mutation_id=str(mutation_id),
                    request_object_id=str(request_object_id) if request_object_id else None,
                    request_hash=request_hash,
                    payload=receipt.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
            await self.session.flush()
            return StoredMutation(receipt, False)

"""QT-006 real temporary SQLite storage only; fixtures are not research success."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from investment_agent.investor.common import ObjectRef
from investment_agent.research_quality.contracts import (
    AssessmentInput,
    ExperimentCancelled,
    MutationReceipt,
    QualityCaseSaved,
    _BlindMap,
)
from investment_agent.research_quality.refs import QualityArtifactRef
from investment_agent.storage.database import engine_lifespan
from investment_agent.storage.models import Base
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.investor import MethodArtifact
from investment_agent.storage.models.research_quality import (
    ResearchExperimentReceipt,
)
from investment_agent.storage.repositories.research_quality import (
    QualityRepository,
    QualityStorageError,
)

OWNER = "local_owner"


@pytest_asyncio.fixture
async def sessions(tmp_path):
    async with engine_lifespan(
        f"sqlite+aiosqlite:///{tmp_path / 'quality-repository.db'}"
    ) as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _allow(_session):
    pass


async def _case_receipt(session, mutation):
    artifact = await QualityRepository(session).append_artifact(
        OWNER, "quality_case", {"synthetic": True}
    )
    response = QualityCaseSaved(
        case_ref=artifact.ref,
        listing_ref=ObjectRef(kind="listing", id=1, revision=1),
        question="synthetic storage fixture",
        cutoff=datetime.now(UTC),
        material_mode="observed_materials",
        material_refs=(),
    )
    return MutationReceipt(
        mutation_id=mutation,
        operation="create_case",
        experiment_id=None,
        result_refs=(artifact.ref,),
        response_status=201,
        response=response,
        committed_at=datetime.now(UTC),
    )


async def _mutation(session, mutation, apply, **changes):
    values = dict(
        operation="create_case",
        request_hash="a" * 64,
        request_object_id=None,
        authorize=_allow,
        apply=apply,
    )
    values.update(changes)
    return await QualityRepository(session).mutate(OWNER, mutation, **values)


async def _count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


@pytest.mark.asyncio
async def test_artifact_owner_revision_kind_hash_and_no_overwrite(sessions):
    async with sessions() as session, session.begin():
        repo = QualityRepository(session)
        artifact = await repo.append_artifact(OWNER, "quality_case", {"synthetic": True})
        assert (await repo.read_artifact(OWNER, artifact.ref)).payload == {"synthetic": True}
        for owner, ref, digest, code in (
            ("other", artifact.ref, None, "not_found"),
            (OWNER, artifact.ref.model_copy(update={"kind": "quality_report"}), None, "not_found"),
            (OWNER, artifact.ref.model_copy(update={"revision": 2}), None, "invalid_reference"),
            (OWNER, artifact.ref, "b" * 64, "artifact_hash_mismatch"),
        ):
            with pytest.raises(QualityStorageError) as error:
                await repo.read_artifact(owner, ref, expected_hash=digest)
            assert error.value.code == code
        # Even a package UUID or a private-map UUID may not be claimed by append.
        for kind in ("package", "quality_blind_map"):
            key = uuid4()
            session.add(
                MethodArtifact(id=str(key), owner_id="other", kind=kind, payload={"original": True})
            )
            await session.flush()
            with pytest.raises(QualityStorageError):
                await repo.append_artifact(OWNER, "quality_case", {}, artifact_id=key)
        with pytest.raises(QualityStorageError):
            await repo.append_artifact(OWNER, "quality_case", {}, artifact_id=artifact.ref.id)
        with pytest.raises(QualityStorageError):
            await repo.append_artifact(OWNER, "quality_blind_map", {})
        with pytest.raises(QualityStorageError):
            await repo.read_artifact(
                OWNER, {"kind": "quality_blind_map", "id": str(uuid4()), "revision": 1}
            )


@pytest.mark.asyncio
async def test_hash_tampering_fails_closed_without_claiming_pending_sql_guards(sessions):
    async with sessions() as session, session.begin():
        repo = QualityRepository(session)
        # Local corruption injection simulates old files/corrupt storage; QT007
        # owns SQL guards. Insert a corrupt imported row, without bypassing or
        # disabling those guards to overwrite a legitimate historical row.
        key = uuid4()
        session.add(
            MethodArtifact(
                id=str(key),
                owner_id=OWNER,
                kind="quality_result",
                payload=dict(
                    schema_version=1,
                    revision=1,
                    payload_hash="a" * 64,
                    payload={"synthetic": False},
                ),
            )
        )
        await session.flush()
        with pytest.raises(QualityStorageError) as error:
            await repo.read_artifact(
                OWNER, QualityArtifactRef(kind="quality_result", id=key, revision=1)
            )
        assert error.value.code == "artifact_hash_mismatch"


@pytest.mark.asyncio
async def test_foreign_keys_off_owner_and_orphan_checks_and_frozen_spec(sessions):
    async with sessions() as session, session.begin():
        assert await session.scalar(text("PRAGMA foreign_keys")) == 0
        repo = QualityRepository(session)
        experiment = await repo.create_experiment(OWNER)
        key = UUID(experiment.id)
        for owner, parent in ((OWNER, uuid4()), ("other", key)):
            with pytest.raises(QualityStorageError) as error:
                await repo.create_trial(owner, parent, arm="baseline", repeat_index=1)
            assert error.value.code == "not_found"
        foreign = await repo.append_artifact("other", "quality_spec", {})
        with pytest.raises(QualityStorageError):
            await repo.cas_experiment(
                OWNER,
                key,
                expected_revision=1,
                state="sealed",
                spec=foreign.ref,
                spec_hash=foreign.payload_hash,
            )
        spec = await repo.append_artifact(OWNER, "quality_spec", {"synthetic": True})
        sealed = await repo.cas_experiment(
            OWNER,
            key,
            expected_revision=1,
            state="sealed",
            spec=spec.ref,
            spec_hash=spec.payload_hash,
        )
        assert sealed.revision == 2
        with pytest.raises(QualityStorageError) as error:
            await repo.cas_experiment(OWNER, key, expected_revision=1, state="running")
        assert error.value.code == "revision_conflict"
        with pytest.raises(QualityStorageError):
            await repo.cas_experiment(
                OWNER,
                key,
                expected_revision=2,
                state="running",
                spec=spec.ref,
                spec_hash=spec.payload_hash,
            )
        assert (await repo.get_experiment(OWNER, key)).spec_artifact_id == str(spec.ref.id)


async def _task_index(session, owner=OWNER):
    # A referential-integrity fixture only, not an admitted/successful Task.
    key = uuid4()
    session.add(
        AgentTask(
            id=str(key),
            owner_id=owner,
            input_key=str(key),
            input_hash="a" * 64,
            revision=1,
            status="queued",
            run_id=1,
            payload={},
            checkpoint={},
            resolved=[],
            consumed_events=[],
        )
    )
    await session.flush()
    return key


async def _bound(session):
    repo = QualityRepository(session)
    experiment = await repo.create_experiment(OWNER)
    trial = await repo.create_trial(OWNER, UUID(experiment.id), arm="baseline", repeat_index=1)
    envelope = await repo.append_artifact(OWNER, "quality_envelope", {"synthetic": True})
    task = await _task_index(session)
    await repo.bind_task(
        OWNER,
        UUID(trial.id),
        task,
        envelope=envelope.ref,
        envelope_hash=envelope.payload_hash,
        method_binding_hash="a" * 64,
        model_config_hash="b" * 64,
        profile_version="synthetic.v1",
    )
    return repo, trial, task, envelope


@pytest.mark.asyncio
async def test_binding_requires_owned_existing_task_and_envelope(sessions):
    async with sessions() as session, session.begin():
        repo, trial, task, envelope = await _bound(session)
        binding = await repo.get_task_binding(OWNER, task)
        assert binding.trial_id == trial.id and binding.envelope_hash == envelope.payload_hash
        with pytest.raises(QualityStorageError):
            await repo.get_task_binding("other", task)
        second = await repo.create_trial(
            OWNER, UUID(trial.experiment_id), arm="candidate", repeat_index=1
        )
        for task_id in (uuid4(), await _task_index(session, "other")):
            with pytest.raises(QualityStorageError) as error:
                await repo.bind_task(
                    OWNER,
                    UUID(second.id),
                    task_id,
                    envelope=envelope.ref,
                    envelope_hash=envelope.payload_hash,
                    method_binding_hash="a" * 64,
                    model_config_hash="b" * 64,
                    profile_version="synthetic.v1",
                )
            assert error.value.code == "not_found"
        with pytest.raises(QualityStorageError):
            await repo.bind_task(
                OWNER,
                UUID(trial.id),
                task,
                envelope=envelope.ref,
                envelope_hash=envelope.payload_hash,
                method_binding_hash="a" * 64,
                model_config_hash="b" * 64,
                profile_version="synthetic.v1",
            )


def _score(digest, expected=0):
    return AssessmentInput(
        mutation_id=uuid4(),
        expected_assessment_revision=expected,
        output_hash=digest,
        scores={
            name: {"score": 1, "reason": "synthetic storage test only"}
            for name in (
                "fact_relationship",
                "research_depth",
                "counter_evidence",
                "independent_revision",
                "clarity_next_steps",
            )
        },
    )


@pytest.mark.asyncio
async def test_assessments_append_exact_output_and_reports_retain_history(sessions):
    async with sessions() as session, session.begin():
        repo, trial, _, _ = await _bound(session)
        output = await repo.append_artifact(OWNER, "quality_result", {"synthetic": "storage-only"})
        await repo.finish_trial(OWNER, UUID(trial.id), state="completed", result=output.ref)
        with pytest.raises(QualityStorageError) as error:
            await repo.append_assessment(OWNER, UUID(trial.id), "reviewer", _score("f" * 64))
        assert error.value.code == "artifact_hash_mismatch"
        first = await repo.append_assessment(
            OWNER, UUID(trial.id), "reviewer", _score(output.payload_hash)
        )
        with pytest.raises(QualityStorageError) as error:
            await repo.append_assessment(
                OWNER, UUID(trial.id), "reviewer", _score(output.payload_hash)
            )
        assert error.value.code == "revision_conflict"
        second = await repo.append_assessment(
            OWNER, UUID(trial.id), "reviewer", _score(output.payload_hash, 1)
        )
        assert (first.revision, second.revision) == (1, 2)
        assert (
            await repo.read_assessment(
                OWNER, UUID(first.id), revision=1, expected_hash=first.payload_hash
            )
        ).id == first.id
        with pytest.raises(QualityStorageError):
            await repo.read_assessment(
                "other", UUID(first.id), revision=1, expected_hash=first.payload_hash
            )
        old = await repo.append_artifact(OWNER, "quality_report", {"assessment": first.id})
        new = await repo.append_artifact(
            OWNER, "quality_report", {"assessment": second.id}, revision=2
        )
        assert old.ref.id != new.ref.id
        assert (await repo.read_artifact(OWNER, old.ref)).payload == {"assessment": first.id}


@pytest.mark.asyncio
async def test_mutation_replay_original_response_refs_and_hash_conflict(sessions):
    mutation = uuid4()
    calls = 0

    async def apply(session):
        nonlocal calls
        calls += 1
        return await _case_receipt(session, mutation)

    async with sessions() as session, session.begin():
        first = await _mutation(session, mutation, apply)
    async with sessions() as session, session.begin():
        replay = await _mutation(session, mutation, apply)
        assert replay.replayed and replay.receipt == first.receipt
        assert (
            await QualityRepository(session).get_receipt(OWNER, mutation, authorize=_allow)
            == first.receipt
        )
        with pytest.raises(QualityStorageError) as error:
            await _mutation(session, mutation, apply, request_hash="b" * 64)
        assert error.value.code == "idempotency_mismatch"
        assert (
            await _count(session, MethodArtifact)
            == await _count(session, ResearchExperimentReceipt)
            == 1
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_current_authorization_precedes_even_committed_replay(sessions):
    mutation = uuid4()

    async def apply(session):
        return await _case_receipt(session, mutation)

    async with sessions() as session, session.begin():
        await _mutation(session, mutation, apply)

    async def denied(_session):
        raise PermissionError("current owner access revoked")

    async with sessions() as session, session.begin():
        with pytest.raises(PermissionError):
            await _mutation(session, mutation, apply, authorize=denied)
        with pytest.raises(PermissionError):
            await QualityRepository(session).get_receipt(OWNER, mutation, authorize=denied)
        assert (
            await QualityRepository(session).get_receipt("other", mutation, authorize=_allow)
            is None
        )


@pytest.mark.asyncio
async def test_request_object_authorization_precedes_receipt_lookup(sessions):
    mutation = uuid4()
    async with sessions() as session, session.begin():
        repo = QualityRepository(session)
        exp = await repo.create_experiment(OWNER)
        key = UUID(exp.id)

        async def apply(_session):
            return MutationReceipt(
                mutation_id=mutation,
                operation="cancel",
                experiment_id=key,
                result_refs=(),
                response_status=200,
                response=ExperimentCancelled(experiment_id=key, revision=1, state="cancelled"),
                committed_at=datetime.now(UTC),
            )

        await _mutation(session, mutation, apply, operation="cancel", request_object_id=key)
    async with sessions() as session, session.begin():
        # Existing receipt must not turn another/new inaccessible request object
        # into an authorized request, even if its hash was reused.
        with pytest.raises(QualityStorageError) as error:
            await _mutation(session, mutation, apply, operation="cancel", request_object_id=uuid4())
        assert error.value.code == "not_found"
        with pytest.raises(QualityStorageError):
            await _mutation(session, mutation, apply, operation="cancel")


@pytest.mark.asyncio
async def test_failed_savepoint_leaves_no_artifact_or_receipt(sessions):
    mutation = uuid4()

    async def failed(session):
        await _case_receipt(session, mutation)
        raise RuntimeError("injected after artifact")

    async with sessions() as session, session.begin():
        with pytest.raises(RuntimeError):
            await _mutation(session, mutation, failed)
        assert (
            await _count(session, MethodArtifact)
            == await _count(session, ResearchExperimentReceipt)
            == 0
        )


@pytest.mark.asyncio
async def test_cas_artifact_and_receipt_share_rollback_boundary(sessions):
    async with sessions() as session, session.begin():
        key = UUID((await QualityRepository(session).create_experiment(OWNER)).id)
    mutation = uuid4()

    async def failed(session):
        repo = QualityRepository(session)
        spec = await repo.append_artifact(OWNER, "quality_spec", {"synthetic": True})
        await repo.cas_experiment(
            OWNER,
            key,
            expected_revision=1,
            state="sealed",
            spec=spec.ref,
            spec_hash=spec.payload_hash,
        )
        raise RuntimeError("failure after artifact and CAS")

    async with sessions() as session, session.begin():
        with pytest.raises(RuntimeError):
            await _mutation(session, mutation, failed, operation="seal", request_object_id=key)
        experiment = await QualityRepository(session).get_experiment(OWNER, key)
        assert (experiment.revision, experiment.state, experiment.spec_artifact_id) == (
            1,
            "draft",
            None,
        )
        assert (
            await _count(session, MethodArtifact)
            == await _count(session, ResearchExperimentReceipt)
            == 0
        )


@pytest.mark.asyncio
async def test_outer_rollback_leaves_no_artifact_or_receipt(sessions):
    mutation = uuid4()

    async def apply(session):
        return await _case_receipt(session, mutation)

    async with sessions() as session:
        await _mutation(session, mutation, apply)
        await session.rollback()
    async with sessions() as session:
        assert (
            await _count(session, MethodArtifact)
            == await _count(session, ResearchExperimentReceipt)
            == 0
        )


@pytest.mark.asyncio
async def test_concurrent_mutation_serializes_and_cas_has_one_winner(sessions):
    mutation = uuid4()
    calls = 0

    async def apply(session):
        nonlocal calls
        calls += 1
        return await _case_receipt(session, mutation)

    async def writer():
        async with sessions() as session, session.begin():
            return await _mutation(session, mutation, apply)

    results = await asyncio.gather(writer(), writer())
    assert calls == 1 and sorted(item.replayed for item in results) == [False, True]
    assert results[0].receipt == results[1].receipt
    async with sessions() as session, session.begin():
        key = UUID((await QualityRepository(session).create_experiment(OWNER)).id)

    async def cas():
        async with sessions() as session, session.begin():
            try:
                await QualityRepository(session).cas_experiment(
                    OWNER, key, expected_revision=1, state="draft"
                )
                return "won"
            except QualityStorageError as error:
                return error.code

    assert sorted(await asyncio.gather(cas(), cas())) == ["revision_conflict", "won"]


@pytest.mark.asyncio
async def test_private_map_checks_experiment_trial_owner_and_arm(sessions):
    async with sessions() as session, session.begin():
        repo = QualityRepository(session)
        exp = await repo.create_experiment(OWNER)
        trials = [
            await repo.create_trial(OWNER, UUID(exp.id), arm=arm, repeat_index=1)
            for arm in ("baseline", "candidate")
        ]
        data = dict(
            experiment_id=UUID(exp.id),
            mapping_hash="a" * 64,
            assignments=[
                dict(trial_id=UUID(trial.id), label=label, arm=trial.arm)
                for trial, label in zip(trials, ("A", "B"), strict=True)
            ],
        )
        private = await repo._append_blind_map(OWNER, _BlindMap.model_validate(data))
        with pytest.raises(QualityStorageError):
            await repo.read_artifact(OWNER, private)
        with pytest.raises(QualityStorageError):
            await repo._append_blind_map("other", _BlindMap.model_validate(data))
        data["assignments"][0]["arm"] = "candidate"
        with pytest.raises(QualityStorageError):
            await repo._append_blind_map(OWNER, _BlindMap.model_validate(data))

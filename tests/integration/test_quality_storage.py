"""QT-008 cross-module caller transaction; no experiment admission claim.

Unit repository tests already cover mutation races/CAS, cross-owner references,
hash corruption and nested rollback. Here ordinary original admissions supply
real Task/grant/budget/method rows, checking the shared outer transaction with
the new quality tables. Trusted experiment admission is a later task.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from investment_agent.agent.budget import BudgetService
from investment_agent.agent.conversations import ConversationCreate, ConversationService
from investment_agent.agent.tasks import TaskService
from investment_agent.research_quality.contracts import (
    ExperimentCreated,
    MutationReceipt,
)
from investment_agent.storage.database import engine_lifespan
from investment_agent.storage.models import Base
from investment_agent.storage.models.agent import AgentTask, BudgetEntry, ExecutionGrantRow
from investment_agent.storage.models.investor import MethodArtifact, TaskMethodBinding
from investment_agent.storage.models.research_quality import (
    ResearchExperiment,
    ResearchExperimentReceipt,
    ResearchExperimentTask,
    ResearchTrial,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from investment_agent.storage.repositories.research_quality import QualityRepository
from tests.support.conversations import base, message

OWNER = "local_owner"
MODELS = (
    AgentTask,
    ExecutionGrantRow,
    BudgetEntry,
    TaskMethodBinding,
    ResearchExperiment,
    ResearchTrial,
    ResearchExperimentTask,
    ResearchExperimentReceipt,
    MethodArtifact,
)


async def _counts(session):
    return {
        model: await session.scalar(select(func.count()).select_from(model)) for model in MODELS
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [False, True])
async def test_native_admission_and_quality_storage_share_outer_transaction(tmp_path, commit):
    async with engine_lifespan(
        f"sqlite+aiosqlite:///{tmp_path / 'quality-cross-module.db'}"
    ) as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        mutation = uuid4()
        admitted = []

        async def authorize(_session):
            # Current owner for this isolated fixture. Production callbacks
            # must perform real current authorization, including revocation.
            return None

        async def apply(session):
            repo = QualityRepository(session)
            experiment = await repo.create_experiment(OWNER)
            experiment_id = UUID(experiment.id)
            envelope = await repo.append_artifact(
                OWNER, "quality_envelope", {"synthetic": "storage-atomicity-only"}
            )
            for arm in ("baseline", "candidate"):
                conversation_service = ConversationService(session, base())
                conversation = await conversation_service.create(ConversationCreate())
                # This is original ordinary explanation admission, never a fake
                # experimental context or a hand-written successful Task row.
                accepted = await conversation_service.submit(
                    conversation.id, message(research_kind="explanation")
                )
                admitted.append(accepted.task.id)
                task_row = await session.get(AgentTask, str(accepted.task.id))
                run = await PipelineRunRepository(session).claim_next(
                    worker_id="storage-fixture", run_id=task_row.run_id
                )
                claim = await TaskService(session).claim(
                    accepted.task.id, RunLease(run.id, "storage-fixture", run.attempt_count)
                )
                assert claim is not None
                # Reserve via the real ledger boundary, without marking sent
                # or constructing any provider/model call.
                await BudgetService(session, base()).reserve(claim, estimated_tokens=100)
                trial = await repo.create_trial(OWNER, experiment_id, arm=arm, repeat_index=1)
                await repo.bind_task(
                    OWNER,
                    UUID(trial.id),
                    accepted.task.id,
                    envelope=envelope.ref,
                    envelope_hash=envelope.payload_hash,
                    method_binding_hash="a" * 64,
                    model_config_hash="b" * 64,
                    profile_version="storage-fixture-not-experiment-admission",
                )
                # Storage stores validated hashes; resolving/verifying actual
                # method/config identity is the later trusted-admit boundary.
            response = ExperimentCreated(
                experiment_id=experiment_id,
                revision=1,
                preflight={"status": "pending"},
                limits={"max_requests": 2, "max_tokens": 400000},
                trial_plan={"repeat_count": 1, "planned_trials": 2},
                links={"self": f"/investor/methods/quality-experiments/{experiment_id}"},
            )
            return MutationReceipt(
                mutation_id=mutation,
                operation="create_experiment",
                experiment_id=experiment_id,
                result_refs=(),
                response_status=201,
                response=response,
                committed_at=datetime.now(UTC),
            )

        async with sessions() as session:
            assert all(value == 0 for value in (await _counts(session)).values())
            stored = await QualityRepository(session).mutate(
                OWNER,
                mutation,
                operation="create_experiment",
                request_hash="c" * 64,
                request_object_id=None,
                authorize=authorize,
                apply=apply,
            )
            before = await _counts(session)
            assert before[AgentTask] == before[ExecutionGrantRow] == before[TaskMethodBinding] == 2
            assert before[BudgetEntry] >= 2
            assert before[ResearchTrial] == before[ResearchExperimentTask] == 2
            assert (
                before[ResearchExperiment]
                == before[ResearchExperimentReceipt]
                == before[MethodArtifact]
                == 1
            )
            if commit:
                await session.commit()
            else:
                # In particular, releasing the repository's SAVEPOINT did not
                # commit native admissions or any of the quality records.
                await session.rollback()
        async with sessions() as session:
            after = await _counts(session)
            repo = QualityRepository(session)
            if commit:
                assert after == before
                assert (
                    await repo.get_receipt(OWNER, mutation, authorize=authorize) == stored.receipt
                )
                for task_id in admitted:
                    assert (await TaskService(session).get_task(task_id)).status.value == "running"
                    assert (await repo.get_task_binding(OWNER, task_id)).envelope_hash
            else:
                assert all(value == 0 for value in after.values())
                assert await repo.get_receipt(OWNER, mutation, authorize=authorize) is None

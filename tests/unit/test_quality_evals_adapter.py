"""QT-004: Evals reads saved native results; it never owns a research Task."""

from collections import Counter
from dataclasses import dataclass
from importlib.metadata import version

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EqualsExpected
from sqlalchemy import func, select

from investment_agent.agent.research import ResearchOutput, research_artifacts
from investment_agent.agent.tasks import TaskService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask, BudgetEntry
from tests.support.conversations import conversation_db, new_conversation, submit
from tests.support.research import progress, response, run

__all__ = ["conversation_db"]


@dataclass(frozen=True)
class SavedOutcome:
    status: str
    stop_reason: str | None
    artifact_count: int


async def _counts():
    async with session_scope() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (AgentTask, BudgetEntry)
            ]
        )


@pytest.mark.asyncio
async def test_fixed_evals_projects_saved_success_and_failure_without_task_reexecution(
    conversation_db,
):
    assert version("pydantic-ai") == version("pydantic-evals") == "2.31.1"
    model_calls = 0

    def successful(_messages, _info):
        nonlocal model_calls
        model_calls += 1
        return response(ResearchOutput(progress=progress()))

    def invalid(_messages, _info):
        nonlocal model_calls
        model_calls += 1
        return ModelResponse(
            [TextPart("invalid saved-output fixture")],
            usage=RequestUsage(input_tokens=20, output_tokens=10),
        )

    ids = []
    for fixture in (successful, invalid):
        receipt = await submit(await new_conversation())
        await run(receipt, FunctionModel(fixture))
        ids.append(receipt.task.id)

    # Read committed original storage once. Evals receives no session, executor,
    # admission callback, provider or paid-operation callable.
    saved = {}
    async with session_scope() as session:
        for task_id in ids:
            task = await TaskService(session).get_task(task_id)
            artifacts = await research_artifacts(session, task_id)
            saved[str(task_id)] = SavedOutcome(
                task.status.value,
                task.stop_reason.value if task.stop_reason else None,
                len(artifacts),
            )
    assert [item.status for item in saved.values()] == ["succeeded", "failed"]
    assert next(iter(saved.values())).artifact_count > 0
    before = await _counts()
    calls_before = model_calls
    reads = Counter()

    async def read_saved(task_id):
        reads[task_id] += 1
        return saved[task_id]

    dataset = Dataset(
        name="qt004-saved-native-results",
        cases=[Case(name=key, inputs=key, expected_output=value) for key, value in saved.items()],
        evaluators=[EqualsExpected()],
    )
    report = await dataset.evaluate(
        read_saved,
        max_concurrency=1,
        progress=False,
        repeat=1,
        retry_task=None,
        retry_evaluators=None,
    )
    assert len(report.cases) == 2 and not report.failures
    assert all(assertion.value for case in report.cases for assertion in case.assertions.values())
    assert {case.name: case.output for case in report.cases} == saved
    assert reads == Counter({key: 1 for key in saved})
    assert await _counts() == before
    assert model_calls == calls_before

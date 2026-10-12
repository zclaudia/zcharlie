"""Agent execution fixtures and constructors (moved from tests/test_agent_execution.py)."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest_asyncio
from pydantic import BaseModel, ConfigDict

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionGrant, admit
from investment_agent.agent.executor import AgentExecutor
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.model import AgentModelConfig
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease


@pytest_asyncio.fixture
async def execution_db(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'execution.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


def settings(**overrides):
    values = dict(api_key="fixture-app", agent_api_key="fixture-secret",
        agent_base_url="https://fixture.invalid/v1", agent_model="fixture-model", agent_max_tokens=100,
        investor_agent_enabled=True, investor_agent_task_tokens=100000,
        investor_agent_daily_tokens=500000, investor_agent_task_seconds=600)
    values.update(overrides)
    return Settings(_env_file=None, **values)


def grant(**overrides):
    values = dict(mode="interactive", source="authenticated_message", prompt="Answer fixture question")
    values.update(overrides)
    return ExecutionGrant(**values)


async def admitted(base, permission=None, *, claim=True, kind="agent_turn"):
    async with session_scope() as session:
        task = await admit(session, base=base, input_key=str(uuid4()),
            binding=ContextBinding(context_refs=(), base_refs=(), as_of=datetime.now(UTC)),
            grant=permission or grant(), kind=kind)
        if not claim:
            return task
        run = await PipelineRunRepository(session).claim_next(worker_id="fixture", kinds=[kind])
        trusted = await TaskService(session).claim(task.id, RunLease(run.id, "fixture", run.attempt_count))
    return task, trusted


async def run_worker(base, model=None, *, tools=(), transport=None):
    executor = AgentExecutor(base, registrations=tools, model=model, transport=transport)
    worker = ResearchWorker(settings=base, worker_id="fixture", handlers=[AgentJobHandler("agent_turn", executor)],
                            heartbeat_interval_seconds=0.03)
    assert await worker.run_once() is not None


def priced(**overrides):
    base = settings(investor_agent_input_usd_per_million=Decimal(1),
                    investor_agent_output_usd_per_million=Decimal(2),
                    investor_agent_price_version="verified-fixture-v1",
                    investor_agent_price_model="fixture-model", **overrides)
    return base.model_copy(update={"investor_agent_price_config_version": AgentModelConfig.from_settings(base, 0).version})


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


async def no_scope(session, business):
    return ()


def http_result(*, tools=False, usage=True):
    message = {"role": "assistant", "content": '{"summary":"fixture"}'}
    if tools:
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "provider-call", "type": "function",
            "function": {"name": "write", "arguments": '{"business":{"value":7}}'}}]}
    body = {"id": "fixture", "object": "chat.completion", "created": 1, "model": "fixture-model",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tools else "stop"}]}
    if usage:
        body["usage"] = {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}
    return httpx.Response(200, json=body)

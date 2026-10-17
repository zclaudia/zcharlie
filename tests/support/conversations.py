"""Conversation fixtures and helpers (moved from tests/test_agent_conversations.py)."""

from uuid import uuid4

import pytest_asyncio

from investment_agent.agent.contracts import MessageInput
from investment_agent.agent.conversations import ConversationCreate, ConversationService
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.base import Base
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease


@pytest_asyncio.fixture
async def conversation_db(tmp_path):
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'conversation.db'}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield


def base(**overrides):
    # P3 widened the model-visible research/publication contract schemas, which
    # grows every serialized request by ~2 KB. The old 100000-token task budget
    # put the full-research fixture within ~1 KB of the finish_only reserve
    # flip (remaining_tokens < 2 * largest request estimate), hiding tools on
    # the final turn; 200000 restores the planning headroom the fixture was
    # written against. Tight-budget tests pass their own explicit overrides.
    values = dict(api_key="fixture-app", investor_agent_enabled=True,
        agent_api_key="fixture-model-key", agent_base_url="https://fixture.invalid/v1",
        agent_model="fixture-model", agent_max_tokens=100, investor_agent_task_tokens=200000,
        investor_agent_daily_tokens=500000, investor_agent_task_seconds=600)
    values.update(overrides)
    return Settings(_env_file=None, **values)


def message(**overrides):
    values = dict(client_message_id=uuid4(), text="Explain this", context_refs=())
    values.update(overrides)
    return MessageInput(**values)


async def new_conversation():
    async with session_scope() as session:
        return await ConversationService(session, base()).create(ConversationCreate())


async def submit(conversation, body=None, settings=None):
    async with session_scope() as session:
        return await ConversationService(session, settings or base()).submit(conversation.id, body or message())


async def claim(task):
    async with session_scope() as session:
        row = await session.get(AgentTask, str(task.id))
        run = await PipelineRunRepository(session).claim_next(worker_id="fixture", run_id=row.run_id)
        return await TaskService(session).claim(task.id, RunLease(run.id, "fixture", run.attempt_count))

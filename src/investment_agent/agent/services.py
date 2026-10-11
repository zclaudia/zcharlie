"""Authenticated application interfaces; no executor or workflow framework."""

from typing import Protocol
from uuid import UUID

from investment_agent.agent.contracts import MessageAccepted, MessageInput, Task, TaskEvent
from investment_agent.investor.common import OwnerId


class AgentService(Protocol):
    async def submit_message(
        self, user_id: OwnerId, conversation_id: UUID, message: MessageInput
    ) -> MessageAccepted: ...
    async def get_task(self, user_id: OwnerId, task_id: UUID) -> Task: ...
    async def cancel_task(self, user_id: OwnerId, task_id: UUID, idempotency_key: UUID) -> Task: ...
    async def get_events(
        self, user_id: OwnerId, task_id: UUID, after_event_id: int
    ) -> tuple[TaskEvent, ...]: ...

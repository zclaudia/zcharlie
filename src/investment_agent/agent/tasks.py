"""Durable execution boundaries, independent of model and HTTP adapters.

All writes use the caller's session and transaction; never commit here. Prepare
an action in a separate committed boundary before invoking a tool. SQLite writes
and their receipts settle atomically. External effects require provider-side
idempotency/reconciliation, and cannot be promised exactly once by this ledger.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.contracts import ContextBinding, Task, TaskEvent, Usage, WaitRequirement
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.enums import StopReason, TaskStatus
from investment_agent.storage.models.agent import (
    AgentTask,
    PublicTaskEvent,
    TaskAttempt,
    ToolAction,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    PipelineRunRepository,
    RunLease,
)

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
EVENT: TypeAdapter[TaskEvent] = TypeAdapter(TaskEvent)


@dataclass(frozen=True)
class ActionResult:
    """Private tool output and explicitly public artifact references."""

    output: dict[str, Any]
    artifact_refs: tuple[ObjectRef, ...] = ()


WriteAction = Callable[[AsyncSession, UUID], Awaitable[ActionResult | dict[str, Any]]]


def fingerprint(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class TaskClaim:
    task_id: UUID
    attempt_id: UUID
    lease: RunLease


class EventCursorInvalidError(ValueError):
    pass


class EventCursorExpiredError(RuntimeError):
    def __init__(self, task_id: UUID) -> None:
        self.task_id = task_id
        super().__init__("cursor_expired")


class TaskBoundaryError(RuntimeError):
    """A cancelled, terminal or obsolete attempt cannot write."""


class ReconciliationRequiredError(RuntimeError):
    """An external result is unknown; query the provider using operation_id."""


class TaskService:
    def __init__(
        self, session: AsyncSession, owner: Literal["local_owner"] = "local_owner"
    ) -> None:
        self.session = session
        self.owner = owner
        self.runs = PipelineRunRepository(session)

    async def _row(self, task_id: UUID) -> AgentTask:
        row = await self.session.scalar(
            select(AgentTask)
            .where(AgentTask.id == str(task_id), AgentTask.owner_id == self.owner)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise LookupError("task not found")
        return row

    async def get_task(self, task_id: UUID) -> Task:
        return Task.model_validate((await self._row(task_id)).payload)

    async def create(
        self,
        *,
        input_key: str,
        binding: ContextBinding,
        kind: str = "agent_turn",
        parent_task_id: UUID | None = None,
        message_ref: ObjectRef | None = None,
        queue_class: str | None = None,
    ) -> Task:
        if kind not in {"agent_turn", "agent_research", "agent_review", "agent_discovery"}:
            raise ValueError("unsupported agent kind")
        if not input_key.strip() or len(input_key) > 128:
            raise ValueError("input_key must contain 1 to 128 characters")
        digest = fingerprint(
            {
                "binding": binding.model_dump(mode="json"),
                **({"message_ref": message_ref.model_dump(mode="json")} if message_ref else {}),
                "kind": kind,
                "parent": str(parent_task_id) if parent_task_id else None,
            }
        )
        existing = await self.session.scalar(
            select(AgentTask).where(
                AgentTask.owner_id == self.owner, AgentTask.input_key == input_key
            )
        )
        if existing:
            if existing.input_hash != digest:
                raise IdempotencyPayloadMismatchError("task input key reused")
            return Task.model_validate(existing.payload)
        from investment_agent.agent.operations import admission
        class_name = queue_class or {"agent_discovery": "exploration", "agent_review": "review"}.get(kind, "interactive")
        if not await admission(self.session, class_name):
            raise TaskBoundaryError("queue_capacity")
        async with self.session.begin_nested():
            task_id = uuid4()
            task = Task(
                id=task_id,
                revision=1,
                created_at=datetime.now(UTC),
                actor=self.owner,
                origin_ref=None,
                parent_task_id=parent_task_id,
                message_ref=message_ref,
                binding=binding,
                status=TaskStatus.QUEUED,
                stop_reason=None,
                waiting=None,
                artifact_refs=(),
                usage=Usage(
                    cost=Quantity(value=None, unit="currency", unknown_reason="not settled")
                ),
            )
            run, _ = await self.runs.enqueue(
                kind=kind,
                idempotency_key=f"agent:{task_id}:1",
                triggered_by="agent",
                request_json={"task_id": str(task_id), "queue_class": class_name},
            )
            row = AgentTask(
                id=str(task_id),
                owner_id=self.owner,
                input_key=input_key,
                input_hash=digest,
                revision=1,
                status="queued",
                run_id=run.id,
                payload=task.model_dump(mode="json"),
                checkpoint={},
                resolved=[],
                consumed_events=[],
                wait_token=None,
            )
            self.session.add(row)
            await self.session.flush()
            await self._state_event(row, None)
        return task

    async def _state_event(self, row: AgentTask, attempt_id: UUID | None) -> None:
        await self.append_event(
            UUID(row.id),
            attempt_id,
            "task_state",
            {"status": row.status, "cancel_requested": row.payload["cancel_requested"]},
        )

    async def append_event(
        self, task_id: UUID, attempt_id: UUID | None, event_type: str, payload: dict[str, Any]
    ) -> TaskEvent:
        # Trusted application summaries only: never pass provider events or reasoning here.
        await self._row(task_id)
        if attempt_id is not None:
            attempt = await self.session.get(TaskAttempt, str(attempt_id))
            if attempt is None or attempt.task_id != str(task_id):
                raise ValueError("event attempt does not belong to task")
        data = {
            "event_id": 1,
            "task_id": str(task_id),
            "attempt_id": str(attempt_id) if attempt_id else None,
            "occurred_at": datetime.now(UTC).isoformat(),
            "type": event_type,
            "payload": payload,
        }
        validated = EVENT.validate_python(data).model_dump(mode="json")
        event = PublicTaskEvent(task_id=str(task_id), payload=validated, occurred_at=datetime.now(UTC))
        self.session.add(event)
        await self.session.flush()
        validated = {**validated, "event_id": event.event_id}
        event.payload = validated
        return EVENT.validate_python(validated)

    async def get_events(self, task_id: UUID, after_event_id: int = 0) -> tuple[TaskEvent, ...]:
        row = await self._row(task_id)
        if not 0 <= after_event_id <= 2**63 - 1:
            raise EventCursorInvalidError("invalid_cursor")
        if after_event_id > 0 and after_event_id <= row.event_floor:
            raise EventCursorExpiredError(task_id)
        maximum = await self.session.scalar(select(func.max(PublicTaskEvent.event_id))
            .where(PublicTaskEvent.task_id == str(task_id))) or 0
        if after_event_id > max(maximum, row.event_floor):
            raise EventCursorInvalidError("cursor_ahead")
        rows = (
            await self.session.scalars(
                select(PublicTaskEvent)
                .where(
                    PublicTaskEvent.task_id == str(task_id),
                    PublicTaskEvent.event_id > after_event_id,
                )
                .order_by(PublicTaskEvent.event_id).limit(200)
            )
        ).all()
        return tuple(EVENT.validate_python(r.payload) for r in rows)

    async def retain_events(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """Delete a bounded 30-day batch; durable floors survive an empty table."""
        if not 1 <= limit <= 1000:
            raise ValueError("invalid retention limit")
        rows = list(await self.session.scalars(select(PublicTaskEvent)
            .where(PublicTaskEvent.occurred_at < (now or datetime.now(UTC)) - timedelta(days=30))
            .order_by(PublicTaskEvent.event_id).limit(limit)))
        for event in rows:
            await self.session.execute(update(AgentTask)
                .where(AgentTask.id == event.task_id, AgentTask.event_floor < event.event_id)
                .values(event_floor=event.event_id))
        if rows:
            await self.session.execute(delete(PublicTaskEvent)
                .where(PublicTaskEvent.event_id.in_([event.event_id for event in rows])))
        return len(rows)

    async def _save(self, row: AgentTask, task: Task) -> None:
        old = row.revision
        data = task.model_copy(update={"revision": old + 1}).model_dump(mode="json")
        result = await self.session.execute(
            update(AgentTask)
            .where(AgentTask.id == row.id, AgentTask.revision == old)
            .values(revision=old + 1, status=data["status"], payload=data)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise TaskBoundaryError("task changed concurrently")
        await self.session.refresh(row)

    async def claim(self, task_id: UUID, lease: RunLease) -> TaskClaim | None:
        row = await self._row(task_id)
        from investment_agent.storage.models.analysis import PipelineRun

        run = await self.session.get(PipelineRun, lease.run_id)
        if run is None or run.request_json.get("task_id") != str(task_id):
            raise TaskBoundaryError("run does not belong to task")
        if row.status in TERMINAL or row.run_id != lease.run_id:
            # Normal cancellation race: close the claimed queue slot, never run tools.
            await self.runs.mark_success(lease.run_id, lease=lease)
            return None
        if row.status not in {"queued", "running"}:
            raise TaskBoundaryError("task is waiting")
        await self.runs.heartbeat(lease.run_id, lease=lease)
        existing = await self.session.scalar(
            select(TaskAttempt).where(
                TaskAttempt.run_id == lease.run_id, TaskAttempt.lease_attempt == lease.attempt
            )
        )
        if existing is None:
            await self.session.execute(
                update(TaskAttempt)
                .where(
                    TaskAttempt.run_id == lease.run_id,
                    TaskAttempt.lease_attempt < lease.attempt,
                    TaskAttempt.status == "running",
                )
                .values(status="abandoned")
            )
            existing = TaskAttempt(
                id=str(uuid4()),
                task_id=row.id,
                run_id=lease.run_id,
                lease_attempt=lease.attempt,
                worker_id=lease.worker_id,
                status="running",
            )
            self.session.add(existing)
            await self.session.flush()
        await self._save(
            row, Task.model_validate(row.payload).model_copy(update={"status": TaskStatus.RUNNING})
        )
        await self._state_event(row, UUID(existing.id))
        return TaskClaim(task_id, UUID(existing.id), lease)

    async def fence(self, claim: TaskClaim) -> AgentTask:
        await self.runs.heartbeat(claim.lease.run_id, lease=claim.lease)
        attempt = await self.session.get(TaskAttempt, str(claim.attempt_id))
        if (
            attempt is None
            or attempt.task_id != str(claim.task_id)
            or attempt.run_id != claim.lease.run_id
            or attempt.lease_attempt != claim.lease.attempt
            or attempt.worker_id != claim.lease.worker_id
            or attempt.status != "running"
        ):
            raise TaskBoundaryError("invalid execution attempt")
        row = await self._row(claim.task_id)
        if (
            row.status != "running"
            or row.run_id != claim.lease.run_id
            or row.payload["cancel_requested"]
        ):
            raise TaskBoundaryError("task no longer permits execution")
        # Acquire the SQLite writer boundary and re-check task status atomically.
        result = await self.session.execute(
            update(AgentTask)
            .where(
                AgentTask.id == row.id,
                AgentTask.status == "running",
                AgentTask.run_id == claim.lease.run_id,
                AgentTask.revision == row.revision,
            )
            .values(revision=AgentTask.revision)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise TaskBoundaryError("task fence lost")
        return row

    async def prepare_action(
        self,
        claim: TaskClaim,
        *,
        intent_key: str,
        parameters: dict[str, Any],
        effect: Literal["database", "external"] = "database",
    ) -> ToolAction:
        await self.fence(claim)
        digest = fingerprint({"parameters": parameters, "effect": effect})
        existing = await self.session.scalar(
            select(ToolAction).where(
                ToolAction.task_id == str(claim.task_id), ToolAction.intent_key == intent_key
            )
        )
        if existing:
            if existing.request_hash != digest:
                raise IdempotencyPayloadMismatchError(
                    "stable intent reused with different parameters"
                )
            return existing
        if not intent_key.strip() or len(intent_key) > 128:
            raise ValueError("invalid intent key")
        action = ToolAction(
            operation_id=str(uuid4()),
            task_id=str(claim.task_id),
            intent_key=intent_key,
            request_hash=digest,
            parameters=parameters,
            effect=effect,
            revision=1,
            state="prepared",
            result=None,
        )
        self.session.add(action)
        await self.session.flush()
        return action

    async def _action(self, task_id: UUID, operation_id: UUID) -> ToolAction:
        await self._row(task_id)
        action = await self.session.get(ToolAction, str(operation_id), populate_existing=True)
        if action is None or action.task_id != str(task_id):
            raise LookupError("action not found")
        return action

    async def settle_action(
        self, claim: TaskClaim, operation_id: UUID, write: WriteAction
    ) -> dict[str, Any]:
        async with self.session.begin_nested():
            await self.fence(claim)
            action = await self._action(claim.task_id, operation_id)
            if action.state == "committed":
                assert action.result is not None
                return action.result
            if action.effect != "database":
                raise ReconciliationRequiredError(
                    "external effects use start_external/reconcile_external"
                )
            written = await write(self.session, operation_id)
            result = written.output if isinstance(written, ActionResult) else written
            row = await self.fence(claim)  # callback cannot bypass a revoked lease/cancellation
            if isinstance(written, ActionResult) and written.artifact_refs:
                task = Task.model_validate(row.payload)
                refs = tuple(dict.fromkeys((*task.artifact_refs, *written.artifact_refs)))
                await self._save(row, task.model_copy(update={"artifact_refs": refs}))
                for ref in written.artifact_refs:
                    await self.append_event(
                        claim.task_id,
                        claim.attempt_id,
                        "artifact_saved",
                        {"ref": ref.model_dump(mode="json")},
                    )
            action.result = result
            action.state = "committed"
            await self.session.flush()
            return result

    async def _external_cas(
        self, action: ToolAction, expected_revision: int, state: str, result: dict[str, Any] | None
    ) -> int:
        changed = await self.session.execute(
            update(ToolAction)
            .where(
                ToolAction.operation_id == action.operation_id,
                ToolAction.revision == expected_revision,
                ToolAction.state != "committed",
            )
            .values(state=state, result=result, revision=expected_revision + 1)
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:  # type: ignore[attr-defined]
            raise ReconciliationRequiredError("stale external reconciliation generation")
        await self.session.refresh(action)
        return action.revision

    async def start_external(
        self, claim: TaskClaim, operation_id: UUID, *, expected_revision: int
    ) -> int:
        """Commit returned generation BEFORE I/O; reconcile using that generation."""
        await self.fence(claim)
        action = await self._action(claim.task_id, operation_id)
        if action.effect != "external" or action.state != "prepared":
            raise ReconciliationRequiredError(
                "read ledger and reconcile before invoking external effect"
            )
        return await self._external_cas(action, expected_revision, "uncertain", None)

    async def reconcile_external(
        self,
        task_id: UUID,
        operation_id: UUID,
        *,
        expected_revision: int,
        outcome: Literal["committed", "not_applied", "unknown"],
        result: dict[str, Any] | None = None,
    ) -> int:
        """Trusted provider reconciliation; generation prevents stale attempts reverting receipts.

        Allowed after cancellation, never invokes an effect. A confirmed not_applied
        advances the generation before another provider invocation can start.
        """
        action = await self._action(task_id, operation_id)
        if action.effect != "external":
            raise ValueError("database actions settle transactionally")
        if action.state == "committed":
            if outcome != "committed" or result != action.result:
                raise IdempotencyPayloadMismatchError("committed result is immutable")
            return action.revision
        if outcome == "committed" and result is None:
            raise ValueError("confirmed result required")
        return await self._external_cas(
            action,
            expected_revision,
            {"committed": "committed", "not_applied": "prepared", "unknown": "uncertain"}[outcome],
            result if outcome == "committed" else None,
        )

    async def recovery_actions(self, task_id: UUID) -> tuple[ToolAction, ...]:
        """Read before replanning; uncertain actions require provider reconciliation."""
        await self._row(task_id)
        return tuple(
            (
                await self.session.scalars(
                    select(ToolAction).where(ToolAction.task_id == str(task_id))
                )
            ).all()
        )

    async def checkpoint(
        self,
        claim: TaskClaim,
        *,
        checkpoint: dict[str, Any],
        waiting: WaitRequirement | None = None,
        wait_status: Literal["waiting_data", "waiting_user"] = "waiting_data",
        terminal: Literal["succeeded", "partial", "failed"] | None = None,
        stop_reason: StopReason | None = None,
        artifact_refs: tuple[ObjectRef, ...] | None = None,
    ) -> Task:
        async with self.session.begin_nested():
            row = await self.fence(claim)
            if waiting is not None and terminal is not None:
                raise ValueError("waiting and terminal are mutually exclusive")
            if terminal is not None and stop_reason is None:
                raise ValueError("terminal reason required")
            status = TaskStatus(terminal or (wait_status if waiting else "running"))
            task = Task.model_validate(row.payload).model_copy(
                update={
                    "status": status,
                    "waiting": waiting,
                    "stop_reason": stop_reason if terminal else None,
                    "artifact_refs": artifact_refs
                    if artifact_refs is not None
                    else tuple(Task.model_validate(row.payload).artifact_refs),
                }
            )
            Task.model_validate(task.model_dump())
            row.checkpoint = {"schema_version": 1, "data": checkpoint}
            row.resolved = []
            row.wait_token = str(uuid4()) if waiting else None
            await self._save(row, task)
            if waiting or terminal:
                attempt = await self.session.get(TaskAttempt, str(claim.attempt_id))
                assert attempt is not None
                attempt.status = status.value
                await self.runs.mark_success(claim.lease.run_id, lease=claim.lease)
            await self._state_event(row, claim.attempt_id)
        return Task.model_validate(row.payload)

    async def patch_checkpoint(self, claim: TaskClaim, *, data: dict[str, Any]) -> None:
        """CAS private-checkpoint update; never touches status or public events."""
        async with self.session.begin_nested():
            row = await self.fence(claim)
            row.checkpoint = {"schema_version": 1, "data": data}
            await self._save(row, Task.model_validate(row.payload))

    async def resume(
        self,
        task_id: UUID,
        dependency_id: UUID,
        *,
        wait_token: UUID,
        completion_event_id: UUID,
        now: datetime | None = None,
    ) -> bool:
        async with self.session.begin_nested():
            row = await self._row(task_id)
            task = Task.model_validate(row.payload)
            if row.status not in {"waiting_data", "waiting_user"}:
                return False
            assert task.waiting is not None
            if (
                row.wait_token != str(wait_token)
                or f"{dependency_id}:{completion_event_id}" in row.consumed_events
            ):
                return False
            if task.waiting.deadline <= (now or datetime.now(UTC)):
                await self.expire(task_id, now=now)
                return False
            if (
                dependency_id not in task.waiting.dependency_ids
                or str(dependency_id) in row.resolved
            ):
                return False
            row.resolved = [*row.resolved, str(dependency_id)]
            row.consumed_events = [*row.consumed_events, f"{dependency_id}:{completion_event_id}"]
            await self._save(row, task)  # CAS serializes duplicate dependency deliveries
            if not set(map(str, task.waiting.dependency_ids)) <= set(row.resolved):
                return False
            from investment_agent.storage.models.analysis import PipelineRun

            previous = await self.session.get(PipelineRun, row.run_id)
            assert previous is not None
            run, _ = await self.runs.enqueue(
                kind=previous.kind,
                triggered_by="agent",
                idempotency_key=f"agent:{row.id}:{row.revision}",
                request_json={"task_id": row.id},
            )
            row.run_id = run.id
            row.wait_token = None
            await self._save(
                row, task.model_copy(update={"status": TaskStatus.QUEUED, "waiting": None})
            )
            await self._state_event(row, None)
            return True

    async def expire(self, task_id: UUID, *, now: datetime | None = None) -> Task:
        row = await self._row(task_id)
        task = Task.model_validate(row.payload)
        if task.waiting is not None and task.waiting.deadline <= (now or datetime.now(UTC)):
            task = task.model_copy(
                update={
                    "status": TaskStatus.PARTIAL if task.artifact_refs else TaskStatus.FAILED,
                    "waiting": None,
                    "stop_reason": StopReason.EVIDENCE_BLOCKED,
                }
            )
            await self._save(row, task)
            await self._state_event(row, None)
        return Task.model_validate(row.payload)

    async def cancel(self, task_id: UUID) -> Task:
        async with self.session.begin_nested():
            # Serialize with queue claim/retry before deciding whether a call can
            # still be in flight. A queued retry has no active lease.
            await self.session.execute(update(AgentTask)
                .where(AgentTask.id == str(task_id), AgentTask.owner_id == self.owner)
                .values(revision=AgentTask.revision))
            row = await self._row(task_id)
            task = Task.model_validate(row.payload)
            run = await self.session.get(PipelineRun, row.run_id, populate_existing=True)
            active = run is not None and run.status == "running" and run.worker_id is not None
            if row.status not in TERMINAL and (not task.cancel_requested or not active):
                task = task.model_copy(update={
                    "status": TaskStatus.RUNNING if active else TaskStatus.CANCELLED,
                    "waiting": None, "cancel_requested": True,
                    "stop_reason": None if active else StopReason.CANCELLED,
                })
                if run is not None and run.status == "queued":
                    await self.session.execute(update(PipelineRun)
                        .where(PipelineRun.id == run.id, PipelineRun.status == "queued")
                        .values(status="failed", stage="cancelled", finished_at=datetime.now(UTC)))
                await self._save(row, task)
                await self._state_event(row, None)
            return Task.model_validate(row.payload)

    async def get_checkpoint(self, task_id: UUID) -> tuple[dict[str, Any], UUID | None]:
        row = await self._row(task_id)
        return row.checkpoint, UUID(row.wait_token) if row.wait_token else None

    async def ack_cancel(self, claim: TaskClaim) -> Task:
        """Executor acknowledges that the current tool has stopped at a boundary."""
        async with self.session.begin_nested():
            await self.runs.heartbeat(claim.lease.run_id, lease=claim.lease)
            row = await self._row(claim.task_id)
            attempt = await self.session.get(TaskAttempt, str(claim.attempt_id))
            if (
                attempt is None
                or attempt.task_id != row.id
                or attempt.run_id != claim.lease.run_id
                or attempt.lease_attempt != claim.lease.attempt
                or attempt.worker_id != claim.lease.worker_id
                or row.run_id != claim.lease.run_id
                or row.status != "running"
                or not row.payload["cancel_requested"]
            ):
                raise TaskBoundaryError("cancellation acknowledgement requires the current attempt")
            task = Task.model_validate(row.payload).model_copy(
                update={
                    "status": TaskStatus.CANCELLED,
                    "waiting": None,
                    "stop_reason": StopReason.CANCELLED,
                }
            )
            await self._save(row, task)
            attempt.status = "cancelled"
            await self.runs.mark_success(claim.lease.run_id, lease=claim.lease)
            await self._state_event(row, claim.attempt_id)
            return Task.model_validate(row.payload)

    async def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        """Bounded maintenance hook; AG08 registers it before worker polling.

        JSON deadlines are parsed via the task contract, never lexical timestamps.
        The cursor scans waiting rows so early non-due tasks cannot starve due ones.
        """
        if limit < 1:
            raise ValueError("positive limit required")
        at = now or datetime.now(UTC)
        rows = await self.session.scalars(
            select(AgentTask)
            .where(
                AgentTask.owner_id == self.owner,
                AgentTask.status.in_(("waiting_data", "waiting_user")),
            )
            .order_by(AgentTask.id)
        )
        count = 0
        for row in rows:
            task = Task.model_validate(row.payload)
            if task.waiting is not None and task.waiting.deadline <= at:
                await self.expire(task.id, now=at)
                count += 1
                if count >= limit:
                    break
        return count

"""Collector-backed current observation windows; admission and budgets stay fixed."""
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import ExecutionGrant
from investment_agent.agent.tasks import TaskService
from investment_agent.investor.common import utc_timestamp
from investment_agent.storage.models.agent import AgentTask, ExecutionGrantRow, ToolAction
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot


async def effective_cutoff(session: AsyncSession, task_id: UUID) -> datetime:
    service = TaskService(session)
    task = await service.get_task(task_id)
    saved = await session.get(ExecutionGrantRow, str(task_id))
    if saved is None or not ExecutionGrant.model_validate(saved.payload).current_research:
        return task.binding.as_of
    envelope, _ = await service.get_checkpoint(task_id)
    records = envelope.get("data", {}).get("observation_cutoffs", [])
    return max([task.binding.as_of, *(utc_timestamp(datetime.fromisoformat(r["cutoff"])) for r in records)])


async def resume_collectors(session: AsyncSession, *, limit: int = 100) -> int:
    """Poll only linked successful collectors with committed snapshot rows.

    Nothing in arbitrary deferred payloads changes the cutoff. The saved action
    identifies the queued run; raw_ref identifies this collector's committed
    captures. Historical tasks resume without moving their knowledge window.
    """
    from investment_agent.agent.executor import DEFERRED, deliver_deferred_result
    rows = (await session.scalars(select(AgentTask).where(AgentTask.status == "waiting_data").limit(limit))).all()
    resumed = 0
    for initial in rows:
        await session.execute(update(AgentTask).where(AgentTask.id == initial.id).values(revision=AgentTask.revision))
        row = await service_row(session, UUID(initial.id))
        if row.status != "waiting_data" or not row.wait_token:
            continue
        data = dict(row.checkpoint.get("data", {}))
        if not data.get("deferred"):
            continue
        deferred = DEFERRED.validate_python(data["deferred"])
        dependencies = {UUID(deferred.metadata[c.tool_call_id]["dependency_id"]) for c in deferred.calls}
        for dependency in dependencies:
            action = await session.get(ToolAction, str(dependency))
            if action is None or action.task_id != row.id or action.state != "committed" or action.parameters.get("tool") != "request_data_refresh":
                continue
            result = action.result or {}
            ref = result.get("data", {})
            if ref.get("kind") != "pipeline_run":
                continue
            run = await session.get(PipelineRun, ref["id"], populate_existing=True)
            if run is None or run.kind not in {"market_snapshot", "financial_snapshot"} or run.status != "success" or run.finished_at is None or run.idempotency_key != f"investor:local_owner:{dependency}":
                continue
            now = datetime.now(UTC)
            completed = utc_timestamp(run.finished_at.replace(tzinfo=UTC) if run.finished_at.tzinfo is None else run.finished_at)
            if completed > now:
                continue
            model = MarketSnapshot if run.kind == "market_snapshot" else FinancialSnapshot
            captures: Any = (await session.scalars(select(model).where(model.raw_ref.startswith(f"snapshot-runs/{run.id}/"), model.listing_id == run.listing_id))).all()
            if not captures or any(utc_timestamp(c.captured_at.replace(tzinfo=UTC) if c.captured_at.tzinfo is None else c.captured_at) > completed for c in captures):
                continue
            grant_row = await session.get(ExecutionGrantRow, row.id)
            assert grant_row is not None
            grant = ExecutionGrant.model_validate(grant_row.payload)
            # Only this trusted committed run can advance current observations.
            data = dict(row.checkpoint.get("data", {}))
            audit = list(data.get("observation_cutoffs", []))
            if grant.current_research and not any(r["dependency"] == str(dependency) for r in audit):
                audit.append({"dependency": str(dependency), "collector_run": run.id, "cutoff": completed.isoformat(),
                    "observed_at": now.isoformat(), "source_refs": [{"kind": "market_snapshot" if model is MarketSnapshot else "financial_snapshot", "id": c.id, "revision": c.version} for c in captures]})
                data["observation_cutoffs"] = audit
                row.checkpoint = {"schema_version": 1, "data": data}
                await session.flush()
            cutoff = await effective_cutoff(session, UUID(row.id))
            if await deliver_deferred_result(session, UUID(row.id), dependency, wait_token=UUID(row.wait_token),
                completion_event_id=uuid5(NAMESPACE_URL, f"collector:{run.id}:{dependency}"),
                result={"status": "ok", "collector_run": run.id, "as_of": cutoff.isoformat(), "coverage": "committed linked collector captures"}):
                resumed += 1
    return resumed


async def service_row(session: AsyncSession, task_id: UUID) -> AgentTask:
    return await TaskService(session)._row(task_id)

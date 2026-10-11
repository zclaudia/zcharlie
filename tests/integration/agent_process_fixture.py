"""AG20 child process: real API/worker/SQLite, fictional business semantics only."""
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn
from sqlalchemy import func, select

from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.tasks import TaskClaim, TaskService
from investment_agent.api.app import create_app
from investment_agent.config import Settings
from investment_agent.investor.enums import StopReason
from investment_agent.investor.monitoring import MonitoringService
from investment_agent.investor.publication import PublicationCandidate, PublicationService
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import portfolio, portfolio_plan, relationship  # noqa: F401
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.monitoring import InvestorNotification
from investment_agent.storage.models.publication import PublicationEvent, PublicationOutbox
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease
from tests.test_investor_publication import base, fixture, prepared


async def work(directory: Path, mode: str):
    engine = init_engine(f"sqlite+aiosqlite:///{directory / 'process.db'}")
    if mode == "seed":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        candidate, _, _ = await fixture()
        claim, _, review = await prepared(candidate)
        (directory / "state.json").write_text(json.dumps({"candidate": candidate.model_dump(mode="json"), "task_id": str(claim.task_id), "semantic": review.model_dump(mode="json")}))
        # A fresh worker must own the claim. Recover the seeded lease through the worker.
    state = json.loads((directory / "state.json").read_text())
    candidate = PublicationCandidate.model_validate(state["candidate"])
    first = mode == "crash"

    async def execute(claim, ctx):
        async with session_scope() as session:
            action = await TaskService(session).prepare_action(claim,
                intent_key=f"publish:{candidate.thesis.case_ref.id}",
                parameters={"candidate": candidate.model_dump(mode="json"), "semantic": state["semantic"]})
        # Use the semantic record frozen in the action, rather than new metadata.
        from investment_agent.investor.publication import SemanticReview
        review = SemanticReview.model_validate(action.parameters["semantic"])
        async with session_scope() as session:
            result = await PublicationService(session).publish(claim, UUID(action.operation_id), candidate, review, base=base())
            assert result.published
        async with session_scope() as session:
            await MonitoringService(session, base()).maintain()
        if first:
            (directory / "committed.json").write_text(json.dumps({"operation": action.operation_id, "attempt": claim.lease.attempt, "task_id": str(claim.task_id), "attempt_id": str(claim.attempt_id), "run_id": claim.lease.run_id, "worker_id": claim.lease.worker_id}))
            await asyncio.Event().wait()  # parent SIGKILLs exactly this process after commit
        async with session_scope() as session:
            await TaskService(session).checkpoint(claim, checkpoint={"replayed": action.operation_id}, terminal="succeeded", stop_reason=StopReason.ANSWERED, artifact_refs=(result.recommendation_ref,))
            counts = {model.__tablename__: await session.scalar(select(func.count()).select_from(model)) for model in (PublicationEvent, PublicationOutbox, InvestorNotification)}
            counts["operation"] = action.operation_id
            counts["attempt"] = claim.lease.attempt
        (directory / "recovered.json").write_text(json.dumps(counts))
        return "completed"

    worker = ResearchWorker(settings=base(), worker_id=f"process-{os.getpid()}", handlers=[AgentJobHandler("agent_turn", execute)], heartbeat_interval_seconds=0.03)
    await worker.recover_stale(stale_after_seconds=-1 if mode == "seed" else 0.1)
    if mode == "recover":
        old = json.loads((directory / "committed.json").read_text())
        old_claim = TaskClaim(UUID(old["task_id"]), UUID(old["attempt_id"]), RunLease(old["run_id"], old["worker_id"], old["attempt"]))
        async with session_scope() as session:
            try:
                await TaskService(session).prepare_action(old_claim, intent_key="obsolete-process-write", parameters={})
            except LeaseLostError:
                (directory / "lease-fenced").touch()
            else:
                raise AssertionError("old process lease was not fenced")
    if mode != "seed":
        await worker.run_once()
    await engine.dispose()


class ExitAfterCommit:
    def __init__(self, app, marker):
        self.app, self.marker = app, marker

    async def __call__(self, scope, receive, send):
        async def committed(message):
            if scope.get("path", "").endswith("/messages") and scope.get("method") == "POST" and message["type"] == "http.response.start" and message["status"] == 202:
                self.marker.write_text("admission committed; HTTP response not delivered")
                os._exit(86)
            await send(message)
        await self.app(scope, receive, committed)


if __name__ == "__main__":
    Settings.model_config["env_file"] = None
    mode, raw = sys.argv[1:3]
    directory = Path(raw).resolve()
    if not (directory / "ag20-isolated").exists():
        raise RuntimeError("isolated marker required")
    if mode.startswith("api"):
        app = create_app()
        uvicorn.run(ExitAfterCommit(app, directory / "api-committed") if mode == "api-crash" else app, host="127.0.0.1", port=int(sys.argv[3]), log_level="error")
    else:
        asyncio.run(work(directory, mode))

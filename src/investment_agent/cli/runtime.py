"""Shared CLI runtime helpers: settings bootstrap and in-process job runs.

The command modules used to repeat the same three pieces: the bare
``Settings()`` bootstrap, the ``cli:<host>:<pid>`` worker identity, and the
"run one queued job in this process and report it" block. They live here once.
The *decisions* stay with the commands: the ``--now`` short circuits (the
``not now`` / ``status != "queued"`` early returns and their printed lines) run
before these helpers, so the user-visible behaviour is unchanged.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Sequence
from typing import TYPE_CHECKING

from rich import print as rprint

from investment_agent.config import Settings
from investment_agent.storage.database import session_scope

if TYPE_CHECKING:
    from investment_agent.jobs.base import JobHandler
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.storage.models.analysis import PipelineRun


def bootstrap_settings() -> Settings:
    """The env / ``.env`` bootstrap ``Settings`` (before the DB runtime overlay).

    ``Settings`` reads ``API_KEY`` and every other field from the environment,
    so its required fields are deliberately absent from the constructor
    signature: this is the single documented ``type: ignore[call-arg]`` that
    every command used to carry. Commands that need the runtime layer on top
    pass the result to ``load_settings(base=...)`` (or ``load_runtime``).
    """

    return Settings()  # type: ignore[call-arg]


def worker_identity() -> str:
    """``cli:<host>:<pid>`` — the audit identity of a job run by this process."""

    return f"cli:{socket.gethostname()}:{os.getpid()}"


def inline_worker(
    settings: Settings,
    handlers: Sequence[JobHandler],
    *,
    kinds: Sequence[str] | None = None,
) -> ResearchWorker:
    """A :class:`~investment_agent.research.worker.ResearchWorker` for this process.

    Used by the flows that drive the worker themselves (the snapshot backfill
    loops, the peer collect JSON report); ``kinds`` restricts the served kinds
    like the ``ia worker start --kinds`` option does.
    """

    from investment_agent.research.worker import ResearchWorker

    return ResearchWorker(
        settings=settings,
        worker_id=worker_identity(),
        handlers=handlers,
        kinds=kinds,
    )


async def finished_run(run_id: int) -> PipelineRun:
    """The settled ``PipelineRun`` row of ``run_id`` (asserting it exists)."""

    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

    async with session_scope() as session:
        finished = await PipelineRunRepository(session).get(run_id)
    assert finished is not None
    return finished


async def run_inline(
    settings: Settings,
    *,
    run_id: int,
    handlers: Sequence[JobHandler],
    kinds: Sequence[str] | None = None,
) -> PipelineRun:
    """Execute one queued run in this process and print the finish line.

    Callers do the ``--now`` gating first; the returned row carries the job's
    ``status`` / ``error_summary`` / ``metadata_json`` for their extra report
    (``expire_on_commit=False`` keeps it readable after the fetch session).
    """

    worker = inline_worker(settings, handlers, kinds=kinds)
    await worker.run_once(run_id=run_id)
    finished = await finished_run(run_id)
    rprint(f"Run #{run_id} finished: status={finished.status}")
    return finished

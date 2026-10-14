"""Run-terminal statuses of a pipeline run: one home for "success/partial/failed".

A :class:`~investment_agent.storage.models.analysis.PipelineRun` is *terminal*
once it has reached one of these statuses: no worker will advance it again and
its outcome is final. ``success`` completed fully, ``partial`` finished with a
degraded outcome, ``failed`` exhausted the work it could do. The set is the
gate callers use before acting on a run's final status (settling a watchlist
refresh, notifying an origin, finalizing a rule draft, replaying a health read
model), so every one of them must test exactly these values.

This module deliberately imports nothing from the project: it is a ``domain``
leaf, so any package may depend on it without dragging in the persistence
layer. In particular :mod:`investment_agent.snapshots.health.models` is a
database-free model module that must not import the pipeline-run repository.

Do not conflate this with the agent-task status sets: ``agent.tasks.TERMINAL``
adds ``cancelled``, and ``agent.tasks.COMPLETED_TASK_STATUSES`` looks identical
by value but belongs to the task ledger.
"""

from __future__ import annotations

TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"success", "partial", "failed"})
"""Statuses a pipeline run can no longer leave (see module docstring)."""

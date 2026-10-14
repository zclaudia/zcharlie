"""Terminal status sets have one home each (E2b); the three sets stay distinct.

* run terminal (``success`` / ``partial`` / ``failed``) -> ``domain.pipeline_status``;
* task terminal, cancellation included -> ``agent.tasks.TERMINAL``;
* task "produced a result", cancellation excluded -> ``agent.tasks`` set C,
  which must not be folded into either of the other two.
"""

from typing import get_args

from investment_agent.agent.tasks import (
    COMPLETED_TASK_STATUSES,
    TERMINAL,
    CompletedTaskStatus,
)
from investment_agent.domain.pipeline_status import TERMINAL_RUN_STATUSES
from investment_agent.investor.discovery import TERMINAL as DISCOVERY_TERMINAL
from investment_agent.investor.monitoring import TERMINAL as MONITORING_TERMINAL
from investment_agent.snapshots.health.models import (
    TERMINAL_STATUSES as HEALTH_TERMINAL_STATUSES,
)
from investment_agent.storage.repositories.pipeline_run import TERMINAL_STATUSES


def test_run_terminal_set_has_one_shared_object():
    assert frozenset({"success", "partial", "failed"}) == TERMINAL_RUN_STATUSES
    assert isinstance(TERMINAL_RUN_STATUSES, frozenset)
    # Both public names re-export the single domain object.
    assert TERMINAL_STATUSES is TERMINAL_RUN_STATUSES
    assert HEALTH_TERMINAL_STATUSES is TERMINAL_RUN_STATUSES


def test_task_terminal_set_has_one_shared_object():
    assert {"succeeded", "partial", "failed", "cancelled"} == TERMINAL
    # Both investor duplicates now import the agent.tasks object itself.
    assert MONITORING_TERMINAL is TERMINAL
    assert DISCOVERY_TERMINAL is TERMINAL


def test_completed_task_set_excludes_cancelled_and_is_not_run_terminal():
    assert frozenset({"succeeded", "partial", "failed"}) == COMPLETED_TASK_STATUSES
    assert "cancelled" not in COMPLETED_TASK_STATUSES
    assert COMPLETED_TASK_STATUSES < TERMINAL


def test_completed_task_status_literal_matches_runtime_set():
    assert get_args(CompletedTaskStatus.__value__) == ("succeeded", "partial", "failed")
    assert frozenset(get_args(CompletedTaskStatus.__value__)) == COMPLETED_TASK_STATUSES

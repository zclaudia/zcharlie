"""iFinD on-demand verification (P1-A13): request queue, budget ledger, ``ifind_verify``.

Public entry point for other workflows (promote flow, AkShare cross-checks):
:func:`enqueue_verification`. See :mod:`.queue` (dedup / priority),
:mod:`.budget` (rolling-window ledger), :mod:`.job` (the queue job) and
:mod:`.schedule` (dispatcher, weekly calibration).
"""

from investment_agent.verification.queue import (
    TRIGGER_PRIORITY,
    enqueue_verification,
)

__all__ = ["TRIGGER_PRIORITY", "enqueue_verification"]

"""Compatibility shim: the iFinD budget ledger now lives in ``providers``.

The implementation moved to :mod:`investment_agent.providers.ifind_budget`
(B2): the ledger counts iFinD *provider* calls, and its old home inside
``verification`` forced ``verification`` to import ``snapshots.collect`` and
``research.ifind_chain`` at module level while both packages imported back for
the hook. The new location sits next to the twin ``provider_call`` ledger and
imports neither package.

This module only re-exports the moved public surface so existing imports of
``investment_agent.verification.budget`` keep working; new code should import
from :mod:`investment_agent.providers.ifind_budget` directly.
"""

from __future__ import annotations

from investment_agent.providers.ifind_budget import (
    DEFAULT_BUDGET,
    DEFAULT_WINDOW,
    BudgetedProxyClient,
    BudgetExhaustedError,
    BudgetStatus,
    ProxyClient,
    as_utc,
    budget_status,
    ledger_hook,
    record_call,
    ticker_count,
    utc_now,
)

__all__ = [
    "DEFAULT_BUDGET",
    "DEFAULT_WINDOW",
    "BudgetExhaustedError",
    "BudgetStatus",
    "BudgetedProxyClient",
    "ProxyClient",
    "as_utc",
    "budget_status",
    "ledger_hook",
    "record_call",
    "ticker_count",
    "utc_now",
]

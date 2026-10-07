"""Candidates (P1-D): lifecycle, decision journal and the promote flow.

* :mod:`~investment_agent.candidates.lifecycle` - candidates from screen
  hits and manual nominations, dismiss / expiry, rule-draft enqueueing;
* :mod:`~investment_agent.candidates.promote` - approve a rule and move a
  candidate onto the watchlist; rule reject / edit.

Every state change writes a :class:`~investment_agent.storage.models.candidates.DecisionRecord`
in the same transaction (funnel design 2: each transition is journaled).
"""

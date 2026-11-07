"""Explicit, stable business failure codes for simulation services and adapters."""

from typing import Literal

from investment_agent.investor.common import Contract, Text

SimulationErrorCode = Literal[
    "not_found",
    "scope_mismatch",
    "revision_conflict",
    "idempotency_conflict",
    "invalid_transition",
    "invalid_config",
    "unsupported_security",
    "unsupported_rule",
    "incomplete_data",
    "future_evidence",
    "deadline_passed",
    "invalid_decision",
    "insufficient_cash",
    "insufficient_sellable",
    "price_protection",
    "capacity_exceeded",
    "budget_exhausted",
    "model_failed",
    "model_timeout",
    "unknown_call",
    "settlement_blocked",
    "reconciliation_failed",
]


class SimulationFailure(Contract):
    code: SimulationErrorCode
    message: Text
    retryable: bool = False
    details: tuple[Text, ...] = ()


class SimulationError(Exception):
    def __init__(self, code: SimulationErrorCode, message: str, *, retryable: bool = False) -> None:
        self.failure = SimulationFailure(code=code, message=message, retryable=retryable)
        self.code = code
        self.retryable = retryable
        super().__init__(message)

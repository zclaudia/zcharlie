"""Typed provider errors shared by reference-data adapters."""

from __future__ import annotations

from datetime import datetime


class ProviderError(RuntimeError):
    """A provider call failed; terminal unless a subclass says otherwise."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.error_code = error_code


class ProviderTransientError(ProviderError):
    """Network / session / timeout failure that is worth retrying later."""


class ProviderUnavailableError(ProviderTransientError):
    """The circuit breaker is open for ``provider:endpoint``: no call was made.

    Raised by :class:`investment_agent.providers.circuit.CircuitBreaker` while
    the negative cache of a provider endpoint is active (``open_until``).
    Transient by nature - the next scheduled run retries once the window
    closes - but collectors stop the current job for that provider instead of
    walking every remaining ticker into the same closed door.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        open_until: datetime,
        endpoint: str = "",
        fail_count: int = 0,
    ) -> None:
        super().__init__(message, provider=provider, error_code="circuit_open")
        self.open_until = open_until
        self.endpoint = endpoint
        self.fail_count = fail_count


class ProviderDataError(ProviderError):
    """The provider answered, but the payload cannot be trusted or parsed."""


class LookaheadDataError(ProviderDataError):
    """The provider returned data effective after the capture (or as-of) date."""


class QuotaExhaustedError(ProviderError):
    """The provider's usage quota is exhausted (e.g. the iFinD gateway 5-hour limit).

    Terminal for the current job: retrying inside the job only burns time and
    whatever quota frees up. Collectors stop their remaining batches, keep
    what they stored and finish ``partial`` (reason ``quota``); the next
    scheduled run resumes naturally.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message, provider=provider, error_code="quota_exhausted")
        self.status_code = status_code
        self.detail = detail

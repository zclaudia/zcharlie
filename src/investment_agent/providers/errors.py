"""Typed provider errors shared by reference-data adapters."""

from __future__ import annotations


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


class ProviderDataError(ProviderError):
    """The provider answered, but the payload cannot be trusted or parsed."""


class LookaheadDataError(ProviderDataError):
    """The provider returned data effective after the capture (or as-of) date."""

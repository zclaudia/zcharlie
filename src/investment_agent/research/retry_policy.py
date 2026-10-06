"""Retry classification shared by the research chain and the queue worker."""

from __future__ import annotations

import socket

import httpx
import openai
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from sqlalchemy.exc import OperationalError

_SQLITE_TRANSIENT_MARKERS = ("database is locked", "database is busy", "database table is locked")


class ResearchRetryableError(RuntimeError):
    """Transient failure of a worker-owned run that the worker should retry.

    The research chain raises this (instead of persisting a terminal status)
    when it runs under a worker claim and a retry budget remains; the worker
    then atomically re-queues or fails the run.
    """

    def __init__(self, *, run_id: int, reason: str) -> None:
        super().__init__(reason)
        self.run_id = run_id
        self.reason = reason


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 429} or status_code >= 500


def is_transient_error(exc: BaseException) -> bool:
    """Return whether ``exc`` is worth retrying later.

    Network, timeout, rate-limit and 5xx failures (iFinD proxy via httpx, LLM
    via openai / pydantic-ai) and SQLite lock contention are transient. Local
    filesystem errors (``FileExistsError``, ``PermissionError``, disk full, ...)
    and validation/programming errors are terminal: retrying cannot fix them.
    """

    if isinstance(exc, ResearchRetryableError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_retryable_status(exc.response.status_code)
    if isinstance(exc, httpx.TransportError):
        # Includes httpx.TimeoutException, ConnectError, ReadError, ...
        return True
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return True
    if isinstance(exc, openai.APIStatusError):
        return _is_retryable_status(exc.status_code)
    if isinstance(exc, ModelHTTPError):
        return _is_retryable_status(exc.status_code)
    if isinstance(exc, ModelAPIError):
        # Non-HTTP provider failures (connection reset, timeouts) surface here.
        return True
    if isinstance(exc, OperationalError):
        message = str(exc.orig if exc.orig is not None else exc).lower()
        return any(marker in message for marker in _SQLITE_TRANSIENT_MARKERS)
    # TimeoutError and ConnectionError are OSError subclasses, so they must be
    # matched explicitly before the generic OSError is rejected.
    return isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror))

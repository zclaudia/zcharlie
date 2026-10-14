"""Timestamp normalization to aware UTC (stored / ISO datetimes).

Scope boundary: this module owns *timestamps* (a TZ-aware instant in UTC).
Business/trading *dates* in Asia/Shanghai belong to
:mod:`investment_agent.clock` (``today_in`` / ``shanghai_today``); the two
concerns never convert into each other here.

Naive input is read as UTC wall time because that is the SQLite convention in
this codebase: ``DateTime`` columns drop the offset, and every writer stores
UTC wall time (``datetime.now(UTC)`` or a value already normalized through
:func:`investment_agent.storage.repositories.pit.to_utc`). ``to_utc`` is the
strict sibling of :func:`as_utc`: it *rejects* naive input and is preferred
where a missing offset is a caller bug rather than stored data.

Two call sites deliberately preserve a caller-supplied offset instead of
normalizing and therefore do not use this helper:
:func:`investment_agent.agent.conversations.as_utc` and
``investment_agent.api.routes.rule_templates._as_utc``. Switching them to
this module would silently rewrite an aware value's offset.
"""

from __future__ import annotations

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime; the input is not mutated.

    Naive input is assumed to be UTC wall time (stored-data convention, see
    the module docstring); aware input is converted with ``astimezone(UTC)``.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def optional_utc(value: datetime | None) -> datetime | None:
    """None-tolerant :func:`as_utc`: ``None`` passes through as ``None``."""

    return None if value is None else as_utc(value)

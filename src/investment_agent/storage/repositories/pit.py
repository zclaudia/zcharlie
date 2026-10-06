"""Shared point-in-time helpers for snapshot repositories."""

from datetime import UTC, date, datetime, time, timedelta

from investment_agent.clock import SHANGHAI_TZ


class OutOfOrderSnapshotError(ValueError):
    """A snapshot dated before already-recorded changes cannot be applied.

    Interval tables are built forward in time; applying an older snapshot that
    changes state would rewrite history that later snapshots depend on.
    """


def to_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC (SQLite stores wall time only)."""

    if value.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    return value.astimezone(UTC)


def end_of_business_day_utc(as_of: date) -> datetime:
    """First instant after ``as_of`` in Asia/Shanghai, expressed in UTC.

    ``captured_at < end_of_business_day_utc(d)`` means "known by the end of d".
    """

    return datetime.combine(as_of + timedelta(days=1), time(0), SHANGHAI_TZ).astimezone(UTC)

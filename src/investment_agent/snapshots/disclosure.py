"""Statutory disclosure deadlines and overdue-report rules (P1-A6, decision D2).

A-share periodic reports must be published by:

| period | deadline |
|---|---|
| Q1 (03-31)     | 04-30 same year |
| semi (06-30)   | 08-31 same year |
| Q3 (09-30)     | 10-31 same year |
| annual (12-31) | 04-30 next year |

A target-pool member with no baostock report of a period once its deadline has
passed is ``report_overdue``: the period counts as unavailable (factors keep
using the previous one) until the late report arrives; the overdue record is
then resolved with the report's actual ``pubDate`` and kept as history.

Detection runs inside the baostock ``financial_snapshot`` job (scope
``universe``) whenever its ``as_of`` is after the period's deadline - the
scheduler fires that job on the first trading day after each deadline (and
every Saturday), so the scan reuses the deadline trigger and always sees the
freshest collection of the period.
"""

from __future__ import annotations

from datetime import date


def statutory_deadline(report_period: date) -> date:
    """Last day a periodic report for ``report_period`` may be published."""

    month_day = (report_period.month, report_period.day)
    if month_day == (3, 31):
        return date(report_period.year, 4, 30)
    if month_day == (6, 30):
        return date(report_period.year, 8, 31)
    if month_day == (9, 30):
        return date(report_period.year, 10, 31)
    if month_day == (12, 31):
        return date(report_period.year + 1, 4, 30)
    raise ValueError(f"not a report period: {report_period.isoformat()}")


def is_past_deadline(report_period: date, as_of: date) -> bool:
    """Whether a missing report of ``report_period`` is overdue on ``as_of``."""

    return as_of > statutory_deadline(report_period)

"""The SEC period/version state machine (split out of ``us_sec.py``, plan 04).

This module owns the period-facing value shapes and their version logic; it
depends on nothing but the standard library:

* the frozen dataclasses whose ``payload()`` methods are the shapes written to
  the database (:class:`FactRow`, :class:`FilingRef`, :class:`UnreliableNotice`,
  :class:`PeriodCandidate`, :class:`PeriodSelection`);
* :func:`select_period_state`, the pure version state machine (plan rule 12),
  and `_applicable_notice`, its Item 4.02 filter;
* the constants those read (:data:`PERIOD_STATES`, :data:`PERIOD_NOT_FILED`,
  ``UNRELIABLE_ITEM_CODE``, ``_ANNUAL_FORMS``) and the reference/time helpers
  they need (``_known_at``, ``_moment``, ``_reference_order``,
  ``_reference_known_at``, ``_iso_date``, ``_iso_datetime``).

``us_sec.py`` imports from here — never the other way around: the adapter feeds
:func:`select_period_state` its ``FilingRef`` objects and serialises the selected
state into the annual-facts and filing-index payloads.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

_ANNUAL_FORMS: tuple[str, ...] = ("10-K", "10-K/A", "20-F", "20-F/A")
"""Forms that describe an issuer's fiscal year (amendments included)."""

UNRELIABLE_ITEM_CODE = "4.02"
"""SEC EDGAR 8-K item code for "non-reliance on previously issued statements"."""

PERIOD_STATES: tuple[str, ...] = (
    "current",
    "superseded",
    "nonreliance_notice_known",
    "conflict",
)
"""The four fixed version states of a period."""

PERIOD_NOT_FILED = "not_yet_filed"
"""Absence of any known version at a cutoff — not one of the four states."""

_MIN_MOMENT = datetime.min.replace(tzinfo=UTC)


# --- public data structures ------------------------------------------------


@dataclass(frozen=True)
class FactRow:
    """One CompanyFacts observation, already keyed for selection."""

    taxonomy: str
    concept: str
    unit: str
    start: date | None
    end: date
    value: str
    accession: str
    form: str
    filed: date | None
    fy: int | None
    fp: str | None

    @property
    def is_duration(self) -> bool:
        return self.start is not None

    @property
    def span_days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days


@dataclass(frozen=True)
class FilingRef:
    """One filing as the version machine sees it for one described period.

    ``observations`` are the ``(metric, value)`` pairs this filing reports for
    ``period_end``. They are what lets a later report that re-cites an earlier
    period be compared with the period's own annual report.
    """

    accession: str
    form: str
    filing_date: date | None
    accepted_at: datetime | None
    report_period: date | None
    primary_document: str | None
    items: tuple[str, ...] = ()
    period_end: date | None = None
    observations: tuple[tuple[str, str], ...] = ()

    @property
    def is_amendment(self) -> bool:
        return self.form.endswith("/A")

    @property
    def is_annual(self) -> bool:
        return self.form in _ANNUAL_FORMS

    @property
    def is_own_annual(self) -> bool:
        """The annual report *for* ``period_end`` — not a later re-citation."""

        return (
            self.is_annual and self.period_end is not None and self.report_period == self.period_end
        )

    def observation(self, metric: str) -> str | None:
        for name, value in self.observations:
            if name == metric:
                return value
        return None

    def payload(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "form": self.form,
            "filing_date": _iso_date(self.filing_date),
            "accepted_at": _iso_datetime(self.accepted_at),
            "report_period": _iso_date(self.report_period),
            "primary_document": self.primary_document,
            "is_amendment": self.is_amendment,
        }


@dataclass(frozen=True)
class UnreliableNotice:
    """An 8-K Item 4.02 notice: earlier statements must not be relied on."""

    accession: str
    form: str
    accepted_at: datetime | None
    items: tuple[str, ...]
    known_at: datetime | None

    def payload(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "form": self.form,
            "accepted_at": _iso_datetime(self.accepted_at),
            "items": list(self.items),
            "known_at": _iso_datetime(self.known_at),
        }


@dataclass(frozen=True)
class PeriodCandidate:
    """One version describing the period: its state and its observations.

    ``report_period`` is the period the *filing itself* reports: a candidate
    whose ``report_period`` differs from the period key is a later report that
    re-cites this period, not an annual report for it.
    """

    accession: str
    form: str
    state: str
    usable: bool
    accepted_at: datetime | None
    report_period: date | None = None
    observations: tuple[tuple[str, str], ...] = ()

    def value_for(self, metric: str) -> str | None:
        for name, value in self.observations:
            if name == metric:
                return value
        return None

    def payload(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "form": self.form,
            "state": self.state,
            "usable": self.usable,
            "accepted_at": _iso_datetime(self.accepted_at),
            "report_period": _iso_date(self.report_period),
        }


@dataclass(frozen=True)
class PeriodSelection:
    """The version state of one period, from :func:`select_period_state`."""

    status: str
    selected_accession: str | None
    usable: bool
    candidates: tuple[PeriodCandidate, ...]
    known_at: datetime | None
    note: str | None = None

    def value_for(self, metric: str) -> str | None:
        """The selected version's value for ``metric`` (None if it reports none)."""

        for candidate in self.candidates:
            if candidate.accession == self.selected_accession:
                return candidate.value_for(metric)
        return None

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected_accession": self.selected_accession,
            "usable": self.usable,
            "known_at": _iso_datetime(self.known_at),
            "candidates": [candidate.payload() for candidate in self.candidates],
            "note": self.note,
        }


# --- the version state machine ---------------------------------------------


def select_period_state(
    filings: Sequence[FilingRef],
    notices: Sequence[UnreliableNotice],
    *,
    as_of: datetime | None = None,
) -> PeriodSelection:
    """Pure period version state from filings and Item 4.02 notices.

    Every filing in ``filings`` describes the same period (its ``period_end``);
    the period's own annual reports are those whose ``report_period`` equals
    that date, anything else carrying observations is a re-citation.

    Rules (plan rule 12):

    * the latest accepted own annual report is ``current`` and the versions it
      replaces are ``superseded`` — an earlier ``10-K/A`` replaces a ``10-K`` by
      acceptance time. The ``/A`` suffix alone is never trusted and
      ``is_restated`` is not consulted at all (the LNC revision files with
      ``is_restated`` still false);
    * a version with no observation at all in the saved CompanyFacts leaves the
      period ``superseded`` with ``usable=False`` while the previous value stays
      traceable;
    * an Item 4.02 notice accepted *after* the selected version marks the period
      ``nonreliance_notice_known`` with ``usable=False`` and the old value kept;
      a version accepted after the notice clears it again;
    * a re-citation whose value differs from the selected version's value for
      that metric makes the period ``conflict`` and keeps every candidate — no
      auto-overwrite and no growth-rate sewing;
    * ``as_of`` keeps only what was already public then (a filing without an
      acceptance time falls back to its filing date, and one without any date is
      treated as known); ``as_of=None`` means "everything known now".
    """

    known = tuple(ref for ref in filings if _known_at(ref.accepted_at, ref.filing_date, as_of))
    notices_known = tuple(
        notice for notice in notices if _known_at(notice.accepted_at, None, as_of)
    )
    own = tuple(ref for ref in known if ref.is_own_annual)
    if not known:
        return PeriodSelection(PERIOD_NOT_FILED, None, False, (), None, None)
    if not own:
        return PeriodSelection(
            PERIOD_NOT_FILED,
            None,
            False,
            tuple(
                PeriodCandidate(
                    ref.accession,
                    ref.form,
                    "conflict",
                    False,
                    ref.accepted_at,
                    ref.report_period,
                    ref.observations,
                )
                for ref in sorted(known, key=_reference_order)
            ),
            None,
            "no annual report for this period is part of the material; the listed "
            "filings only re-cite it",
        )

    ordered = sorted(own, key=_reference_order)
    authoritative = ordered[-1]
    selected = next((ref for ref in reversed(ordered) if ref.observations), authoritative)
    conflicting = [
        ref
        for ref in known
        if not ref.is_own_annual
        and any(
            value is not None and value != selected.observation(metric)
            for metric, value in ref.observations
        )
    ]
    notice = _applicable_notice(notices_known, selected)

    candidates: list[PeriodCandidate] = [
        PeriodCandidate(
            ref.accession,
            ref.form,
            "current" if ref.accession == selected.accession else "superseded",
            ref.accession == selected.accession and bool(ref.observations),
            ref.accepted_at,
            ref.report_period,
            ref.observations,
        )
        for ref in ordered
    ]
    conflicting_accessions = {ref.accession for ref in conflicting}
    candidates.extend(
        PeriodCandidate(
            ref.accession,
            ref.form,
            "conflict" if ref.accession in conflicting_accessions else "current",
            ref.accession not in conflicting_accessions,
            ref.accepted_at,
            ref.report_period,
            ref.observations,
        )
        for ref in sorted((ref for ref in known if not ref.is_own_annual), key=_reference_order)
    )

    note: str | None = None
    if conflicting:
        status, usable = "conflict", False
        note = (
            "re-cited values differ from the selected version: "
            + "; ".join(
                f"{ref.accession}="
                + ",".join(
                    f"{metric}:{value}"
                    for metric, value in ref.observations
                    if value != selected.observation(metric)
                )
                for ref in sorted(conflicting, key=_reference_order)
            )
            + " (all candidates kept; no overwrite, no growth rate)"
        )
    elif notice is not None:
        status, usable = "nonreliance_notice_known", False
        note = (
            f"8-K {notice.accession} reports item {UNRELIABLE_ITEM_CODE} after "
            f"{selected.accession}; the previous value is kept for traceability"
        )
    elif selected is authoritative and selected.observations:
        status, usable = "current", True
    elif selected is authoritative:
        status, usable = "current", False
        note = "the current annual report carries no observation for this period"
    else:
        status, usable = "superseded", False
        note = (
            f"{authoritative.accession} carries no observation for this period in the saved "
            f"CompanyFacts; the value shown is the superseded {selected.accession}"
        )

    return PeriodSelection(
        status=status,
        selected_accession=selected.accession,
        usable=usable,
        candidates=tuple(candidates),
        known_at=notice.known_at if notice is not None else _reference_known_at(selected),
        note=note,
    )


def _applicable_notice(
    notices: Sequence[UnreliableNotice], selected: FilingRef
) -> UnreliableNotice | None:
    """The newest Item 4.02 notice that invalidates ``selected`` (or None)."""

    selected_at = _moment(selected.accepted_at, selected.filing_date)
    applicable = [
        notice
        for notice in notices
        if UNRELIABLE_ITEM_CODE in notice.items
        and (selected_at is None or notice.accepted_at is None or notice.accepted_at > selected_at)
    ]
    if not applicable:
        return None
    return max(applicable, key=lambda notice: _moment(notice.accepted_at, None) or _MIN_MOMENT)


# --- small helpers ---


def _known_at(
    accepted_at: datetime | None, filing_date: date | None, as_of: datetime | None
) -> bool:
    if as_of is None:
        return True
    moment = _moment(accepted_at, filing_date)
    return moment is None or moment <= as_of


def _moment(accepted_at: datetime | None, filing_date: date | None) -> datetime | None:
    if accepted_at is not None:
        return accepted_at
    if filing_date is not None:
        return datetime.combine(filing_date, time.min, tzinfo=UTC)
    return None


def _reference_order(ref: FilingRef) -> tuple[datetime, str]:
    return (_moment(ref.accepted_at, ref.filing_date) or _MIN_MOMENT, ref.accession)


def _reference_known_at(ref: FilingRef) -> datetime | None:
    return _moment(ref.accepted_at, ref.filing_date)


def _iso_date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _iso_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()

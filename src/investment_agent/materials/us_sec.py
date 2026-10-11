"""US SEC CompanyFacts + Submissions adapter (plan 04).

Two controlled sources, two datasets:

* ``sec.companyfacts.v1`` -> ``annual_facts``: one :class:`DatasetDraft` per
  (company, fiscal year). The fiscal year comes from the issuer's own filing
  period (the ``start``/``end`` of its ``10-K`` / ``20-F`` rows, cross-checked
  against the Submissions ``reportDate``) — never from ``frames`` and never from
  a hard-coded December 31st. 52/53-week years keep their true span.
* ``sec.submissions.v1`` -> ``filing_index``: one per company (``period=None``),
  carrying the filings, the merged historical shards, the per-period version
  state and the Item 4.02 notices. Its ``payload["facts"]`` is the empty list
  the frozen contract requires of every payload.

Decisions kept from the plan:

* **No EdgarTools** (``pyproject.toml`` untouched, offline environment): version
  selection and eligibility belong to this application, so the bounded parsing
  and the version state machine live here. :func:`select_period_state` pins the
  behaviour for the LNC case; if a third-party parser is ever introduced, this
  module's tests (``tests/unit/test_material_us_sec.py``) must be re-run against
  the new library version — the plan requires exactly that.
* **Fact selection key** = ``CIK + taxonomy + concept + unit + start/end +
  accession + form``. ``duration`` (a ``start`` is present) and ``instant`` (no
  ``start``) rows never mix. ``frame`` is not read at all: it stays in the
  fixtures only to prove that deleting it changes nothing.
* **Missing is not zero**: a mapped metric without a standard tag produces
  ``raw_value=None`` + ``missing_reason`` (JPM does not tag
  ``PaymentsToAcquirePropertyPlantAndEquipment``; no nearby tag is substituted),
  and differing versions stay visible as separate fact entries with
  ``conflict="unresolved"`` while the period state names the current version.
* **Taxonomy whitelist** = ``us-gaap:*`` and ``ifrs-full:*``. ``dei``/``srt``/
  ``ecd``/``ffd``/``invest`` and issuer extensions produce no facts and are
  counted in ``payload["unmapped"]["taxonomies"]``. TSM's IFRS amounts stay in
  TWD; the USD convenience translations of the same concepts are skipped and
  counted in ``payload["unmapped"]["unit_skipped"]`` — no conversion exists here.
* **Account standard comes from the data**: the whitelisted taxonomy satisfying
  the most mapped metrics wins (ties keep ``us-gaap``). That is how TSM lands on
  ``ifrs-full`` without an issuer-specific branch, and why ``profit_*`` for TSM
  only ever reads the 20-F series (``6-K`` rows are not annual-report forms).

Data discipline:

* ``acceptanceDateTime`` -> ``accepted_at`` + ``source_published_at`` +
  ``precision=TIMESTAMP``; only ``filed`` -> ``precision=DATE`` with
  ``source_published_at=None``; neither -> ``UNKNOWN`` with both published
  fields null. ``source_available_at`` stays null everywhere (unknown is
  unknown).
* ``captured_at`` is the real parse time; ``source_fetched_at`` belongs to the
  import path (task 05) and is never back-filled here.
* Drafts carry no ``raw_bytes``: one CompanyFacts document covers several fiscal
  years, so the raw artifact belongs to the fetch/import stage (05/06).
* Structural problems (non-JSON body, wrong shape, missing ``facts``, CIK
  mismatch, unreadable supplied shard) reject the whole parse
  (``drafts == ()``); incompleteness (a document or shard listed but not
  supplied) is reported in ``problems`` while drafts are still produced.

Fixture provenance (``tests/fixtures/materials/us/**``): trimmed from the saved
research responses under
``research/market-data-validation/evidence/{remaining-final-03/raw/us,validated-run-01/raw/sec}``
(AAPL/COST/JPM/XOM/TSM captured 2026-10-11; LNC from ``validated-run-01``). Only
the taxonomies/concepts/units and filings the tests exercise were kept, well
under 80 KB per company; ``accn``/``filed``/``form``/``fy``/``fp``/``start``/
``end``/``val``/``frame`` are retained. Each fixture's ``filings.files`` list is
trimmed to the shards shipped with that fixture, so no fixture claims a shard it
does not carry.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from investment_agent.domain.security import ControlledIdentity, source_symbol
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    AccountingStandard,
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    ParseResult,
    PeriodBasis,
    PerShareBasis,
    RawPayload,
    ReportingScope,
    SubjectKind,
    UpstreamRequest,
    Verification,
    fact_to_payload,
)

SOURCE_FACTS = "sec.companyfacts.v1"
SOURCE_FILINGS = "sec.submissions.v1"
ADAPTER_VERSION = "1"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SHARD_URL = "https://data.sec.gov/submissions/{name}"
USER_AGENT = "zcharlie-research/0.1 (personal research; contact in config)"
MIN_INTERVAL_SECONDS = 0.5
"""Lower bound shared with the app-wide limiter injected by task 06."""

FACTS_DOCUMENT = "companyfacts.json"
SUBMISSIONS_DOCUMENT = "submissions.json"
_ANNUAL_FORMS: tuple[str, ...] = ("10-K", "10-K/A", "20-F", "20-F/A")
"""Forms that describe an issuer's fiscal year (amendments included)."""
_ANNUAL_REPORT_FORMS: tuple[str, ...] = ("10-K", "20-F")
"""Non-amendment annual reports; only these define a fiscal-year period."""
_TAXONOMY_WHITELIST: tuple[str, ...] = ("us-gaap", "ifrs-full")
_ANNUAL_SPAN_RANGE = (330, 400)
UNRELIABLE_ITEM_CODE = "4.02"
"""SEC EDGAR 8-K item code for "non-reliance on previously issued statements"."""
DEFAULT_PERIOD_WINDOW = 4
"""Most recent fiscal years produced when no ``periods`` are requested."""
MISSING_TAG_REASON = "standard tag absent in CompanyFacts for this issuer"
_SIGN_CONVENTION = "as_reported"

PERIOD_STATES: tuple[str, ...] = (
    "current",
    "superseded",
    "nonreliance_notice_known",
    "conflict",
)
"""The four fixed version states of a period."""
PERIOD_NOT_FILED = "not_yet_filed"
"""Absence of any known version at a cutoff — not one of the four states."""

_UnitKind = Literal["currency", "per_share", "shares"]


@dataclass(frozen=True)
class ConceptBinding:
    """One taxonomy concept bound to a metric, with the caveats of that binding."""

    taxonomy: str
    concept: str
    metric: str
    unit_kind: _UnitKind
    basis: PeriodBasis
    equity_scope: EquityScope
    per_share_basis: PerShareBasis
    priority: int
    note: str


def _b(
    taxonomy: str,
    concept: str,
    metric: str,
    unit_kind: _UnitKind,
    basis: PeriodBasis,
    equity_scope: EquityScope,
    per_share_basis: PerShareBasis = PerShareBasis.NA,
    priority: int = 0,
    note: str = "",
) -> ConceptBinding:
    return ConceptBinding(
        taxonomy=taxonomy,
        concept=concept,
        metric=metric,
        unit_kind=unit_kind,
        basis=basis,
        equity_scope=equity_scope,
        per_share_basis=per_share_basis,
        priority=priority,
        note=note,
    )


#: Concept table. Every entry states the source concept's own definition; the
#: metric is this application's name for it. Both travel on the fact
#: (``source_field`` / ``source_label``), so a definition difference stays
#: visible instead of being silently normalised away.
CONCEPT_MAP: tuple[ConceptBinding, ...] = (
    # --- us-gaap (10-K filers: AAPL, COST, JPM, XOM) -------------------------
    _b(
        "us-gaap",
        "Revenues",
        "revenue_operating",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note=(
            "standard Revenues; for XOM this includes equity-method income and other "
            "revenue (349,585M FY2024) and is explicitly not the pure sales line "
            "(339,247M). The plan binds XOM's revenue_operating to this concept."
        ),
    ),
    _b(
        "us-gaap",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "revenue_operating",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        priority=1,
        note="contract revenue excluding assessed taxes; the current tag for AAPL/COST.",
    ),
    _b(
        "us-gaap",
        "NetIncomeLoss",
        "net_profit",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.ALL_PARENT,
        note=(
            "standard GAAP net income as filed (JPM 58,471M FY2024). us-gaap defines it "
            "as the portion attributable to the parent."
        ),
    ),
    _b(
        "us-gaap",
        "ProfitLoss",
        "profit_parent",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note=(
            "bound to profit_parent exactly as the plan's table says. us-gaap:ProfitLoss "
            "is the consolidated result *including* noncontrolling interests (XOM FY2024 "
            "35,063M vs NetIncomeLoss 33,680M), so the fact keeps equity_scope=total and "
            "never stands in for a parent-only number."
        ),
    ),
    _b(
        "us-gaap",
        "StockholdersEquity",
        "equity_parent",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.ALL_PARENT,
        note=(
            "total stockholders' equity (all parent equity holders, preferred included); "
            "never filled from or into equity_ordinary."
        ),
    ),
    _b(
        "us-gaap",
        "Assets",
        "total_assets",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.TOTAL,
        note="consolidated total assets, instant fact (no start).",
    ),
    _b(
        "us-gaap",
        "Liabilities",
        "total_liabilities",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.TOTAL,
        note="consolidated total liabilities, instant fact (no start).",
    ),
    _b(
        "us-gaap",
        "NetCashProvidedByUsedInOperatingActivities",
        "operating_cash_flow",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="cash provided by (used in) operating activities, as reported (JPM is negative).",
    ),
    _b(
        "us-gaap",
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "capex_ppe",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note=(
            "one cash expenditure component (PP&E purchases), not a universal capex "
            "definition. JPM does not tag it, so the metric is reported as missing."
        ),
    ),
    _b(
        "us-gaap",
        "EarningsPerShareBasic",
        "eps_basic",
        "per_share",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        PerShareBasis.BASIC,
        note="basic earnings per share of the issuer's common stock.",
    ),
    _b(
        "us-gaap",
        "EarningsPerShareDiluted",
        "eps_diluted",
        "per_share",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        PerShareBasis.DILUTED,
        note="diluted earnings per share of the issuer's common stock.",
    ),
    _b(
        "us-gaap",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "shares_weighted_basic",
        "shares",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        note="weighted average basic shares outstanding over the fiscal year.",
    ),
    _b(
        "us-gaap",
        "CommonStockSharesOutstanding",
        "shares_outstanding_end",
        "shares",
        PeriodBasis.INSTANT,
        EquityScope.ORDINARY,
        note="common shares outstanding at the balance-sheet date (instant fact).",
    ),
    # ``dei:EntityCommonStockSharesOutstanding`` is the plan's other candidate for
    # shares_outstanding_end. Rule 7 keeps ``dei`` outside the whitelist, so its
    # rows are counted in ``unmapped.taxonomies`` and never produce a fact; a test
    # asserts exactly that, and the us-gaap tag above carries the metric.
    # --- ifrs-full (20-F filer: TSM) -----------------------------------------
    _b(
        "ifrs-full",
        "Revenue",
        "revenue_operating",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="IFRS revenue from contracts with customers, 20-F series only.",
    ),
    _b(
        "ifrs-full",
        "ProfitLoss",
        "net_profit",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="IFRS consolidated profit for the period, including non-controlling interests.",
    ),
    _b(
        "ifrs-full",
        "ProfitLossAttributableToOwnersOfParent",
        "profit_parent",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.ALL_PARENT,
        note=(
            "the IFRS concept the plan's bare ``ProfitLoss`` entry means for a 20-F filer: "
            "TSM FY2024 1,158,380.2M TWD as filed in the 20-F, never the local-report "
            "figure and never stitched together with it."
        ),
    ),
    _b(
        "ifrs-full",
        "EquityAttributableToOwnersOfParent",
        "equity_parent",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.ALL_PARENT,
        note="equity attributable to owners of the parent; excludes non-controlling interests.",
    ),
    _b(
        "ifrs-full",
        "Assets",
        "total_assets",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.TOTAL,
        note="consolidated total assets, instant fact.",
    ),
    _b(
        "ifrs-full",
        "Liabilities",
        "total_liabilities",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.TOTAL,
        note="consolidated total liabilities, instant fact.",
    ),
    _b(
        "ifrs-full",
        "CashFlowsFromUsedInOperatingActivities",
        "operating_cash_flow",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="IFRS cash flows from (used in) operating activities.",
    ),
    _b(
        "ifrs-full",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "capex_ppe",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="cash purchases of PP&E classified as investing activities.",
    ),
    _b(
        "ifrs-full",
        "BasicEarningsLossPerShare",
        "eps_basic",
        "per_share",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        PerShareBasis.BASIC,
        note="basic earnings (loss) per share, TWD — no USD convenience value is read.",
    ),
    _b(
        "ifrs-full",
        "DilutedEarningsLossPerShare",
        "eps_diluted",
        "per_share",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        PerShareBasis.DILUTED,
        note="diluted earnings (loss) per share, TWD — no USD convenience value is read.",
    ),
    _b(
        "ifrs-full",
        "WeightedAverageShares",
        "shares_weighted_basic",
        "shares",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        note="weighted average number of shares over the fiscal year.",
    ),
    # TSM's share count is tagged as NumberOfSharesIssuedAndFullyPaid (issued and
    # fully paid). That is a different definition from "outstanding", so this
    # adapter does not bind it to shares_outstanding_end; the metric is simply
    # not part of the IFRS metric set.
)

#: Deterministic metric order inside one draft's ``facts`` list.
METRIC_ORDER: tuple[str, ...] = (
    "revenue_operating",
    "net_profit",
    "profit_parent",
    "total_assets",
    "total_liabilities",
    "equity_parent",
    "operating_cash_flow",
    "capex_ppe",
    "eps_basic",
    "eps_diluted",
    "shares_weighted_basic",
    "shares_outstanding_end",
)

_STANDARD_BY_TAXONOMY: dict[str, AccountingStandard] = {
    "us-gaap": AccountingStandard.US_GAAP,
    "ifrs-full": AccountingStandard.IFRS,
}

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


# --- the adapter ------------------------------------------------------------


@dataclass(frozen=True)
class SecFactsAdapter:
    """CompanyFacts + Submissions adapter (the ``MaterialAdapter`` protocol).

    ``subject_id`` is ``company.id`` in this application; a
    :class:`ControlledIdentity` is id-free by design, so the caller supplies it
    (task 06 owns the listing -> company mapping). It may be given on the
    instance or per call.

    ``requests()`` is a two-stage builder: the first call returns CompanyFacts +
    Submissions, and once ``parse()`` has seen the Submissions document every
    historical shard listed in ``filings.files[].name`` is appended as one more
    request — the shards must be fetched and merged, never skipped.
    """

    subject_id: int | None = None
    source: str = SOURCE_FACTS
    adapter_version: str = ADAPTER_VERSION
    _shard_names: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def requests(
        self,
        identity: ControlledIdentity,
        *,
        periods: Sequence[FactPeriod] = (),
        subject_id: int | None = None,
    ) -> tuple[UpstreamRequest, ...]:
        """Documents to fetch: CompanyFacts, Submissions, then its shards."""

        del periods, subject_id
        cik10 = _identity_cik(identity)
        return (
            UpstreamRequest(name=FACTS_DOCUMENT, url=COMPANYFACTS_URL.format(cik=int(cik10))),
            UpstreamRequest(name=SUBMISSIONS_DOCUMENT, url=SUBMISSIONS_URL.format(cik=int(cik10))),
            *(
                UpstreamRequest(name=name, url=SHARD_URL.format(name=name))
                for name in self._shard_names.get(cik10, ())
            ),
        )

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod] = (),
        subject_id: int | None = None,
    ) -> ParseResult:
        """Build the annual-facts drafts and the filing index from raw responses.

        ``periods`` restricts the fiscal years (matched by period end); empty
        means the most recent :data:`DEFAULT_PERIOD_WINDOW` fiscal years found in
        the response.
        """

        cik10 = _identity_cik(identity)
        company_id = subject_id if subject_id is not None else self.subject_id
        if company_id is None:
            raise ValueError(
                "SecFactsAdapter needs subject_id (the application's company id) on the "
                "instance or per parse() call"
            )

        fatal: list[str] = []
        soft: list[str] = []
        facts_doc = _document(raw, FACTS_DOCUMENT)
        submissions_doc = _document(raw, SUBMISSIONS_DOCUMENT)
        if facts_doc is None:
            soft.append(f"missing_document: {FACTS_DOCUMENT}")
        if submissions_doc is None:
            soft.append(f"missing_document: {SUBMISSIONS_DOCUMENT}")
            soft.extend(
                f"unexpected_document: {payload.name}"
                for payload in raw
                if payload.name not in (FACTS_DOCUMENT, SUBMISSIONS_DOCUMENT)
            )

        facts = None if facts_doc is None else _read_facts_document(facts_doc, cik10, fatal, soft)
        submissions = None
        if submissions_doc is not None:
            submissions = _read_submissions_document(
                cik10,
                submissions_doc,
                _shard_payloads(raw, submissions_doc),
                fatal,
                soft,
            )
        if fatal:
            return ParseResult(drafts=(), problems=tuple(fatal + soft))
        if submissions is not None:
            self._shard_names[cik10] = submissions.shard_names

        captured_at = datetime.now(UTC)
        annual = _annual_facts_drafts(
            identity=identity,
            facts=facts,
            submissions=submissions,
            periods=periods,
            subject_id=company_id,
            captured_at=captured_at,
            soft=soft,
        )
        index = _filing_index_draft(
            identity=identity,
            facts=facts,
            submissions=submissions,
            subject_id=company_id,
            captured_at=captured_at,
        )
        return ParseResult(
            drafts=(*annual, *((index,) if index is not None else ())), problems=tuple(soft)
        )


# --- CompanyFacts parsing ---------------------------------------------------


@dataclass(frozen=True)
class _FactsDocument:
    rows: tuple[FactRow, ...]
    entity_name: str | None
    taxonomy_counts: dict[str, int]
    unmapped_concepts: dict[str, int]


def _read_facts_document(
    payload: RawPayload, cik10: str, fatal: list[str], soft: list[str]
) -> _FactsDocument | None:
    body = _json_object(payload, fatal)
    if body is None:
        return None
    if not _cik_matches(body.get("cik"), cik10):
        fatal.append(
            f"cik_mismatch: {payload.name} expected={cik10} got={_cik_text(body.get('cik'))}"
        )
        return None
    facts_block = body.get("facts")
    if not isinstance(facts_block, Mapping):
        fatal.append(f"missing_facts: {payload.name}")
        return None

    rows: list[FactRow] = []
    taxonomy_counts: dict[str, int] = {}
    unmapped_concepts: dict[str, int] = {}
    for taxonomy, concepts in facts_block.items():
        if not isinstance(taxonomy, str) or not isinstance(concepts, Mapping):
            fatal.append(f"unexpected_shape: {payload.name} facts[{taxonomy!r}]")
            return None
        for concept, node in concepts.items():
            units = node.get("units") if isinstance(node, Mapping) else None
            if not isinstance(concept, str) or not isinstance(units, Mapping):
                fatal.append(f"unexpected_shape: {payload.name} facts[{taxonomy}][{concept!r}]")
                return None
            mapped = bool(_bindings_for(taxonomy, concept))
            for unit, unit_rows in units.items():
                if not isinstance(unit, str) or not isinstance(unit_rows, list):
                    fatal.append(
                        f"unexpected_shape: {payload.name} facts[{taxonomy}][{concept}][{unit!r}]"
                    )
                    return None
                for raw_row in unit_rows:
                    row = _fact_row(taxonomy, concept, unit, raw_row, payload.name, soft)
                    if row is None:
                        continue
                    rows.append(row)
                    taxonomy_counts[taxonomy] = taxonomy_counts.get(taxonomy, 0) + 1
                    if taxonomy in _TAXONOMY_WHITELIST and not mapped:
                        unmapped_concepts[taxonomy] = unmapped_concepts.get(taxonomy, 0) + 1

    entity_name = body.get("entityName")
    return _FactsDocument(
        rows=tuple(rows),
        entity_name=entity_name if isinstance(entity_name, str) else None,
        taxonomy_counts=taxonomy_counts,
        unmapped_concepts=unmapped_concepts,
    )


def _fact_row(
    taxonomy: str,
    concept: str,
    unit: str,
    raw_row: object,
    document: str,
    soft: list[str],
) -> FactRow | None:
    if not isinstance(raw_row, Mapping):
        soft.append(f"skipped_row: {document} {taxonomy}:{concept} is not an object")
        return None
    end_text = raw_row.get("end")
    accession = raw_row.get("accn")
    form = raw_row.get("form")
    value = raw_row.get("val")
    if (
        not isinstance(end_text, str)
        or not isinstance(accession, str)
        or not isinstance(form, str)
        or value is None
    ):
        soft.append(f"skipped_row: {document} {taxonomy}:{concept} lacks end/accn/form/val")
        return None
    end = _parse_date(end_text)
    start_text = raw_row.get("start")
    start = _parse_date(start_text) if isinstance(start_text, str) else None
    if end is None or (isinstance(start_text, str) and start is None):
        soft.append(f"skipped_row: {document} {taxonomy}:{concept} accn {accession} has a bad date")
        return None
    value_text = _decimal_text(value)
    if value_text is None:
        soft.append(
            f"skipped_row: {document} {taxonomy}:{concept} accn {accession} has a bad value"
        )
        return None
    return FactRow(
        taxonomy=taxonomy,
        concept=concept,
        unit=unit,
        start=start,
        end=end,
        value=value_text,
        accession=accession,
        form=form,
        filed=_parse_date(raw_row.get("filed")),
        fy=_parse_int(raw_row.get("fy")),
        fp=raw_row.get("fp") if isinstance(raw_row.get("fp"), str) else None,
    )


# --- Submissions parsing ----------------------------------------------------


@dataclass(frozen=True)
class _SubmissionsDocument:
    filings: dict[str, FilingRef]
    notices: tuple[UnreliableNotice, ...]
    filing_count: int
    shard_names: tuple[str, ...]
    merged_shards: tuple[str, ...]
    missing_shards: tuple[str, ...]


def _read_submissions_document(
    cik10: str,
    payload: RawPayload,
    shards: tuple[RawPayload, ...],
    fatal: list[str],
    soft: list[str],
) -> _SubmissionsDocument | None:
    body = _json_object(payload, fatal)
    if body is None:
        return None
    if not _cik_matches(body.get("cik"), cik10):
        fatal.append(
            f"cik_mismatch: {payload.name} expected={cik10} got={_cik_text(body.get('cik'))}"
        )
        return None
    filings_block = body.get("filings")
    if not isinstance(filings_block, Mapping):
        fatal.append(f"unexpected_shape: {payload.name} filings")
        return None
    recent = filings_block.get("recent")
    if not isinstance(recent, Mapping):
        fatal.append(f"missing_recent: {payload.name}")
        return None
    filings = _filing_rows(recent, payload.name, fatal, soft)
    if filings is None:
        return None

    shard_names = _shard_names(filings_block)
    merged: list[str] = []
    for shard in shards:
        if shard.name not in shard_names:
            soft.append(f"unexpected_document: {shard.name}")
            continue
        shard_body = _json_object(shard, fatal)
        if shard_body is None:
            return None
        table = _shard_table(shard_body, shard.name, fatal)
        if table is None:
            return None
        rows = _filing_rows(table, shard.name, fatal, soft)
        if rows is None:
            return None
        for accession, ref in rows.items():
            if accession in filings:
                soft.append(f"duplicate_accession: {accession} in {shard.name}")
                continue
            filings[accession] = ref
        merged.append(shard.name)
    missing = tuple(name for name in shard_names if name not in merged)
    soft.extend(f"missing_shard: {name}" for name in missing)

    notices = tuple(
        UnreliableNotice(
            accession=ref.accession,
            form=ref.form,
            accepted_at=ref.accepted_at,
            items=ref.items,
            known_at=_reference_known_at(ref),
        )
        for ref in sorted(filings.values(), key=_reference_order)
        if ref.form == "8-K" and UNRELIABLE_ITEM_CODE in ref.items
    )
    return _SubmissionsDocument(
        filings=filings,
        notices=notices,
        filing_count=len(filings),
        shard_names=shard_names,
        merged_shards=tuple(merged),
        missing_shards=missing,
    )


def _filing_rows(
    table: Mapping[str, Any], document: str, fatal: list[str], soft: list[str]
) -> dict[str, FilingRef] | None:
    accessions = table.get("accessionNumber")
    if not isinstance(accessions, list):
        fatal.append(f"unexpected_shape: {document} accessionNumber")
        return None
    columns = {
        name: table.get(name)
        for name in (
            "filingDate",
            "reportDate",
            "acceptanceDateTime",
            "form",
            "items",
            "primaryDocument",
        )
    }
    for name, values in columns.items():
        if values is not None and not isinstance(values, list):
            fatal.append(f"unexpected_shape: {document} {name}")
            return None
    filings: dict[str, FilingRef] = {}
    for index, accession in enumerate(accessions):
        if not isinstance(accession, str):
            soft.append(f"skipped_filing: {document} row {index} accession is not a string")
            continue
        form = _cell(columns, "form", index)
        filings[accession] = FilingRef(
            accession=accession,
            form=form if isinstance(form, str) else "",
            filing_date=_parse_date(_cell(columns, "filingDate", index)),
            accepted_at=_parse_datetime(_cell(columns, "acceptanceDateTime", index)),
            report_period=_parse_date(_cell(columns, "reportDate", index)),
            primary_document=_text_or_none(_cell(columns, "primaryDocument", index)),
            items=_items(_cell(columns, "items", index)),
        )
    return filings


def _cell(columns: Mapping[str, Any], name: str, index: int) -> object:
    values = columns.get(name)
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


def _shard_table(body: Mapping[str, Any], name: str, fatal: list[str]) -> Mapping[str, Any] | None:
    """Saved shard bodies are the bare column table; accept the wrapped shape too."""

    if "accessionNumber" in body:
        return body
    filings_block = body.get("filings")
    if isinstance(filings_block, Mapping):
        if "accessionNumber" in filings_block:
            return filings_block
        recent = filings_block.get("recent")
        if isinstance(recent, Mapping):
            return recent
    fatal.append(f"unexpected_shape: {name}")
    return None


def _shard_names(filings_block: Mapping[str, Any]) -> tuple[str, ...]:
    files = filings_block.get("files")
    if not isinstance(files, list):
        return ()
    names: list[str] = []
    for entry in files:
        if isinstance(entry, Mapping):
            name = entry.get("name")
            if isinstance(name, str):
                names.append(name)
    return tuple(names)


# --- annual facts -----------------------------------------------------------


@dataclass(frozen=True)
class _AnnualPeriod:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def _annual_facts_drafts(
    *,
    identity: ControlledIdentity,
    facts: _FactsDocument | None,
    submissions: _SubmissionsDocument | None,
    periods: Sequence[FactPeriod],
    subject_id: int,
    captured_at: datetime,
    soft: list[str] | None = None,
) -> tuple[DatasetDraft, ...]:
    if facts is None or not facts.rows:
        if periods and soft is not None:
            soft.extend(f"missing_period: {period.end.isoformat()}" for period in periods)
        return ()
    report_currency = identity.report_currency
    taxonomy = _choose_taxonomy(facts.rows, report_currency)
    standard = _STANDARD_BY_TAXONOMY[taxonomy]
    metrics = _metrics_for(taxonomy)
    unit_skipped = _unit_skipped_counts(facts.rows, taxonomy, report_currency)
    filings_by_accession = {} if submissions is None else submissions.filings
    notices = () if submissions is None else submissions.notices

    targets = _target_periods(facts.rows, periods)
    if periods and soft is not None:
        found = {period.end for period in targets}
        soft.extend(
            f"missing_period: {period.end.isoformat()}"
            for period in periods
            if period.end not in found
        )
    drafts: list[DatasetDraft] = []
    for period in targets:
        references = _period_references(facts.rows, filings_by_accession, period, taxonomy, metrics)
        selection = select_period_state(references, notices)
        entries: list[dict[str, Any]] = []
        missing: list[str] = []
        for metric in metrics:
            metric_entries, metric_missing = _metric_entries(
                rows=facts.rows,
                filings_by_accession=filings_by_accession,
                taxonomy=taxonomy,
                standard=standard,
                metric=metric,
                period=period,
                report_currency=report_currency,
                selection=selection,
            )
            entries.extend(metric_entries)
            if metric_missing:
                missing.append(metric)
        label = _fiscal_label(facts.rows, period)
        payload: dict[str, Any] = {
            "facts": entries,
            "cik": _identity_cik(identity),
            "entity_name": facts.entity_name,
            "report_currency": report_currency,
            "accounting_standard": standard.value,
            "fiscal_year_label": label,
            "period_state": selection.payload(),
            "missing_metrics": missing,
            "unmapped": {
                "taxonomies": dict(sorted(facts.taxonomy_counts.items())),
                "concepts": dict(sorted(facts.unmapped_concepts.items())),
                "unit_skipped": dict(sorted(unit_skipped.items())),
            },
            "filings_with_known_acceptance": sum(
                1 for ref in references if ref.accepted_at is not None
            ),
            "documents": [
                FACTS_DOCUMENT,
                *(() if submissions is None else submissions.merged_shards),
            ],
        }
        drafts.append(
            DatasetDraft(
                kind=DatasetKind.ANNUAL_FACTS,
                subject_kind=SubjectKind.COMPANY,
                subject_id=subject_id,
                source=SOURCE_FACTS,
                schema_version=SCHEMA_VERSION,
                adapter_version=ADAPTER_VERSION,
                captured_at=captured_at,
                payload=payload,
                period=FactPeriod(
                    start=period.start,
                    end=period.end,
                    basis=PeriodBasis.ANNUAL,
                    label=label,
                    days=period.days,
                ),
                raw_source_url=COMPANYFACTS_URL.format(cik=int(_identity_cik(identity))),
            )
        )
    drafts.sort(key=lambda draft: draft.period.end if draft.period is not None else date.min)
    return tuple(drafts)


def _metric_entries(
    *,
    rows: Sequence[FactRow],
    filings_by_accession: Mapping[str, FilingRef],
    taxonomy: str,
    standard: AccountingStandard,
    metric: str,
    period: _AnnualPeriod,
    report_currency: str,
    selection: PeriodSelection,
) -> tuple[list[dict[str, Any]], bool]:
    bindings = [
        binding
        for binding in CONCEPT_MAP
        if binding.metric == metric and binding.taxonomy == taxonomy
    ]
    binding = min(bindings, key=lambda item: item.priority)
    concepts = {item.concept for item in bindings}
    expected_unit = _expected_unit(binding, report_currency)
    candidates = [
        row
        for row in rows
        if row.taxonomy == taxonomy
        and row.concept in concepts
        and _unit_matches(row.unit, binding, report_currency)
        and _row_in_period(row, period)
    ]
    fact_period = _fact_period(binding, _fiscal_label(candidates, period), period)
    if not candidates:
        return [
            fact_to_payload(
                _fact_record(
                    metric=metric,
                    period=fact_period,
                    binding=binding,
                    standard=standard,
                    raw_value=None,
                    raw_unit=expected_unit.raw_unit,
                    value_unit=expected_unit,
                    provenance=_provenance(accession=None, filed=None, accepted=None),
                    conflict="none",
                    conflict_note=None,
                    applicability="unknown",
                    source_version=None,
                    missing_reason=MISSING_TAG_REASON,
                )
            )
        ], True

    ordered = _candidate_order(candidates, selection.selected_accession)
    values: list[tuple[str, FactRow]] = []
    for row in ordered:
        if all(row.value != value for value, _ in values):
            values.append((row.value, row))
    conflict = "none" if len(values) == 1 else "unresolved"
    note = (
        None
        if conflict == "none"
        else "candidates kept: " + "; ".join(f"{row.accession}={value}" for value, row in values)
    )
    entries: list[dict[str, Any]] = []
    for value, row in values:
        reference = filings_by_accession.get(row.accession)
        row_binding = next((item for item in bindings if item.concept == row.concept), binding)
        entries.append(
            fact_to_payload(
                _fact_record(
                    metric=metric,
                    period=fact_period,
                    binding=row_binding,
                    standard=standard,
                    raw_value=value,
                    raw_unit=row.unit,
                    value_unit=expected_unit,
                    provenance=_provenance(
                        accession=row.accession,
                        filed=row.filed,
                        accepted=None if reference is None else reference.accepted_at,
                    ),
                    conflict=conflict,
                    conflict_note=note,
                    applicability="applicable",
                    source_version=row.accession,
                    missing_reason=None,
                )
            )
        )
    return entries, False


def _candidate_order(
    candidates: Sequence[FactRow], selected_accession: str | None
) -> list[FactRow]:
    """The selected filing's rows first, then by filing time and accession."""

    def key(row: FactRow) -> tuple[int, str, str]:
        return (
            0 if row.accession == selected_accession else 1,
            row.filed.isoformat() if row.filed is not None else "",
            row.accession,
        )

    return sorted(candidates, key=key)


def _fact_period(binding: ConceptBinding, label: str | None, period: _AnnualPeriod) -> FactPeriod:
    if binding.basis == PeriodBasis.INSTANT:
        return FactPeriod(start=None, end=period.end, basis=PeriodBasis.INSTANT, label=label)
    return FactPeriod(
        start=period.start,
        end=period.end,
        basis=PeriodBasis.ANNUAL,
        label=label,
        days=period.days,
    )


def _fiscal_label(rows: Sequence[FactRow], period: _AnnualPeriod) -> str | None:
    """``FY<fy>`` from the source rows; never derived from the period's year."""

    years = [row.fy for row in rows if row.fy is not None and _row_in_period(row, period)]
    if not years:
        return None
    return f"FY{min(years)}"


@dataclass(frozen=True)
class _ExpectedUnit:
    unit: str
    currency: str | None
    raw_unit: str


def _expected_unit(binding: ConceptBinding, report_currency: str) -> _ExpectedUnit:
    if binding.unit_kind == "currency":
        return _ExpectedUnit(report_currency, report_currency, report_currency)
    if binding.unit_kind == "per_share":
        return _ExpectedUnit(
            f"{report_currency}/share", report_currency, f"{report_currency}/shares"
        )
    return _ExpectedUnit("shares", None, "shares")


def _unit_matches(unit: str, binding: ConceptBinding, report_currency: str) -> bool:
    if binding.unit_kind == "currency":
        return unit == report_currency
    if binding.unit_kind == "per_share":
        return unit in (f"{report_currency}/share", f"{report_currency}/shares")
    return unit == "shares"


def _row_in_period(row: FactRow, period: _AnnualPeriod) -> bool:
    if row.is_duration:
        return row.start == period.start and row.end == period.end
    return row.end == period.end


def _fact_record(
    *,
    metric: str,
    period: FactPeriod,
    binding: ConceptBinding,
    standard: AccountingStandard,
    raw_value: str | None,
    raw_unit: str,
    value_unit: _ExpectedUnit,
    provenance: FactProvenance,
    conflict: str,
    conflict_note: str | None,
    applicability: str,
    source_version: str | None,
    missing_reason: str | None,
) -> FactRecord:
    has_value = raw_value is not None
    semantics = FactSemantics(
        metric=metric,
        source_label=binding.concept if has_value else None,
        source_field=f"{binding.taxonomy}:{binding.concept}" if has_value else None,
        accounting_standard=standard,
        reporting_scope=ReportingScope.CONSOLIDATED,
        equity_scope=binding.equity_scope,
        per_share_basis=binding.per_share_basis,
    )
    locator = (
        None
        if not has_value or provenance.accession is None
        else f"CompanyFacts {binding.taxonomy}:{binding.concept} accn {provenance.accession}"
    )
    return FactRecord(
        metric=metric,
        period=period,
        semantics=semantics,
        value=FactValue(
            raw_value=raw_value,
            raw_unit=raw_unit,
            scale=1,
            unit=value_unit.unit,
            currency=value_unit.currency,
            sign_convention=_SIGN_CONVENTION,
            missing_reason=missing_reason,
        ),
        provenance=provenance,
        verification=Verification.CANDIDATE,
        conflict=conflict,
        applicability=applicability,
        conflict_note=conflict_note,
        original_locator=locator,
        source_version=source_version,
    )


def _provenance(
    *,
    accession: str | None,
    filed: date | None,
    accepted: datetime | None,
) -> FactProvenance:
    if accepted is not None:
        precision = DatePrecision.TIMESTAMP
        published_date = filed if filed is not None else accepted.date()
    elif filed is not None:
        precision = DatePrecision.DATE
        published_date = filed
    else:
        precision = DatePrecision.UNKNOWN
        published_date = None
    return FactProvenance(
        source=SOURCE_FACTS,
        accession=accession,
        original_hash=None,
        source_published_at=accepted,
        source_published_date=published_date,
        source_published_precision=precision,
        accepted_at=accepted,
        source_available_at=None,
    )


# --- period discovery and filing references ---------------------------------


def _discover_periods(rows: Sequence[FactRow]) -> tuple[_AnnualPeriod, ...]:
    """Every fiscal year found in the issuer's own annual-report rows.

    A period exists only where a non-amendment ``10-K``/``20-F`` row states a
    fiscal-year-long duration — so the boundary is the issuer's, never a
    calendar quarter or a ``frame`` label.
    """

    low, high = _ANNUAL_SPAN_RANGE
    discovered: dict[date, _AnnualPeriod] = {}
    for row in rows:
        if row.form not in _ANNUAL_REPORT_FORMS or not row.is_duration:
            continue
        span = row.span_days
        if span is None or not low <= span <= high or row.start is None:
            continue
        discovered[row.end] = _AnnualPeriod(row.start, row.end)
    return tuple(sorted(discovered.values(), key=lambda period: period.end))


def _target_periods(
    rows: Sequence[FactRow], periods: Sequence[FactPeriod]
) -> tuple[_AnnualPeriod, ...]:
    """The fiscal years this parse should publish.

    ``periods`` filters the discovered years by period end; empty keeps the most
    recent :data:`DEFAULT_PERIOD_WINDOW` of them.
    """

    discovered = _discover_periods(rows)
    if periods:
        wanted = {period.end for period in periods}
        return tuple(period for period in discovered if period.end in wanted)
    return discovered[-DEFAULT_PERIOD_WINDOW:]


def _period_references(
    rows: Sequence[FactRow],
    filings_by_accession: Mapping[str, FilingRef],
    period: _AnnualPeriod,
    taxonomy: str,
    metrics: Sequence[str],
) -> tuple[FilingRef, ...]:
    """`FilingRef`s for one period: own annual reports plus every re-citation.

    An own annual report appears even when it carries no observation for the
    period (its facts may be absent from the saved CompanyFacts) — that is what
    lets the state machine mark the period ``superseded`` instead of silently
    presenting the older value as current.
    """

    concepts_by_metric = {metric: _metric_concepts(taxonomy, metric) for metric in metrics}
    observations: dict[str, list[tuple[str, str]]] = {}
    forms: dict[str, tuple[str, date | None]] = {}
    for row in rows:
        if not _row_in_period(row, period):
            continue
        forms.setdefault(row.accession, (row.form, row.filed))
        for metric, concepts in concepts_by_metric.items():
            values = observations.setdefault(row.accession, [])
            if row.concept in concepts and all(name != metric for name, _ in values):
                values.append((metric, row.value))

    references: dict[str, FilingRef] = {}
    for ref in filings_by_accession.values():
        if ref.is_annual and ref.report_period == period.end:
            references[ref.accession] = replace(
                ref,
                period_end=period.end,
                observations=tuple(observations.get(ref.accession, ())),
            )
    for accession, observed in observations.items():
        if accession in references:
            continue
        base = filings_by_accession.get(accession)
        form, filed = forms.get(accession, ("", None))
        references[accession] = (
            replace(base, period_end=period.end, observations=tuple(observed))
            if base is not None
            else FilingRef(
                accession=accession,
                form=form,
                filing_date=filed,
                accepted_at=None,
                report_period=None,
                primary_document=None,
                period_end=period.end,
                observations=tuple(observed),
            )
        )
    return tuple(references.values())


def _metric_concepts(taxonomy: str, metric: str) -> frozenset[str]:
    return frozenset(
        binding.concept
        for binding in CONCEPT_MAP
        if binding.taxonomy == taxonomy and binding.metric == metric
    )


def _choose_taxonomy(rows: Sequence[FactRow], report_currency: str) -> str:
    """Whitelisted taxonomy satisfying the most mapped metrics (ties: us-gaap)."""

    best = _TAXONOMY_WHITELIST[0]
    best_score = -1
    for taxonomy in _TAXONOMY_WHITELIST:
        bindings = _bindings_of(taxonomy)
        present = {(row.concept, row.unit) for row in rows if row.taxonomy == taxonomy}
        score = sum(
            1
            for metric in _metrics_for(taxonomy)
            if any(
                binding.metric == metric
                and (binding.concept, _expected_unit(binding, report_currency).raw_unit) in present
                for binding in bindings
            )
        )
        if score > best_score:
            best, best_score = taxonomy, score
    return best


def _bindings_of(taxonomy: str) -> tuple[ConceptBinding, ...]:
    return tuple(binding for binding in CONCEPT_MAP if binding.taxonomy == taxonomy)


def _metrics_for(taxonomy: str) -> tuple[str, ...]:
    bound = {binding.metric for binding in _bindings_of(taxonomy)}
    return tuple(metric for metric in METRIC_ORDER if metric in bound)


def _bindings_for(taxonomy: str, concept: str) -> tuple[ConceptBinding, ...]:
    return tuple(binding for binding in _bindings_of(taxonomy) if binding.concept == concept)


def _unit_skipped_counts(
    rows: Sequence[FactRow], taxonomy: str, report_currency: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.taxonomy != taxonomy:
            continue
        bindings = _bindings_for(taxonomy, row.concept)
        if not bindings:
            continue
        if any(_unit_matches(row.unit, binding, report_currency) for binding in bindings):
            continue
        counts[row.unit] = counts.get(row.unit, 0) + 1
    return counts


# --- filing index -----------------------------------------------------------


def _filing_index_draft(
    *,
    identity: ControlledIdentity,
    facts: _FactsDocument | None,
    submissions: _SubmissionsDocument | None,
    subject_id: int,
    captured_at: datetime,
) -> DatasetDraft | None:
    if submissions is None:
        return None
    report_currency = identity.report_currency
    taxonomy = (
        _choose_taxonomy(facts.rows, report_currency) if facts is not None and facts.rows else None
    )
    metrics = () if taxonomy is None else _metrics_for(taxonomy)
    listed = [ref for ref in submissions.filings.values() if ref.is_annual or ref.form == "8-K"]
    periods: dict[str, Any] = {}
    if facts is not None and facts.rows and taxonomy is not None:
        for period in _discover_periods(facts.rows):
            references = _period_references(
                facts.rows, submissions.filings, period, taxonomy, metrics
            )
            periods[period.end.isoformat()] = select_period_state(
                references, submissions.notices
            ).payload()
    payload: dict[str, Any] = {
        "facts": [],
        "cik": _identity_cik(identity),
        "entity_name": None if facts is None else facts.entity_name,
        "report_currency": report_currency,
        "filings": [
            ref.payload()
            for ref in sorted(listed, key=lambda ref: (ref.filing_date or date.min, ref.accession))
        ],
        "shards": list(submissions.merged_shards),
        "shards_missing": list(submissions.missing_shards),
        "periods": periods,
        "unreliable_notices": [notice.payload() for notice in submissions.notices],
        "filing_count": submissions.filing_count,
        "annual_filing_count": sum(1 for ref in listed if ref.is_annual),
    }
    return DatasetDraft(
        kind=DatasetKind.FILING_INDEX,
        subject_kind=SubjectKind.COMPANY,
        subject_id=subject_id,
        source=SOURCE_FILINGS,
        schema_version=SCHEMA_VERSION,
        adapter_version=ADAPTER_VERSION,
        captured_at=captured_at,
        payload=payload,
        period=None,
        raw_source_url=SUBMISSIONS_URL.format(cik=int(_identity_cik(identity))),
    )


# --- small helpers ----------------------------------------------------------


def _identity_cik(identity: ControlledIdentity) -> str:
    symbol = source_symbol(identity, SOURCE_FACTS)
    if symbol is None:
        raise ValueError(f"identity {identity.company_key!r} binds no {SOURCE_FACTS} symbol (CIK)")
    text = symbol.strip()
    if not text.isdigit():
        raise ValueError(f"SEC CIK must be decimal digits, got {symbol!r}")
    return f"{int(text):010d}"


def _document(raw: Sequence[RawPayload], name: str) -> RawPayload | None:
    for payload in raw:
        if payload.name == name or payload.name.endswith(f"/{name}"):
            return payload
    return None


def _shard_payloads(raw: Sequence[RawPayload], submissions: RawPayload) -> tuple[RawPayload, ...]:
    return tuple(
        payload
        for payload in raw
        if payload is not submissions and payload.name not in (FACTS_DOCUMENT, SUBMISSIONS_DOCUMENT)
    )


def _json_object(payload: RawPayload, fatal: list[str]) -> Mapping[str, Any] | None:
    text = payload.content.decode("utf-8", errors="replace").strip()
    if not text or text.startswith("<"):
        fatal.append(f"not_json: {payload.name}")
        return None
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        fatal.append(f"not_json: {payload.name}")
        return None
    if not isinstance(body, Mapping):
        fatal.append(f"unexpected_shape: {payload.name}")
        return None
    return body


def _cik_text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"{value:010d}"
    if isinstance(value, str) and value.strip().isdigit():
        return f"{int(value.strip()):010d}"
    return None


def _cik_matches(value: object, cik10: str) -> bool:
    return _cik_text(value) == cik10


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


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def _parse_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _decimal_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f")


def _items(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _text_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _iso_date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _iso_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()

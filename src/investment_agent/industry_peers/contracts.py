"""Industry & peer context: deterministic peer-comparison reference for one
target stock, built from immutable cohort rosters and annual fact snapshots.

WP1 fixes only the shared vocabulary and value shapes; the import, evidence,
calculation, service and job layers arrive with later work packages. The
numbers reuse the existing :class:`Quantity` shape (``PeerQuantity`` is the
same contract under this module's name), so a stored value is a decimal
string - never a JSON float - and an unknown value is null with an explicit
``unknown_reason``.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from investment_agent.evidence.contracts import Period
from investment_agent.investor.common import (
    Contract,
    DecimalValue,
    ObjectRef,
    Quantity,
    Text,
)

SCHEMA_VERSION = 1


class PeerContextStatus(StrEnum):
    """Overall peer-context state, fixed by the technical design (§页面与接口接入).

    ``ready``/``partial`` describe computed results; ``pending`` a queued or
    running collection; ``not_supported`` a stock outside the feature;
    ``missing`` no usable fact at all; ``conflict`` unresolved known conflicts;
    ``stale`` a known correction or update awaiting re-check; ``failed`` a
    collection failure (surfaced with the run error summary).
    """

    READY = "ready"
    PARTIAL = "partial"
    PENDING = "pending"
    NOT_SUPPORTED = "not_supported"
    MISSING = "missing"
    CONFLICT = "conflict"
    STALE = "stale"
    FAILED = "failed"


class PeerMetricStatus(StrEnum):
    """Per-metric state; never inferred from one overall success flag.

    ``comparable`` the target and enough peers are usable; ``insufficient_peers``
    fewer than the required valid peers (coverage counts and reasons are still
    saved); ``target_not_applicable`` the metric does not apply to the target
    (peer median may still be usable); ``missing`` no usable source fact;
    ``conflict`` unresolved conflicts excluded the input.
    """

    COMPARABLE = "comparable"
    TARGET_NOT_APPLICABLE = "target_not_applicable"
    INSUFFICIENT_PEERS = "insufficient_peers"
    MISSING = "missing"
    CONFLICT = "conflict"


PeerQuantity = Quantity
"""The numeric shape of every peer value: ``{value: decimal string | null,
unit, currency, unknown_reason}`` - identical to the evidence ``Quantity`` so
page, API and agent never see two shapes of one number. An alias, not a
subclass: the contract cannot drift."""


class PeerFieldVerification(StrEnum):
    """Check status of one saved annual field (WP3 fills it, WP1 fixes it).

    ``candidate`` has source value, times and semantics but no original
    confirmation; ``verified`` requires the trusted adapter to have read the
    actual original document and its checklist. Fabricated ``verified`` is
    rejected at the evidence layer (WP4).
    """

    CANDIDATE = "candidate"
    VERIFIED = "verified"


class PeerAnnualField(Contract):
    """Canonical JSON shape of one entry in ``PeerAnnualSnapshot.fields``.

    Data discipline (fixed here, consumed by WP3's importer):

    * ``value`` is a decimal string with the source's precision kept (never
      converted through float); ``None`` only together with ``missing_reason``
      - a missing value is never 0, and an original 0 is a value.
    * ``unit`` names the canonical unit (``CNY`` for amounts, ``fraction`` for
      the saved ROE ratio, ...); ``currency`` repeats the currency for
      currency amounts and stays null otherwise.
    * ``period`` is the real period with an explicit ``basis``: flows
      (revenue/profit/cash flow) cover Jan 1 - Dec 31 with
      ``period_to_date/annual``; stocks (parent equity, contract liabilities)
      are the Dec 31 point with ``point_in_time``; ROE is the disclosed
      full-year ratio.
    * ``source_field`` is the provider's original field name and ``raw_value``
      / ``raw_unit`` the value as the source stated it (ROE arrives in
      percent; the /100 conversion happens exactly once, at import).
    * ``acquired_at`` is the timezone-aware import time; the source's own
      research fetch time stays source metadata. ``disclosed_on`` /
      ``updated_on`` keep the source's disclosure/revision dates with their
      own precision handling.
    """

    metric: Text
    value: DecimalValue | None
    unit: Text
    currency: str | None = None
    period: Period
    source_field: str | None = None
    raw_value: str | None = None
    raw_unit: str | None = None
    source_version: Text | None = None
    disclosed_on: date | None = None
    updated_on: date | None = None
    acquired_at: datetime
    verification: PeerFieldVerification = PeerFieldVerification.CANDIDATE
    missing_reason: Text | None = None

    @model_validator(mode="after")
    def unknown_is_explicit(self) -> "PeerAnnualField":
        if (self.value is None) != (self.missing_reason is not None):
            raise ValueError("missing values require a reason; known values cannot have one")
        return self


BINDING_METRIC_KEYS: tuple[str, ...] = ("roe", "contract_revenue", "cfo_parent_profit")
"""The fixed metric keys of a peer-context binding.

Mirrors ``calculations.METRIC_KEYS`` (pinned equal by a unit test - this
contracts module must not import the calculation layer)."""


class PeerMetricBinding(Contract):
    """One metric's frozen reference inside a research peer-context binding.

    Same shapes as the batch mapping's ``MetricMappingEntry``: ``saved`` /
    ``reused`` carry the actual ``SavedCalculation`` reference, ``gap`` only
    its reason. The read side re-derives coverage and values from the mapped
    records; the binding freezes exactly *which* records a study may cite.
    """

    metric_key: str
    state: Literal["saved", "reused", "gap"]
    ref: ObjectRef | None = None
    gap_reason: Text | None = None


class PeerContextBinding(Contract):
    """Trusted peer-context binding frozen at research enqueue time (WP9).

    The structure the design fixes for the ``research_snapshot.industry_peer_context``
    column (and, from WP11, ``ExecutionGrant.peer_context``): the target
    ticker, the roster version, the batch holding the results, the report
    period, the binding-time cutoff and the three actual metric calculation
    references. The trusted application builds it from a read result; the old
    HTTP submit path cannot set it. The snapshot column stores it verbatim and
    it stays null for snapshots written before the feature (never backfilled
    with a newer result).

    ``cutoff`` is the research binding time: the service hides mappings and
    records submitted after it, so a later batch never enters an older study.
    """

    target: Text
    cohort_ref: ObjectRef
    batch_run_id: Text
    report_period: date
    cutoff: datetime
    metric_calculation_refs: tuple[PeerMetricBinding, ...]

    @model_validator(mode="after")
    def cohort_is_a_snapshot_ref(self) -> "PeerContextBinding":
        if self.cohort_ref.kind != "peer_cohort_snapshot":
            raise ValueError("cohort_ref must reference a peer_cohort_snapshot")
        return self

    @model_validator(mode="after")
    def covers_the_fixed_metrics(self) -> "PeerContextBinding":
        keys = [entry.metric_key for entry in self.metric_calculation_refs]
        if sorted(keys) != sorted(BINDING_METRIC_KEYS):
            raise ValueError(
                "a peer-context binding carries exactly the three fixed metric keys "
                f"{list(BINDING_METRIC_KEYS)}; got {keys}"
            )
        return self

"""/stocks/{ticker}/industry-peer-context response schemas (WP9).

The read-only HTTP form of the WP6 query service: one conversion layer from
:class:`~investment_agent.industry_peers.service.PeerContextResult` /
:class:`~investment_agent.industry_peers.service.PeerCompaniesResult`, so the
page (WP10) and later the agent (WP11) see the same values, states and
references the service read from the submitted batch mapping. Numeric values
stay ``Quantity`` shapes (decimal string or null with an ``unknown_reason``);
every time field serializes as ISO8601 with its UTC offset (the page renders
Asia/Shanghai, the API never ships a naive time).
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from investment_agent.industry_peers.contracts import PeerContextStatus
from investment_agent.industry_peers.service import (
    SERVICE_SCHEMA_VERSION,
    PeerCompanyDetail,
    PeerFieldEvidenceLink,
    PeerJobHint,
    PeerMetricContext,
    PeerTargetIdentity,
)
from investment_agent.investor.common import ObjectRef


class PeerCohortOut(BaseModel):
    """The roster version the returned batch was built against."""

    ref: ObjectRef | None = None
    content_hash: str | None = None


class PeerMetricRefOut(BaseModel):
    """One entry of the batch's frozen ``metric_calculation_refs``.

    ``saved`` / ``reused`` carry the actual ``SavedCalculation`` reference;
    ``gap`` carries only its reason - exactly as the mapping artifact froze
    them, so a history read can never splice a "latest" record in.
    """

    metric_key: str
    state: Literal["saved", "reused", "gap"]
    ref: ObjectRef | None = None
    gap_reason: str | None = None


class IndustryPeerContextOut(BaseModel):
    """The whole peer-context answer for one target (current or by batch).

    ``target`` is null only in the feature-disabled response (the minimal gate
    returns the not-yet-covered business state without touching the database).
    """

    schema_version: int = SERVICE_SCHEMA_VERSION
    status: PeerContextStatus
    target: PeerTargetIdentity | None = None
    report_period: date | None = None
    """The period of the returned batch (the old year on an old reference)."""
    current_report_period: date | None = None
    """The locally determined current year, when it could be determined."""
    old_reference: bool = False
    cohort: PeerCohortOut = Field(default_factory=PeerCohortOut)
    batch_run_id: str | None = None
    generation: int | None = None
    provider: str | None = None
    adapter_version: str | None = None
    batch_status: Literal["success", "partial"] | None = None
    metric_calculation_refs: list[PeerMetricRefOut] = Field(default_factory=list)
    data_as_of: datetime
    inputs_acquired_earliest: datetime | None = None
    """Earliest acquisition time over the batch's frozen inputs (mixed-date
    batches show both ends instead of one "latest" time)."""
    inputs_acquired_latest: datetime | None = None
    last_checked_at: datetime | None = None
    submitted_at: datetime | None = None
    metrics: tuple[PeerMetricContext, ...] = ()
    warnings: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    stale_reasons: tuple[str, ...] = ()
    job_hint: PeerJobHint | None = None


class PeerCompaniesOut(BaseModel):
    """The fixed-version peer detail of one target inside one batch."""

    schema_version: int = SERVICE_SCHEMA_VERSION
    status: PeerContextStatus
    target: PeerTargetIdentity | None = None
    batch_run_id: str | None = None
    report_period: date | None = None
    cohort_ref: ObjectRef | None = None
    companies: tuple[PeerCompanyDetail, ...] = ()
    field_evidence: tuple[PeerFieldEvidenceLink, ...] = ()
    warnings: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    data_as_of: datetime

"""Trusted peer-context binding lifecycle and the read-only agent tool (WP11).

The authorization discipline is fixed by the technical design (§Agent 工具与
引用校验):

* **One authorization source.** ``ExecutionGrant.peer_context`` is frozen by the
  trusted application at task admission (through
  :func:`investment_agent.industry_peers.service.peer_context_binding_from` over
  a read of the WP6 service) and is the only peer-read permission. A calculation
  UUID in an old conversation, an earlier tool return or any model reference can
  never restore it: every call re-reads this task's saved grant.
* **Lifecycle.** A new message re-binds from the current read; the same task's
  retry keeps the original binding (the immutable grant row, enforced in
  :func:`investment_agent.agent.execution.admit`); a subtask continuation
  inherits the parent binding only when target, cutoff and method are consistent
  and the bound source is still available (:func:`inherit_peer_context`).
* **Server-owned read.** The model supplies only the target ticker; the batch,
  year, roster, metric references and cutoff all come from the binding. The tool
  reads the exact bound batch at the binding cutoff through the WP6 service, so
  a mapping or record submitted/created after the cutoff never enters an old
  task.
* **Reduced return.** The normal return carries the three metrics' available
  values, units, report period, sample counts, check coverage, limitations and
  the ``calculation.results.*`` references - never the raw source responses,
  the peer identity table or original documents; those belong to the page.
  The outer :class:`ToolResult` status stays within the existing enumeration
  (no ``partial``); the concrete coverage state lives in ``data``. Without a
  binding the tool answers ``missing`` with the recorded gap and, when a
  background collection is queued/running, only a run reference - never a
  ``pending``/``dependency_id`` answer that would trigger the deferred wait.
"""

from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.contracts import Task
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant
from investment_agent.agent.tools import ToolRegistration
from investment_agent.industry_peers.contracts import (
    PeerContextBinding,
    PeerContextStatus,
    PeerQuantity,
)
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.storage.models.agent import ExecutionGrantRow, ToolAction

OUTER_STATUS: dict[PeerContextStatus, Literal["ok", "missing", "conflict", "stale", "error"]] = {
    PeerContextStatus.READY: "ok",
    PeerContextStatus.PARTIAL: "ok",
    PeerContextStatus.CONFLICT: "conflict",
    PeerContextStatus.STALE: "stale",
    PeerContextStatus.NOT_SUPPORTED: "missing",
    PeerContextStatus.MISSING: "missing",
    PeerContextStatus.PENDING: "missing",
    PeerContextStatus.FAILED: "missing",
}
"""The service's 8-state coverage mapped onto the existing ToolResult status
enumeration; ``partial`` coverage stays a plain ``ok`` answer whose details
live in ``data`` - no new enum value."""

BOUND_COVERAGE = (
    "the bound batch's frozen peer statistics read at the binding cutoff; values, "
    "counts, check coverage and calculation references only - no raw source "
    "responses, peer identity table or original documents"
)
UNBOUND_COVERAGE = (
    "no peer-context result is bound to this task, so no values are readable; "
    "the coverage notes describe the current gap only"
)
UNBOUND_GAP = (
    "no peer-context result was bound to this task at admission; a batch "
    "submitted later never enters an older task - a new message binds anew"
)


class PeerAgentMetric(Contract):
    """One metric's agent-readable state, projected from the service read.

    Deliberately narrower than ``PeerMetricContext``: the excluded-company
    identity table and the per-field evidence links stay page-only (WP12's
    reference validation re-derives them from the bound calculation refs).
    """

    metric_key: str
    status: str
    state: Literal["saved", "reused", "gap"]
    target_value: PeerQuantity | None = None
    peer_median: PeerQuantity | None = None
    ascending_position: PeerQuantity | None = None
    valid_peer_count: int = 0
    candidate_peer_count: int = 0
    verification_counts: dict[str, int] = {}
    calculation_ref: ObjectRef | None = None
    result_refs: dict[str, ObjectRef] = {}
    reason: str | None = None
    gap_reason: str | None = None
    quality_notes: tuple[str, ...] = ()
    cites_no_evidence: bool = False
    cutoff_excluded: bool = False


class PeerAgentContext(Contract):
    """The reduced peer-context answer the model is allowed to see."""

    coverage: PeerContextStatus
    target_ticker: str
    report_period: date | None = None
    old_reference: bool = False
    metrics: tuple[PeerAgentMetric, ...] = ()
    warnings: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    stale_reasons: tuple[str, ...] = ()
    run_hint: tuple[str, ...] = ()
    """Run references of a queued/running background collection - a lead to
    relay, never a pending status or dependency id."""


# --- trusted admission binding -------------------------------------------------


async def admission_peer_binding(
    session: AsyncSession,
    *,
    settings: Any,
    listing: ObjectRef,
    cutoff: datetime,
) -> PeerContextBinding | None:
    """The trusted binding for one in-scope listing at message admission.

    Called by the message-admission application (never by the model) after it
    decided this admission carries exactly one security. The read follows the
    message's own research cutoff, so a historical message binds only what that
    cutoff could know; without a submitted result the binding stays ``None`` and
    the gap is answered by the tool - it is never filled from a later batch.
    """

    from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
    from investment_agent.industry_peers.service import (
        PeerTargetLookupError,
        get_industry_peer_context,
        peer_context_binding_from,
    )
    from investment_agent.storage.models.reference import Listing

    if not settings.industry_peer_context_enabled or cutoff.tzinfo is None:
        return None
    row = await session.get(Listing, int(listing.id))
    if row is None:
        return None
    ticker = format_listing_ticker(ticker=row.ticker, market_code=row.market_code)
    if parse_a_share_ticker(ticker) is None:
        return None  # the feature covers canonical A-share targets only
    try:
        result = await get_industry_peer_context(
            session, ticker=ticker, cutoff=cutoff, owner_id="local_owner"
        )
    except (PeerTargetLookupError, ValueError):
        return None
    return peer_context_binding_from(result)


async def _listing_id_of(session: AsyncSession, ticker: str) -> int | None:
    from investment_agent.storage.repositories.listing import ListingRepository

    listing = await ListingRepository(session).get_a_share_listing(ticker.strip().upper())
    return listing.id if listing is not None else None


async def _binding_source_available(
    session: AsyncSession, binding: PeerContextBinding, data_dir: Path
) -> bool:
    """The bound roster version and submitted batch mapping still exist."""

    from investment_agent.industry_peers.artifacts import (
        MappingArtifactError,
        mapping_artifact_path,
        read_batch_mapping,
    )
    from investment_agent.storage.models.peer import PeerCohortSnapshot

    if await session.get(PeerCohortSnapshot, str(binding.cohort_ref.id)) is None:
        return False
    try:
        read_batch_mapping(mapping_artifact_path(data_dir, binding.batch_run_id))
    except MappingArtifactError:
        return False
    return True


async def inherit_peer_context(
    session: AsyncSession,
    grant: ExecutionGrant,
    *,
    task: Task,
    parent_task_id: UUID | None,
    data_dir: Path,
) -> ExecutionGrant:
    """Finalize the admission binding before the grant row is written.

    A caller-supplied fresh binding (the new-message re-bind) is kept only when
    its target is exactly an in-scope listing; anything else is stripped. A
    subtask continuation without its own binding inherits the parent's only when
    the target, the cutoff and the method are consistent and the bound source is
    still available; every failed condition degrades to no binding, never to a
    newer batch.
    """

    scope_listings = {int(r.id) for r in grant.scope_refs if r.kind == "listing"}
    if grant.peer_context is not None:
        bound = await _listing_id_of(session, grant.peer_context.target)
        if bound is None or bound not in scope_listings:
            grant = grant.model_copy(update={"peer_context": None})
        return grant
    if parent_task_id is None:
        return grant
    from investment_agent.investor.methods import task_method

    row = await session.get(ExecutionGrantRow, str(parent_task_id))
    if row is None:
        return grant
    parent = ExecutionGrant.model_validate(row.payload)
    binding = parent.peer_context
    if binding is None or parent.research_kind != grant.research_kind:
        return grant
    bound = await _listing_id_of(session, binding.target)
    if bound is None or bound not in scope_listings:
        return grant  # target differs: the continuation rebinds nothing
    if (await task_method(session, parent_task_id)).method_ref != (
        await task_method(session, task.id)
    ).method_ref:
        return grant  # method differs: not the same research line
    if binding.cutoff > task.binding.as_of:
        return grant  # the parent binding knows more than this cutoff allows
    if not await _binding_source_available(session, binding, data_dir):
        return grant
    return grant.model_copy(update={"peer_context": binding})


# --- the read-only tool --------------------------------------------------------


class PeerContextParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    """The exact target ticker; every control field (batch, year, roster,
    cutoff, metric references) is server-bound and stays out of this schema."""


async def peer_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
    """The target listing must be an exact in-scope directory identity."""

    assert isinstance(business, PeerContextParameters)
    from investment_agent.storage.repositories.listing import ListingRepository

    listing = await ListingRepository(session).get_a_share_listing(business.ticker.strip().upper())
    if listing is None:
        raise ExecutionDeniedError("listing_unavailable")
    return (ObjectRef(kind="listing", id=listing.id, revision=1),)


def _project(result: Any) -> PeerAgentContext:
    """Reduce the service read to the model-visible shape (identity table and
    raw field evidence excluded by construction)."""

    from investment_agent.industry_peers.service import PeerContextResult

    assert isinstance(result, PeerContextResult)
    return PeerAgentContext(
        coverage=result.status,
        target_ticker=result.target.ticker,
        report_period=result.report_period,
        old_reference=result.old_reference,
        metrics=tuple(
            PeerAgentMetric(
                metric_key=metric.metric_key,
                status=metric.status.value,
                state=metric.state,
                target_value=metric.target_value,
                peer_median=metric.peer_median,
                ascending_position=metric.ascending_position,
                valid_peer_count=metric.valid_peer_count,
                candidate_peer_count=metric.candidate_peer_count,
                verification_counts=dict(metric.verification_counts),
                calculation_ref=metric.calculation_ref,
                result_refs=dict(metric.result_refs),
                reason=metric.reason,
                gap_reason=metric.gap_reason,
                quality_notes=metric.quality_notes,
                cites_no_evidence=metric.cites_no_evidence,
                cutoff_excluded=metric.cutoff_excluded,
            )
            for metric in result.metrics
        ),
        warnings=result.warnings,
        gaps=result.gaps,
        stale_reasons=result.stale_reasons,
    )


def _citable_refs(payload: PeerAgentContext) -> tuple[ObjectRef, ...]:
    refs: list[ObjectRef] = []
    for metric in payload.metrics:
        if metric.calculation_ref is not None:
            refs.append(metric.calculation_ref)
        refs.extend(ref for ref in metric.result_refs.values() if ref not in refs)
    return tuple(dict.fromkeys(refs))


async def _active_run_hints(session: AsyncSession, result: Any) -> tuple[str, ...]:
    """Run ids of the scope's queued/running collections (WP8 lookup, WP11 use).

    The service never invents job state at read time; only this trusted adapter
    fills the hint, and only as a plain reference - never a pending status or
    dependency id that would turn the answer into a deferred wait.
    """

    from investment_agent.industry_peers.artifacts import (
        DEFAULT_ADAPTER_VERSION,
        DEFAULT_PROVIDER,
    )
    from investment_agent.industry_peers.jobs import find_active_scope_run
    from investment_agent.industry_peers.service import PeerContextResult
    from investment_agent.storage.models.peer import PeerCohortSnapshot

    assert isinstance(result, PeerContextResult)
    if result.cohort_ref is None or result.current_report_period is None:
        return ()
    cohort = await session.get(PeerCohortSnapshot, str(result.cohort_ref.id))
    if cohort is None:
        return ()
    run = await find_active_scope_run(
        session,
        cohort=cohort,
        report_period=result.current_report_period,
        provider=DEFAULT_PROVIDER,
        adapter_version=DEFAULT_ADAPTER_VERSION,
    )
    return (str(run.id),) if run is not None else ()


async def _unbound_read(session: AsyncSession, grant: ExecutionGrant, ticker: str) -> dict[str, Any]:
    """No binding: the recorded gap plus the current coverage notes, no values.

    The current read supplies only status texts and gaps; metrics, counts and
    references are never projected, so a batch submitted after admission cannot
    leak a single number into an unbound task.
    """

    from investment_agent.evidence.contracts import ToolResult
    from investment_agent.industry_peers.service import (
        PeerTargetLookupError,
        get_industry_peer_context,
    )

    try:
        result = await get_industry_peer_context(session, ticker=ticker, owner_id=grant.owner)
    except (PeerTargetLookupError, ValueError) as exc:
        raise ExecutionDeniedError("listing_unavailable") from exc
    hints = await _active_run_hints(session, result)
    payload = PeerAgentContext(
        coverage=result.status,
        target_ticker=result.target.ticker,
        gaps=(UNBOUND_GAP, *result.gaps),
        warnings=result.warnings,
        run_hint=hints,
    )
    return ToolResult(
        status="missing",
        data=payload,
        refs=(),
        as_of=result.data_as_of,
        coverage=UNBOUND_COVERAGE,
    ).model_dump(mode="json")


async def read_peer_context(
    session: AsyncSession, operation: UUID, business: BaseModel
) -> dict[str, Any]:
    """Read the bound peer context for the exact authorized target."""

    assert isinstance(business, PeerContextParameters)
    action = await session.get(ToolAction, str(operation))
    assert action is not None
    row = await session.get(ExecutionGrantRow, action.task_id)
    if row is None:
        raise ExecutionDeniedError("trusted_admission_required")
    grant = ExecutionGrant.model_validate(row.payload)
    ticker = business.ticker.strip().upper()
    binding = grant.peer_context
    if binding is None:
        return await _unbound_read(session, grant, ticker)
    if ticker != binding.target.strip().upper():
        raise ExecutionDeniedError("peer_context_target_not_bound")
    return await _bound_read(session, grant, binding, ticker)


async def _bound_read(
    session: AsyncSession, grant: ExecutionGrant, binding: PeerContextBinding, ticker: str
) -> dict[str, Any]:
    from investment_agent.evidence.contracts import ToolResult
    from investment_agent.industry_peers.service import (
        PeerTargetLookupError,
        get_industry_peer_context,
    )

    try:
        result = await get_industry_peer_context(
            session,
            ticker=ticker,
            batch_run_id=binding.batch_run_id,
            cutoff=binding.cutoff,
            owner_id=grant.owner,
        )
    except (PeerTargetLookupError, ValueError) as exc:
        # The binding is trusted and was built from this exact batch; a read
        # that can no longer be honored fails closed instead of substituting.
        raise ExecutionDeniedError("peer_context_binding_unavailable") from exc
    payload = _project(result)
    return ToolResult(
        status=OUTER_STATUS[result.status],
        data=payload,
        refs=_citable_refs(payload),
        as_of=result.data_as_of,
        coverage=BOUND_COVERAGE,
    ).model_dump(mode="json")


def registrations() -> tuple[ToolRegistration, ...]:
    return (
        ToolRegistration(
            "get_industry_peer_context",
            PeerContextParameters,
            peer_scope,
            database=read_peer_context,
            description=(
                "Read the industry-peer reference statistics bound to this task's trusted admission "
                "(roe, contract_revenue, cfo_parent_profit) for the exact authorized target ticker. "
                "The server reads the frozen batch at the binding cutoff and returns per-metric "
                "values, units, report period, sample counts, verification coverage, limitations and "
                "calculation.results.* references - never raw source responses, the peer identity "
                "table or original documents. Missing means this task bound no peer context; a new "
                "message may bind one. Peer statistics are reference context, not saved evidence."
            ),
        ),
    )


__all__ = [
    "PeerAgentContext",
    "PeerAgentMetric",
    "PeerContextParameters",
    "admission_peer_binding",
    "inherit_peer_context",
    "peer_scope",
    "read_peer_context",
    "registrations",
]

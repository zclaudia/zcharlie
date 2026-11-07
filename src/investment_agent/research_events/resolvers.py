"""Read trusted source rows in the caller's session; never infer owner from payload.

Legacy global listing/round/change tables are supported only for the existing
single-owner deployment. UUID-owned sources additionally require persistent
owner matches. Future domain kinds and evidence corrections are not installed
as publishers by this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord
from investment_agent.investor.common import Contract, ObjectRef, OwnerId, Revision, utc_timestamp
from investment_agent.investor.contracts import (
    Case,
    CompanyRecommendation,
    RecommendationEvent,
    ThesisRevision,
)
from investment_agent.investor.foundations import InvestorError
from investment_agent.research_events import canonical
from investment_agent.research_events.contracts import (
    AccountSubject,
    CaseControlPayload,
    EventSourceRef,
    ListingSubject,
    RecommendationPayload,
    ResearchChangePayload,
    ReviewCyclePayload,
    ReviewPanelPayload,
    RoutingPayload,
    Subject,
)
from investment_agent.storage.models.analysis import (
    PipelineRun,
    ResearchChangeSet,
    ResearchSnapshot,
)
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.investor import (
    CaseRevision,
    InvestorCase,
    UserObject,
    UserObjectRevision,
)
from investment_agent.storage.models.monitoring import ReviewCycle
from investment_agent.storage.models.publication import PublicationArtifact, PublicationEvent
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.review_panel import ReviewComparisonRecord, ReviewRoundRecord

if TYPE_CHECKING:
    from investment_agent.research_events.registry import EventRegistry


class ResolvedSource(Contract):
    owner_id: OwnerId
    subject: Subject
    source_ref: EventSourceRef
    source_revision: Revision
    occurred_at: datetime
    ordering_key: Annotated[str, Field(min_length=1, max_length=250)]


def event_subject(payload: RoutingPayload) -> Subject:
    # RoutingPayload is intentionally minimal; each closed event DTO declares
    # its own listing/account subject. Never infer it from arbitrary JSON.
    value = payload.__dict__.get("subject")
    if not isinstance(value, (ListingSubject, AccountSubject)):
        raise InvestorError("invalid_reference", "Event payload has no typed subject")
    return value


def _require(test: bool, message: str = "Event source is unavailable or inconsistent") -> None:
    if not test:
        raise InvestorError("invalid_reference", message)


def _owner(owner: str) -> None:
    if owner != "local_owner":
        raise InvestorError("not_found", "Source is not accessible")


def _column_time(value: datetime | None) -> datetime:
    _require(value is not None, "Source occurrence time is not recorded")
    assert value is not None
    # SQLite drops tzinfo on DateTime columns; all persisted column times in
    # this application are UTC. JSON times still require an explicit offset.
    return utc_timestamp(value.replace(tzinfo=UTC) if value.tzinfo is None else value)


def _json_time(value: object) -> datetime:
    try:
        return utc_timestamp(TypeAdapter(datetime).validate_python(value))
    except (ValueError, ValidationError) as exc:
        raise InvestorError("invalid_reference", "Source timestamp is invalid") from exc


async def _listing(session: AsyncSession, listing_id: int) -> Listing:
    row = await session.get(Listing, listing_id, populate_existing=True)
    _require(row is not None, "Source listing does not exist")
    assert row is not None
    return row


async def read_case_ref(session: AsyncSession, owner: OwnerId, ref: ObjectRef) -> Case:
    _owner(owner)
    _require(ref.kind == "case" and ref.field_path is None)
    head = await session.get(InvestorCase, str(ref.id), populate_existing=True)
    if head is None or head.owner_id != owner:
        raise InvestorError("not_found", "Source is not accessible")
    row = await session.get(CaseRevision, (str(ref.id), ref.revision), populate_existing=True)
    _require(row is not None and head.revision >= ref.revision)
    assert row is not None
    case = Case.model_validate(row.payload)
    _require(case.id == ref.id and case.revision == ref.revision and case.user_id == owner
             and case.listing_ref.kind == "listing" and case.listing_ref.id == head.listing_id)
    await _listing(session, head.listing_id)
    return case


async def read_review_comparison(session: AsyncSession, owner: OwnerId, comparison_id: int,
                                 *, round_id: int, algorithm_version: str) -> ReviewComparisonRecord:
    _owner(owner)
    _require(type(comparison_id) is int and comparison_id > 0 and type(round_id) is int and round_id > 0)
    row = await session.get(ReviewComparisonRecord, comparison_id, populate_existing=True)
    _require(row is not None and row.round_id == round_id and row.algorithm_version == algorithm_version)
    assert row is not None
    round_row = await session.get(ReviewRoundRecord, round_id, populate_existing=True)
    _require(round_row is not None)
    assert round_row is not None
    await _listing(session, round_row.listing_id)
    return row


async def read_evidence_ref(session: AsyncSession, owner: OwnerId, ref: ObjectRef) -> EvidenceRecord:
    _owner(owner)
    _require(ref.kind == "evidence" and ref.field_path is None)
    row = await session.get(SavedEvidence, (str(ref.id), ref.revision), populate_existing=True)
    if row is None or row.owner_id != owner:
        raise InvestorError("not_found", "Source is not accessible")
    record = EvidenceRecord.model_validate(row.payload)
    _require(record.id == ref.id and record.revision == ref.revision)
    _require(record.subject_ref.kind == "listing" and type(record.subject_ref.id) is int)
    await _listing(session, int(record.subject_ref.id))
    # This read helper does not upgrade candidates to verified corrections.
    return record


async def read_calculation_ref(session: AsyncSession, owner: OwnerId, ref: ObjectRef) -> CalculationRecord:
    _owner(owner)
    _require(ref.kind == "calculation" and ref.field_path is None)
    row = await session.get(SavedCalculation, (str(ref.id), ref.revision), populate_existing=True)
    if row is None or row.owner_id != owner:
        raise InvestorError("not_found", "Source is not accessible")
    record = CalculationRecord.model_validate(row.payload)
    _require(record.id == ref.id and record.revision == ref.revision)
    # No method is granted event publication or financial acceptance by reading.
    return record


def _result(owner: OwnerId, ref: EventSourceRef, subject: ListingSubject,
            at: datetime, chain: str) -> ResolvedSource:
    return ResolvedSource(owner_id=owner, subject=subject, source_ref=ref,
                          source_revision=ref.revision, occurred_at=at,
                          ordering_key=f"{owner}:{chain}")


async def resolve_review_round(session: AsyncSession, owner: OwnerId, ref: EventSourceRef,
                               revision: int, payload: RoutingPayload) -> ResolvedSource:
    _owner(owner)
    _require(ref.kind == "review_round" and revision == ref.revision == 1 and isinstance(payload, ReviewPanelPayload))
    assert isinstance(payload, ReviewPanelPayload) and isinstance(ref.id, int)
    row = await session.get(ReviewRoundRecord, ref.id, populate_existing=True)
    _require(row is not None and row.status in {"completed", "failed"})
    assert row is not None
    await _listing(session, row.listing_id)
    comparison = await read_review_comparison(session, owner, payload.comparison_ref.id,
                                               round_id=row.id, algorithm_version=payload.algorithm_version)
    reasons = comparison.result_json.get("notify_reasons")
    _require(isinstance(reasons, list) and all(isinstance(value, str) for value in reasons))
    assert isinstance(reasons, list)
    _require(canonical.canonical_json(sorted(set(payload.notify_reasons))) ==
             canonical.canonical_json(sorted(set(reasons))))
    return _result(owner, ref, ListingSubject(kind="listing", listing_id=row.listing_id),
                   _column_time(row.completed_at), f"round:{row.id}")


async def resolve_research_change(session: AsyncSession, owner: OwnerId, ref: EventSourceRef,
                                  revision: int, payload: RoutingPayload) -> ResolvedSource:
    _owner(owner)
    _require(ref.kind == "research_change_set" and revision == ref.revision == 1 and isinstance(payload, ResearchChangePayload))
    assert isinstance(payload, ResearchChangePayload) and isinstance(ref.id, int)
    change = await session.get(ResearchChangeSet, ref.id, populate_existing=True)
    _require(change is not None)
    assert change is not None
    snapshot = await session.get(ResearchSnapshot, change.current_snapshot_id, populate_existing=True)
    _require(snapshot is not None)
    assert snapshot is not None
    run = await session.get(PipelineRun, snapshot.pipeline_run_id, populate_existing=True)
    _require(run is not None)
    assert run is not None
    listing = await _listing(session, payload.subject.listing_id)
    identity = parse_a_share_ticker(format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code))
    _require(identity is not None)
    assert identity is not None
    _require(snapshot.symbol == identity[0] and snapshot.market == identity[1].suffix)
    _require(run.listing_id is None or run.listing_id == listing.id)
    _require(payload.run_ref.id == run.id and payload.run_ref.revision == 1 and payload.run_ref.field_path is None
             and payload.current_snapshot_id == snapshot.id and payload.previous_snapshot_id == change.previous_snapshot_id
             and payload.baseline == change.baseline and payload.has_meaningful_changes == change.has_meaningful_changes
             and payload.source_changed == change.diff_json.get("source_changed", False))
    if change.previous_snapshot_id is not None:
        previous = await session.get(ResearchSnapshot, change.previous_snapshot_id, populate_existing=True)
        _require(previous is not None and previous.symbol == snapshot.symbol and previous.market == snapshot.market)
    return _result(owner, ref, ListingSubject(kind="listing", listing_id=listing.id),
                   _column_time(change.created_at), f"listing:{listing.id}:change:{change.id}")


async def resolve_case_control(session: AsyncSession, owner: OwnerId, ref: EventSourceRef,
                               revision: int, payload: RoutingPayload) -> ResolvedSource:
    _owner(owner)
    _require(ref.kind == "case" and ref.revision == revision and isinstance(payload, CaseControlPayload))
    assert isinstance(payload, CaseControlPayload)
    case = await read_case_ref(session, owner, payload.case_ref)
    _require(case.id == ref.id and case.revision == revision and case.episode == payload.case_episode)
    origin = case.origin_ref
    _require(origin is not None and origin.kind == "instruction" and origin.field_path is None)
    assert origin is not None
    instruction = await session.get(UserObject, str(origin.id), populate_existing=True)
    if instruction is None or instruction.owner_id != owner or instruction.kind != "instruction":
        raise InvestorError("not_found", "Source is not accessible")
    saved = await session.get(UserObjectRevision, (str(origin.id), origin.revision), populate_existing=True)
    _require(saved is not None and instruction.revision >= origin.revision)
    assert saved is not None
    parameters = saved.payload.get("parameters", {})
    _require(isinstance(parameters, dict) and parameters.get("operation") == "case_action"
             and parameters.get("action") == payload.action)
    expected_state = {
        "archive": case.status.value == "archived" and case.user_archived,
        "reopen": case.status.value == "researching" and not case.user_archived,
        "pause": case.background_paused, "resume": not case.background_paused,
        "mute": case.muted, "unmute": not case.muted,
    }
    _require(expected_state[payload.action], "Control instruction disagrees with saved case state")
    prior = ObjectRef.model_validate(parameters.get("case_ref"))
    _require(prior.kind == "case" and prior.id == case.id and prior.revision + 1 == revision)
    _require(saved.payload.get("id") == str(origin.id) and saved.payload.get("revision") == origin.revision)
    return _result(owner, ref, ListingSubject(kind="listing", listing_id=int(case.listing_ref.id)),
                   _json_time(saved.payload.get("created_at")), f"case:{case.id}:episode:{case.episode}")


async def resolve_review_cycle(session: AsyncSession, owner: OwnerId, ref: EventSourceRef,
                               revision: int, payload: RoutingPayload) -> ResolvedSource:
    _owner(owner)
    _require(ref.kind == "review_cycle" and isinstance(payload, ReviewCyclePayload))
    assert isinstance(payload, ReviewCyclePayload)
    row = await session.get(ReviewCycle, str(ref.id), populate_existing=True)
    _require(row is not None)
    assert row is not None
    case = await read_case_ref(session, owner, payload.case_ref)
    _require(row.case_id == str(case.id))
    # IT owns the durable version column and historical phase capture. Until
    # that adapter exists, a mutable JSON hash is never used as a fake revision.
    raise InvestorError("dependency_unavailable", "Review cycle event revision adapter is not installed")


async def resolve_recommendation_event(session: AsyncSession, owner: OwnerId, ref: EventSourceRef,
                                      revision: int, payload: RoutingPayload) -> ResolvedSource:
    _owner(owner)
    _require(ref.kind == "recommendation_event" and revision == ref.revision == 1 and isinstance(payload, RecommendationPayload))
    assert isinstance(payload, RecommendationPayload)
    rows = (await session.scalars(select(PublicationEvent).where(
        PublicationEvent.owner_id == owner, PublicationEvent.payload["id"].as_string() == str(ref.id)).limit(2).execution_options(populate_existing=True))).all()
    _require(len(rows) == 1, "Recommendation event identity is missing or ambiguous")
    row = rows[0]
    event = RecommendationEvent.model_validate(row.payload)
    _require(event.id == ref.id and event.revision == revision and event.recommendation_ref == payload.recommendation_ref
             and row.recommendation_id == str(payload.recommendation_ref.id) and event.type == payload.validity
             and event.replacement_ref == payload.replacement_ref and event.review_ref == payload.review_ref)
    _require(canonical.canonical_json(sorted({canonical.canonical_json(value) for value in event.checked_dependency_refs})) ==
             canonical.canonical_json(sorted({canonical.canonical_json(value) for value in payload.exact_source_refs})))
    recommendation = await session.get(PublicationArtifact, (str(event.recommendation_ref.id), event.recommendation_ref.revision), populate_existing=True)
    if recommendation is None or recommendation.owner_id != owner or recommendation.kind != "recommendation":
        raise InvestorError("not_found", "Source is not accessible")
    published = CompanyRecommendation.model_validate(recommendation.payload)
    _require(published.id == event.recommendation_ref.id and published.revision == event.recommendation_ref.revision
             and len(published.case_ids) == 1 and str(published.case_ids[0]) == recommendation.case_id)
    # The real publisher freezes its exact thesis rows, whose case_ref carries
    # the material/episode version. RecommendationEvent has no independent
    # "current case" snapshot. Lifecycle changes remain on the published chain.
    published_cases: dict[str, ObjectRef] = {}
    for thesis_ref in published.thesis_revision_refs:
        _require(thesis_ref.kind == "thesis" and thesis_ref.field_path is None)
        saved_thesis = await session.get(PublicationArtifact, (str(thesis_ref.id), thesis_ref.revision), populate_existing=True)
        _require(saved_thesis is not None and saved_thesis.owner_id == owner and saved_thesis.kind == "thesis"
                 and saved_thesis.case_id == recommendation.case_id)
        assert saved_thesis is not None
        thesis = ThesisRevision.model_validate(saved_thesis.payload)
        _require(thesis.id == thesis_ref.id and thesis.revision == thesis_ref.revision
                 and thesis.case_ref.kind == "case" and str(thesis.case_ref.id) == recommendation.case_id)
        published_cases[canonical.canonical_json(thesis.case_ref)] = thesis.case_ref
    _require(len(published_cases) == 1, "Published case version is missing or ambiguous")
    published_case_ref = next(iter(published_cases.values()))
    _require(payload.case_ref == published_case_ref, "Event case reference differs from the published case version")
    case = await read_case_ref(session, owner, published_case_ref)
    for dependent, kind in ((payload.replacement_ref, "recommendation"), (payload.review_ref, "review")):
        if dependent is not None:
            saved = await session.get(PublicationArtifact, (str(dependent.id), dependent.revision), populate_existing=True)
            _require(saved is not None and saved.owner_id == owner and saved.kind == kind and saved.case_id == str(case.id))
    return _result(owner, ref, ListingSubject(kind="listing", listing_id=int(case.listing_ref.id)),
                   event.created_at, f"case:{case.id}:episode:{case.episode}")


def install_builtin_sources(registry: EventRegistry) -> None:
    registry.register_source("review_round", resolve_review_round, allowed_event_types=("review_panel.finalized",))
    registry.register_source("research_change_set", resolve_research_change, allowed_event_types=("research_change.recorded",))
    registry.register_source("case", resolve_case_control, allowed_event_types=("case.control_changed",))
    registry.register_source("recommendation_event", resolve_recommendation_event, allowed_event_types=("recommendation.validity_changed",))
    # IT replaces this deliberately unavailable adapter at its explicit
    # composition boundary; it cannot publish before durable cycle revisions.
    # Not registering it permits that task to install its completed resolver.


async def resolve_source(session: AsyncSession, *, owner_id: OwnerId, event_type: str,
                         source_ref: EventSourceRef, source_revision: int,
                         payload: RoutingPayload, registry: EventRegistry | None = None) -> ResolvedSource:
    """Frozen public resolver protocol; shape support alone never grants access."""
    from investment_agent.research_events.contracts import EVENT_CONTRACTS, SOURCE_REF_ADAPTER
    from investment_agent.research_events.registry import get_default_registry

    _owner(owner_id)
    selected = registry if registry is not None else get_default_registry()
    try:
        source = SOURCE_REF_ADAPTER.validate_python(source_ref)
        _require(event_type in EVENT_CONTRACTS and type(source_revision) is int
                 and source.revision == source_revision)
        expected = EVENT_CONTRACTS[event_type][1]
        _require(type(payload) is expected)
        registration = selected.source(source.kind, event_type)
        resolved = await registration.resolver(session, owner_id, source, source_revision, payload)
        _require(resolved.owner_id == owner_id and resolved.source_ref == source
                 and resolved.source_revision == source_revision
                 and resolved.subject == event_subject(payload))
        return resolved
    except ValidationError as exc:
        raise InvestorError("invalid_reference", "Persisted source contract is invalid") from exc

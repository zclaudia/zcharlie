"""Bounded publish-side coverage rechecks shared by reads and publication.

One predicate compares a published coverage manifest with the currently
matching rule set, rule/event time boundaries, and event visibility. The
enumerations run inside the caller's transaction through the storage
repositories, so reads of published opinions reach this only via the
storage repository and never import the supplemental package directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import OwnerId
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.contracts import (
    ResearchRequirementDefinition,
    RuleApplication,
    RuleCoverageManifest,
)

SCAN_LIMIT = 2000


def _application_limits(application: RuleApplication, at: datetime) -> list[str]:
    """P2 application_limits comparison, keyed by rule/event identity.

    Expiry and review deadlines are absolute; the research requirement and
    event boundaries are crossings between the frozen as_of and ``at``.
    """

    limits: list[str] = []
    rule_id = str(application.rule_ref.id)
    if application.valid_until is not None and at >= application.valid_until:
        limits.append(f"rule_expired:{rule_id}")
    if at >= application.review_due_at:
        limits.append(f"rule_review_due:{rule_id}")
    definition = application.frozen_pair.rule.definition
    if isinstance(definition, ResearchRequirementDefinition) and (
        application.as_of < definition.check_by <= at
    ):
        limits.append(f"research_check_due:{rule_id}")
    selected = application.frozen_pair.selected_event
    if selected is not None:
        for boundary, name in (
            (selected.published_at, "event_publication_boundary"),
            (selected.effective_from, "event_effective_started"),
            (selected.effective_to, "event_effective_ended"),
        ):
            if boundary is not None and application.as_of < boundary <= at:
                limits.append(f"{name}:{selected.id}")
    return limits


def _event_limits(
    events: tuple[Any, ...],
    *,
    visible_until: datetime | None,
    anchored: set[tuple[str, int]],
    listing_id: int,
    listing: Listing | None,
    company_id: int | None,
) -> list[str]:
    """Events newly visible after the binding that no covered application anchors.

    The anchor set holds exact (event id, revision) pairs, so an old event_ref
    anchor never shadows a newer revision of the same event. Irrelevant
    events are probed only and never limit a conclusion.
    """

    if visible_until is None:
        return []
    identities: set[str] = {str(listing_id), f"listing:{listing_id}"}
    if listing is not None:
        identities.update({listing.ticker, f"company:{listing.company_id}", str(listing.company_id)})
    elif company_id is not None:
        identities.update({f"company:{company_id}", str(company_id)})
    limits: list[str] = []
    for event in events:
        available = event.available_from
        if available is not None and available <= visible_until:
            continue  # already visible when the task was bound
        if event.affected_subjects and not identities.intersection(event.affected_subjects):
            continue  # not subject relevant
        if (str(event.id), int(event.revision)) in anchored:
            continue  # the exact visible revision is anchored by a covered application
        limits.append(f"unbound_event:{event.id}")
    return limits


def coverage_limit_reasons(
    *,
    manifest: RuleCoverageManifest,
    binding_events_visible_until: datetime | None,
    live_payloads: tuple[dict[str, Any], ...],
    covered_applications: tuple[RuleApplication, ...],
    visible_events: tuple[Any, ...],
    listing: Listing | None,
    at: datetime,
    scan_truncated: bool = False,
    events_truncated: bool = False,
) -> list[str]:
    """The single coverage recheck predicate.

    Uncovered + applicable + critical rules are ``rule_coverage_pending``; a
    truncated rule or event scan is an explicit ``rule_coverage_scan_incomplete``;
    covered applications contribute rule and event time boundaries; events
    newly visible since the binding that no covered application anchors are
    ``unbound_event``. Never enqueues research and never mutates rows.
    """

    limits: list[str] = []
    if scan_truncated or events_truncated:
        limits.append("rule_coverage_scan_incomplete")
    covered = {str(entry.rule_ref.id) for entry in manifest.rule_entries}
    anchored: set[tuple[str, int]] = set()
    company_id: int | None = None
    for application in covered_applications:
        company_id = application.company_id
        selected = application.frozen_pair.selected_event
        if selected is not None:
            anchored.add((str(selected.id), int(selected.revision)))
        limits.extend(_application_limits(application, at))
    for payload in live_payloads:
        rule_id = str(payload["rule_ref"]["id"])
        if rule_id in covered or payload.get("applicability") != "applicable":
            continue  # covered, confirmed irrelevant, or non-matching
        if payload.get("frozen_pair", {}).get("rule", {}).get("severity") == "critical":
            limits.append(f"rule_coverage_pending:{rule_id}")
    limits.extend(_event_limits(
        visible_events,
        visible_until=binding_events_visible_until,
        anchored=anchored,
        listing_id=int(manifest.listing_ref.id),
        listing=listing,
        company_id=company_id,
    ))
    return list(dict.fromkeys(limits))


async def published_manifest_limits(
    session: AsyncSession, *, owner: OwnerId, manifest: RuleCoverageManifest,
    at: datetime | None = None,
) -> tuple[str, ...]:
    """Enumerate the current state (bounded) and compare it with the manifest.

    Runs entirely inside the caller's existing transaction or read snapshot;
    a historical read passes its own ``at`` so old views project honestly.
    """

    at = at or datetime.now(UTC)
    store = SupplementalRulesRepository(session)
    assessments = InvestmentAssessmentRepository(session)
    binding = await store.get_research_rule_binding(owner, manifest.binding_ref)
    if binding is None:
        return (f"rule_coverage_binding_missing:{manifest.binding_ref.id}",)
    payloads, truncated = await assessments.list_latest_live_rule_application_payloads(
        owner, int(manifest.listing_ref.id), "company_research"
    )
    covered: list[RuleApplication] = []
    for entry in manifest.rule_entries:
        if entry.status != "application" or entry.application_ref is None:
            continue
        application = await assessments.get_rule_application(owner, entry.application_ref)
        if application is not None:
            covered.append(application)
    events, events_truncated = await store.list_events_visible_by(
        owner, cutoff=at, visible_at=at, limit=SCAN_LIMIT
    )
    listing = await session.get(Listing, int(manifest.listing_ref.id))
    return tuple(coverage_limit_reasons(
        manifest=manifest,
        binding_events_visible_until=binding.events_visible_until,
        live_payloads=payloads,
        covered_applications=tuple(covered),
        visible_events=events,
        listing=listing,
        at=at,
        scan_truncated=truncated,
        events_truncated=events_truncated,
    ))

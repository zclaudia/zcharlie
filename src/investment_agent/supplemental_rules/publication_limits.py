"""Bounded publish-side coverage rechecks shared by reads and publication.

One predicate compares a published coverage manifest with the currently
matching rule set, rule/event time boundaries, and event visibility. The
enumerations run inside the caller's transaction through the storage
repositories, so reads of published opinions reach this only via the
storage repository and never import the supplemental package directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import (
    PollWindowSourceProof,
    ResearchRequirementDefinition,
    RuleApplication,
    RuleCoverage,
    RuleCoverageManifestV2,
    RuleCoverageManifestV3,
    SavedRefsSourceProof,
    UpdateSourceBinding,
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
    manifest: RuleCoverage,
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
    session: AsyncSession, *, owner: OwnerId, manifest: RuleCoverage,
    at: datetime | None = None, lock_source_state: bool = False,
    check_source_state: bool = True,
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
    source_limits = (
        await _source_coverage_limits(
            session, owner=owner, manifest=manifest, at=at,
            lock_source_state=lock_source_state,
        )
        if check_source_state else ()
    )
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
    rule_limits = coverage_limit_reasons(
        manifest=manifest,
        binding_events_visible_until=binding.events_visible_until,
        live_payloads=payloads,
        covered_applications=tuple(covered),
        visible_events=events,
        listing=listing,
        at=at,
        scan_truncated=truncated,
        events_truncated=events_truncated,
    )
    return tuple(dict.fromkeys((*source_limits, *rule_limits)))


async def _source_coverage_limits(
    session: AsyncSession, *, owner: OwnerId, manifest: RuleCoverage, at: datetime,
    lock_source_state: bool = False,
) -> tuple[str, ...]:
    """Recheck only source scopes explicitly frozen into this manifest.

    This keeps a source outage local to the listing/context that declared it.
    """

    if isinstance(manifest, RuleCoverageManifestV3):
        binding = manifest.source_coverage_binding
        saved_sources = [
            source.entry for source in binding.sources
            if isinstance(source, SavedRefsSourceProof)
        ]
        v3_limits: list[str] = []
        if saved_sources:
            policy_hash = canonical_hash(tuple(sorted(
                (source.source_id, source.contract_hash, source.scope_hash)
                for source in saved_sources
            )))
            saved_binding = UpdateSourceBinding(
                listing_ref=binding.listing_ref, context=binding.context,
                scope_key=binding.scope_key, sources=tuple(saved_sources),
                source_policy_hash=policy_hash, observed_at=binding.observed_at,
            )
            # Reuse the V2 saved-reference proof predicate without changing any
            # persisted V1/V2 DTO or hash domain.
            data = manifest.model_dump(mode="python")
            data.pop("source_coverage_binding", None)
            data.update({"schema_version": 2, "update_source_binding": saved_binding})
            data["manifest_hash"] = "pending"
            data["manifest_hash"] = canonical_hash(
                {key: value for key, value in data.items() if key != "manifest_hash"}
            )
            legacy_view = RuleCoverageManifestV2.model_validate(data)
            v3_limits.extend(await _source_coverage_limits(
                session, owner=owner, manifest=legacy_view, at=at,
                lock_source_state=lock_source_state,
            ))
        poll_sources = [
            source for source in binding.sources
            if isinstance(source, PollWindowSourceProof)
        ]
        for poll_source in poll_sources:
            v3_limits.extend(await _poll_source_coverage_limits(
                session, owner=owner, listing_id=int(manifest.listing_ref.id),
                context=binding.context, source=poll_source, at=at,
                lock_source_state=lock_source_state,
            ))
        return tuple(dict.fromkeys(v3_limits))

    if not isinstance(manifest, RuleCoverageManifestV2):
        from investment_agent.storage.repositories.supplemental_updates import (
            SupplementalUpdatesRepository,
        )

        declared = await SupplementalUpdatesRepository(session).list_sources(
            owner, int(manifest.listing_ref.id), "company_research",
            for_update=lock_source_state,
        )
        return ("source_coverage_unknown:binding_missing",) if declared else ()
    from investment_agent.storage.repositories.supplemental_updates import (
        SupplementalUpdatesRepository,
    )

    repo = SupplementalUpdatesRepository(session)
    legacy_limits: list[str] = []
    scope_key = manifest.update_source_binding.scope_key
    for source in manifest.update_source_binding.sources:
        state = await repo.get_source(
            owner, source.source_id, scope_key, for_update=lock_source_state,
        )
        marker = source.source_id
        if source.completeness != "complete":
            legacy_limits.append(f"source_coverage_incomplete:{marker}")
            continue
        if state is None:
            legacy_limits.append(f"source_coverage_unknown:{marker}")
            continue
        if (
            state.listing_id != manifest.listing_ref.id
            or state.context != manifest.update_source_binding.context
            or state.config_hash != source.contract_hash
            or state.contract_version != source.contract_version
            or state.scope_hash != source.scope_hash
            or state.health_limit_seconds != source.health_limit_seconds
        ):
            legacy_limits.append(f"source_coverage_unknown:{marker}")
            continue
        if state.coverage_state != "complete":
            status = "stale" if state.coverage_state == "failed" else "incomplete"
            legacy_limits.append(f"source_coverage_{status}:{marker}")
            continue
        if (
            state.last_success_at is not None
            and state.last_success_at.astimezone(UTC) > at.astimezone(UTC)
            or state.window_start is not None
            and state.window_start.astimezone(UTC) > at.astimezone(UTC)
            or state.window_end is not None
            and state.window_end.astimezone(UTC) > at.astimezone(UTC)
            or state.complete_watermark is not None
            and state.complete_watermark.astimezone(UTC) > at.astimezone(UTC)
        ):
            legacy_limits.append(f"source_coverage_unknown:{marker}")
            continue
        if (
            state.last_success_at is None
            or state.health_limit_seconds <= 0
            or at >= state.last_success_at.astimezone(UTC)
            + timedelta(seconds=state.health_limit_seconds)
        ):
            legacy_limits.append(f"source_coverage_stale:{marker}")
            continue
        expected_refs = [ref.model_dump(mode="json") for ref in source.source_refs]
        if canonical_hash(state.source_refs) != canonical_hash(expected_refs):
            legacy_limits.append(f"source_coverage_unknown:{marker}")
            continue
        # Keep the manifest's original receipt as immutable historical proof.
        # A later healthy check may advance the current success receipt only
        # when it revalidates the exact same saved refs and their content.
        receipt = await repo.get_change(owner, source.success_sequence)
        if (
            receipt is None
            or receipt.payload_hash != source.success_receipt_hash
            or receipt.listing_id != int(manifest.listing_ref.id)
            or receipt.context != manifest.update_source_binding.context
            or receipt.committed_at is None
            or receipt.committed_at.astimezone(UTC) != source.last_success_at.astimezone(UTC)
            or receipt.committed_at.astimezone(UTC) > at.astimezone(UTC)
            or receipt.acquired_at is not None
            and receipt.acquired_at.astimezone(UTC) > at.astimezone(UTC)
            or receipt.available_from is not None
            and receipt.available_from.astimezone(UTC) > at.astimezone(UTC)
            or receipt.payload.get("source_id") != source.source_id
            or receipt.payload.get("contract_version") != source.contract_version
            or receipt.payload.get("contract_hash") != source.contract_hash
            or receipt.payload.get("scope_hash") != source.scope_hash
            or receipt.payload.get("source_refs_hash") != canonical_hash(expected_refs)
            or canonical_hash(receipt.payload.get("source_refs")) != canonical_hash(expected_refs)
        ):
            legacy_limits.append(f"source_coverage_unknown:{marker}")
            continue
        if (
            state.success_sequence < source.success_sequence
            or state.window_start is None
            or state.window_end is None
            or state.complete_watermark is None
            or state.window_start > source.window_start
            or state.window_end < source.window_end
            or state.complete_watermark < source.window_end
        ):
            legacy_limits.append(f"source_coverage_unknown:{marker}")
            continue
        current_receipt = await repo.get_change(owner, state.success_sequence)
        if (
            current_receipt is None
            or current_receipt.payload_hash != state.success_receipt_hash
            or current_receipt.listing_id != int(manifest.listing_ref.id)
            or current_receipt.context != manifest.update_source_binding.context
            or current_receipt.committed_at is None
            or current_receipt.committed_at.astimezone(UTC)
            != state.last_success_at.astimezone(UTC)
            or current_receipt.committed_at.astimezone(UTC) > at.astimezone(UTC)
            or current_receipt.acquired_at is not None
            and current_receipt.acquired_at.astimezone(UTC) > at.astimezone(UTC)
            or current_receipt.available_from is not None
            and current_receipt.available_from.astimezone(UTC) > at.astimezone(UTC)
            or current_receipt.payload.get("source_id") != source.source_id
            or current_receipt.payload.get("contract_version") != source.contract_version
            or current_receipt.payload.get("contract_hash") != source.contract_hash
            or current_receipt.payload.get("scope_hash") != source.scope_hash
            or current_receipt.payload.get("source_refs_hash") != canonical_hash(expected_refs)
            or canonical_hash(current_receipt.payload.get("source_refs")) != canonical_hash(expected_refs)
        ):
            legacy_limits.append(f"source_coverage_unknown:{marker}")
            continue
        historical_sources = receipt.payload.get("sources")
        current_sources = current_receipt.payload.get("sources")
        if historical_sources is not None and current_sources != historical_sources:
            # Source refs are immutable object versions; a changed content hash
            # means the receipt no longer describes the manifest's evidence.
            legacy_limits.append(f"source_coverage_unknown:{marker}")
    return tuple(dict.fromkeys(legacy_limits))


async def _poll_source_coverage_limits(
    session: AsyncSession, *, owner: OwnerId, listing_id: int, context: str,
    source: PollWindowSourceProof, at: datetime, lock_source_state: bool,
) -> tuple[str, ...]:
    """Recheck one V3 poll proof against bounded historical/current source state."""
    from investment_agent.storage.repositories.supplemental_updates import (
        SupplementalUpdatesRepository,
        load_poll_window_proof,
    )

    marker = source.source_id
    repo = SupplementalUpdatesRepository(session)
    scope_key = f"listing:{listing_id}:{context}"
    state = await repo.get_source(owner, marker, scope_key, for_update=lock_source_state)
    if state is None:
        return (f"source_coverage_unknown:{marker}",)
    if (
        state.listing_id != listing_id or state.context != context
        or state.config_hash != source.contract_hash
        or state.contract_version != source.contract_version
        or state.scope_hash != source.scope_hash
        or state.health_limit_seconds != source.health_limit_seconds
    ):
        return (f"source_coverage_unknown:{marker}",)
    if state.coverage_state != "complete":
        status = "stale" if state.coverage_state == "failed" else "incomplete"
        return (f"source_coverage_{status}:{marker}",)
    try:
        target_raw = state.payload.get("coverage_target_end")
        frontier_raw = state.payload.get("history_frontier")
        if not isinstance(target_raw, str) or not isinstance(frontier_raw, str):
            return (f"source_coverage_unknown:{marker}",)
        target_end = datetime.fromisoformat(target_raw.replace("Z", "+00:00"))
        history_frontier = datetime.fromisoformat(frontier_raw.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return (f"source_coverage_unknown:{marker}",)
    if (
        target_end.tzinfo is None or history_frontier.tzinfo is None
        or state.payload.get("history_gap") is True
        or history_frontier.astimezone(UTC) < target_end.astimezone(UTC)
    ):
        return (f"source_coverage_incomplete:{marker}",)
    at = at.astimezone(UTC)
    if (
        state.last_success_at is None or state.health_limit_seconds <= 0
        or at >= state.last_success_at.astimezone(UTC)
        + timedelta(seconds=state.health_limit_seconds)
    ):
        return (f"source_coverage_stale:{marker}",)
    if (
        state.last_success_at.astimezone(UTC) > at
        or state.window_end is not None and state.window_end.astimezone(UTC) > at
        or state.complete_watermark is not None and state.complete_watermark.astimezone(UTC) > at
    ):
        return (f"source_coverage_unknown:{marker}",)

    protocol_profile = state.payload.get("protocol_profile")
    protocol_status = state.payload.get("protocol_status")
    if (
        protocol_profile != source.protocol_profile
        or protocol_status != source.protocol_status
        or (protocol_profile == "fixture" and (
            protocol_status != "fixture_validated"
            or not session.info.get("allow_disclosure_fixture_proofs", False)
        ))
        or (protocol_profile == "live" and protocol_status != "live_validated")
    ):
        return (f"source_coverage_unknown:{marker}",)

    if protocol_profile == "live":
        from investment_agent.evidence.disclosure_profiles import approved_contract

        if not approved_contract(source.contract_hash, now=at):
            return (f"source_coverage_unknown:{marker}",)

    historical = await load_poll_window_proof(
        session, owner=owner, listing_id=listing_id, context=context,
        cutoff=source.committed_at, expected_source_state=state,
        expected_final_ref=source.final_ref.model_dump(mode="json"),
    )
    if historical is None:
        return (f"source_coverage_unknown:{marker}",)
    try:
        saved_historical = PollWindowSourceProof.model_validate(historical)
    except ValueError:
        return (f"source_coverage_unknown:{marker}",)
    if saved_historical != source:
        return (f"source_coverage_unknown:{marker}",)

    current_data = await load_poll_window_proof(
        session, owner=owner, listing_id=listing_id, context=context,
        cutoff=at, expected_source_state=state,
    )
    if current_data is None:
        return (f"source_coverage_unknown:{marker}",)
    try:
        current = PollWindowSourceProof.model_validate(current_data)
    except ValueError:
        return (f"source_coverage_unknown:{marker}",)
    current_state = state.payload
    if (
        current_state.get("latest_final_sequence") != current.final_ref.sequence
        or current_state.get("latest_final_id") != str(current.final_ref.id)
        or current_state.get("latest_final_hash") != current.final_ref.payload_hash
        or current_state.get("protocol_profile") != current.protocol_profile
        or current_state.get("protocol_status") != current.protocol_status
        or current_state.get("history_epoch") != str(current.history_epoch)
        or current_state.get("epoch_ordinal") != current.epoch_ordinal
        or current_state.get("semantic_generation") != current.semantic_generation
        or current_state.get("coverage_anchor") != current.coverage_anchor
        or current.protocol_profile != source.protocol_profile
        or current.protocol_status != source.protocol_status
        or current.contract_hash != source.contract_hash
        or current.scope_hash != source.scope_hash
        or current.history_epoch != source.history_epoch
        or current.epoch_ordinal != source.epoch_ordinal
        or current.semantic_generation != source.semantic_generation
        or current.coverage_anchor != source.coverage_anchor
        or current.issuer_id != source.issuer_id
        or current.history_basis_hash != source.history_basis_hash
        or current.history_start > source.history_start
        or current.continuous_index_watermark is None
        or source.continuous_index_watermark is None
        or current.continuous_index_watermark < source.continuous_index_watermark
        or current.continuous_original_watermark is None
        or source.continuous_original_watermark is None
        or current.continuous_original_watermark < source.continuous_original_watermark
        or current.continuous_complete_watermark is None
        or source.continuous_complete_watermark is None
        or current.continuous_complete_watermark < source.continuous_complete_watermark
    ):
        return (f"source_coverage_incomplete:{marker}",)
    # Authorization remains an independent current-time gate. A valid frozen
    # historical poll is not permission to continue publishing after revocation
    # or expiry.
    try:
        from investment_agent.supplemental_rules.disclosure_authorization import (
            read_authorization_snapshot,
        )
        authorization = await read_authorization_snapshot(
            # A later healthy poll may use a renewed authorization. Historical
            # grant identity is verified by its immutable receipt above; the
            # independent live gate must inspect the grant on the current proof.
            session, current.grant_ref, owner_id=owner,
        )
    except Exception:
        return (f"source_coverage_stale:{marker}",)
    if getattr(authorization, "schema_version", None) == 2:
        from investment_agent.supplemental_rules.disclosure_authorization import (
            get_current_authorization,
        )

        try:
            await get_current_authorization(session, authorization.authorization_id, now=at)
        except Exception:
            return (f"source_coverage_stale:{marker}",)
    if (
        authorization.status != "active" or authorization.revoked_at is not None
        or authorization.expires_at <= at
    ):
        return (f"source_coverage_stale:{marker}",)
    if (
        authorization.authorization_hash != current.grant_hash
        or authorization.contract_hash != current.contract_hash
        or authorization.owner_id != owner
        or current.grant_ref != authorization.authorization_id
        or not any(str(ref.id) == str(listing_id) for ref in authorization.listing_refs)
    ):
        return (f"source_coverage_unknown:{marker}",)
    return ()

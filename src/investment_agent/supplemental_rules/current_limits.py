"""Current restrictions never mutate saved applications or trigger work."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.supplemental_rules.contracts import (
    EvaluationRequest,
    FrozenManifest,
    ResearchRequirementDefinition,
    RuleApplication,
)
from investment_agent.supplemental_rules.read_snapshot import ensure_read_snapshot
from investment_agent.supplemental_rules.views import CurrentLimits

if TYPE_CHECKING:
    from investment_agent.supplemental_rules.service import SupplementalRuleService


async def application_limits(service: SupplementalRuleService, app: RuleApplication, *, manifest: FrozenManifest | None = None, current_apps: tuple[RuleApplication, ...] | None = None, now: datetime | None = None, scope_fingerprint: str | None = None, latest_refs: frozenset[ObjectRef] | None = None) -> CurrentLimits:
    if manifest is None or current_apps is None:
        await ensure_read_snapshot(service.session)
    at = now or datetime.now(UTC)
    reasons = []
    if app.application_mode != "live":
        reasons.append("historical_replay")
    if app.valid_until is not None and at >= app.valid_until:
        reasons.append("rule_expired")
    if at >= app.review_due_at:
        reasons.append("rule_review_due")
    definition = app.frozen_pair.rule.definition
    if isinstance(definition, ResearchRequirementDefinition) and app.as_of < definition.check_by <= at:
        reasons.append("research_check_due")
    selected = app.frozen_pair.selected_event
    if selected is not None:
        for boundary, reason in ((selected.published_at, "event_publication_boundary"),
                                 (selected.effective_from, "event_effective_started"),
                                 (selected.effective_to, "event_effective_ended")):
            if boundary is not None and app.as_of < boundary <= at:
                reasons.append(reason)
    try:
        if manifest is None:
            request = EvaluationRequest(idempotency_key=uuid4(), listing_refs=(app.listing_ref,), contexts=frozenset({app.context}), mode="live")
            manifest = await service.freeze(request, as_of=at)
        if current_apps is None:
            current_apps = await service.latest_live(app.listing_ref, app.context)
    except InvestorError as exc:
        if exc.detail.code not in {"invalid_reference", "source_capacity_exceeded"}:
            raise
        return CurrentLimits(current_status="unknown", reasons=tuple((*reasons, "coverage_pending", exc.detail.code)), refresh_required=True, coverage_pending=True)
    if scope_fingerprint is None:
        scope_fingerprint = service.scope_hash(manifest, app.listing_ref, app.context)
    if app.payload.get("scope_semantic_hash") != scope_fingerprint:
        reasons.append("supplemental_sources_changed")
    if latest_refs is None:
        latest_refs = frozenset(a.reference for a in current_apps)
    if app.reference not in latest_refs:
        reasons.append("application_superseded")
    if manifest.scan_diagnostics:
        reasons.append("coverage_pending")
    unique = tuple(sorted(set(reasons)))
    return CurrentLimits(current_status="stale" if unique else "valid", reasons=unique,
        refresh_required=bool(unique), coverage_pending=bool(manifest.scan_diagnostics))

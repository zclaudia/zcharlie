"""Read-only P2 projections, independent of company and account conclusions."""

from datetime import datetime
from typing import Any, Literal

from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.contracts import Dimension
from investment_agent.supplemental_rules.contracts import (
    Context,
    CoverageRuleEntry,
    EffectiveEffect,
    EvidenceFacet,
    PreparedClosureProposal,
    ResearchEvent,
    ResearchRuleBinding,
    RuleApplication,
    RuleCoverageManifest,
    RuleDefinition,
    Severity,
)


class CurrentLimits(Contract):
    current_status: Literal["valid", "stale", "unknown"]
    reasons: tuple[str, ...] = ()
    refresh_required: bool = False
    coverage_pending: bool = False


class ApplicationView(Contract):
    status: Literal["available", "missing"] = "available"
    payload: RuleApplication | None
    current_limits: CurrentLimits | None


class IssueBasis(Contract):
    application_ref: ObjectRef
    rule_ref: ObjectRef
    event_ref: ObjectRef | None
    severity: Severity
    materiality: str
    condition_result: str
    evidence_status: str
    assessment_status: str
    impact_assessment: EvidenceFacet
    effects: tuple[EffectiveEffect, ...]
    release_requirements: tuple[str, ...]
    diagnostics: tuple[str, ...]


class IssueGroup(Contract):
    display_key: str
    company_id: int
    dimension: Dimension
    issue_key: str | None
    application_refs: tuple[ObjectRef, ...]
    rule_refs: tuple[ObjectRef, ...]
    event_refs: tuple[ObjectRef, ...]
    bases: tuple[IssueBasis, ...]


class CurrentApplicationsView(Contract):
    listing_ref: ObjectRef
    context: Context
    applications: tuple[ApplicationView, ...]
    issue_groups: tuple[IssueGroup, ...] = ()
    unbound_events: tuple[ResearchEvent, ...] = ()
    coverage_complete: bool
    mixed: bool = False
    diagnostics: tuple[str, ...] = ()
    cursor: str | None = None
    observed_at: datetime


class SupplementalRuleContextView(Contract):
    enabled: bool
    registry_revision: int
    rules_generation: int
    events_generation: int
    contexts: tuple[Context, ...]
    rule_types: tuple[Literal["metric_condition", "research_requirement", "event_impact"], ...]
    effects: tuple[
        Literal["annotate", "require_evidence", "require_review", "limit_conclusion"], ...
    ]
    factors: tuple[dict[str, Any], ...]
    listing_refs: tuple[ObjectRef, ...]
    listing_scan_truncated: bool
    company_identity_available: bool
    source_capabilities: dict[str, bool]


class RuleListItem(Contract):
    rule_id: str
    head_revision: int
    active_state: Literal["active", "suspended", "retired"] | None
    active_rule: RuleDefinition | None
    draft_state: Literal["draft", "validated"] | None
    draft_rule: RuleDefinition | None
    last_validation: dict[str, Any] | None


class RuleListView(Contract):
    registry_revision: int
    items: tuple[RuleListItem, ...]
    next_cursor: str | None


class RuleHeadView(Contract):
    head_revision: int
    active_state: Literal["active", "suspended", "retired"] | None
    active: RuleDefinition | None
    draft_state: Literal["draft", "validated"] | None
    draft: RuleDefinition | None
    last_validation: dict[str, Any] | None


class RuleHistoryEntry(Contract):
    sequence: int
    command: str
    rule_revision: int | None
    operation_id: str
    created_at: datetime
    payload: dict[str, Any]


class RuleHistoryView(Contract):
    rule_id: str
    through_sequence: int
    events: tuple[RuleHistoryEntry, ...]
    next_after_sequence: int | None


class ResearchEventListView(Contract):
    registry_revision: int
    events_generation: int
    events: tuple[ResearchEvent, ...]
    next_cursor: str | None


class CoverageLimitSummary(Contract):
    """Current-limitation reasons of one published manifest; empty means the
    bounded recheck found nothing new. Prepared manifests never carry one."""

    reasons: tuple[str, ...] = ()


class CurrentRuleCoverageView(Contract):
    """Per-subject honesty projection: the newest published manifest plus the
    binding provenance, or an explicit unknown reason (never an empty set)."""

    listing_ref: ObjectRef
    enabled: bool
    state: Literal["bound", "unknown"]
    reasons: tuple[str, ...] = ()
    manifest: RuleCoverageManifest | None = None
    binding: ResearchRuleBinding | None = None
    current_limits: CoverageLimitSummary | None = None
    observed_at: datetime


class RuleCoverageView(Contract):
    """Exact prepared/published coverage read model with one bounded entry page.

    ``manifest_hash`` and ``entry_total`` carry the immutable identity so a
    page cursor can bind itself to both; ``current_limits`` is computed only
    for the published stage and never triggers re-evaluation.
    """

    stage: Literal["prepared", "published"]
    coverage_ref: ObjectRef
    task_ref: ObjectRef
    binding_ref: ObjectRef
    binding: ResearchRuleBinding | None
    listing_ref: ObjectRef
    case_ref: ObjectRef | None = None
    admission_as_of: datetime
    effective_cutoff: datetime
    created_at: datetime
    committed_at: datetime | None = None
    manifest_hash: str
    entry_total: int
    entries: tuple[CoverageRuleEntry, ...]
    next_cursor: str | None = None
    open_issue_refs: tuple[ObjectRef, ...] = ()
    closure_proposals: tuple[PreparedClosureProposal, ...] = ()
    closure_application_refs: tuple[ObjectRef, ...] = ()
    closure_applications: tuple[RuleApplication, ...] = ()
    scan_capped: bool = False
    scan_known_total: int | None = None
    scan_uncovered_note: str | None = None
    prepared_ref: ObjectRef | None = None
    semantic_review_ref: ObjectRef | None = None
    thesis_ref: ObjectRef | None = None
    recommendation_ref: ObjectRef | None = None
    limits_recheck: tuple[str, ...] = ()
    current_limits: CoverageLimitSummary | None = None


class TaskRuleBindingView(Contract):
    """One task's admission binding, or the explicit legacy marker.

    ``legacy_unbound`` means no binding row exists for the task (admitted
    before the rule chain, with the switch off, or not owned by the caller);
    it is never rendered as an empty rule set.
    """

    task_ref: ObjectRef
    state: Literal["bound", "legacy_unbound"]
    binding: ResearchRuleBinding | None = None
    reasons: tuple[str, ...] = ()

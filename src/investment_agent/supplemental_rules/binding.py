"""Admission-time research rule binding (AF-P3-04).

One bounded, read-only enumeration inside the caller's admission transaction
writes one immutable binding row. The binder never runs a model, the network,
or an evaluation; rules activated later stay outside and are handled by the
publication-time current check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import (
    BindingRuleEntry,
    Context,
    ResearchRuleBinding,
    RuleDefinition,
)
from investment_agent.supplemental_rules.inputs import freeze_subject
from investment_agent.supplemental_rules.matching import MATCHER_VERSION, match_subject
from investment_agent.supplemental_rules.service import (
    SCAN_LIMIT,
    _factor_names,
    _selector_sources,
)

BINDING_CONTEXT: Context = "company_research"


class BindingScanOverflowError(ValueError):
    """The bounded rule enumeration hit its stop-line; admission must fail."""


def binding_identity(owner: OwnerId, task_id: UUID) -> UUID:
    """One task binds at most once; retries derive the same binding identity."""

    return uuid5(NAMESPACE_URL, f"research_rule_binding:{owner}:{task_id}")


def _rule_state(rule: RuleDefinition, as_of: datetime) -> str:
    # Mirrors the state derivation in SupplementalRuleService._freeze.
    if rule.valid_until is not None and as_of >= rule.valid_until:
        return "expired"
    if as_of >= rule.review_due_at:
        return "review_due"
    return "active"


def _match_rows(candidates: tuple[RuleDefinition, ...], subject: Any) -> tuple[dict[str, Any], ...]:
    """Deterministic per-rule selector outcomes; unknown is never guessed away."""

    rows = []
    for rule in sorted(candidates, key=lambda item: (str(item.id), item.revision)):
        match = match_subject(rule.selector, subject)
        rows.append({
            "rule_ref": rule.reference.model_dump(mode="json"),
            "status": match.status,
            "diagnostics": match.diagnostics,
        })
    return tuple(rows)


def binding_input_hash(
    *,
    owner: OwnerId,
    task_ref: ObjectRef,
    listing_ref: ObjectRef,
    research_kind: str,
    method_ref: ObjectRef,
    admission_as_of: datetime,
    rules_generation: int,
    events_generation: int,
    rule_set: tuple[BindingRuleEntry, ...],
    matching_manifest_hash: str | None,
    events_visible_until: datetime | None,
    events_visible_unknown: bool,
) -> str:
    """Frozen content domain of the binding's caller-supplied input digest."""

    return canonical_hash({
        "schema_version": 1,
        "owner_id": owner,
        "task_ref": task_ref,
        "listing_ref": listing_ref,
        "research_kind": research_kind,
        "method_ref": method_ref,
        "admission_as_of": admission_as_of,
        "rules_generation": rules_generation,
        "events_generation": events_generation,
        "rule_set": rule_set,
        "matching_manifest_hash": matching_manifest_hash,
        "events_visible_until": events_visible_until,
        "events_visible_unknown": events_visible_unknown,
    })


async def get_task_binding(
    session: AsyncSession, *, owner: OwnerId, task_id: UUID | str
) -> ResearchRuleBinding | None:
    """Read the unique binding of one task; another owner's row reads as absent."""

    return await SupplementalRulesRepository(session).get_research_rule_binding_for_task(
        owner, task_id
    )


async def get_binding(
    session: AsyncSession, *, owner: OwnerId, reference: ObjectRef
) -> ResearchRuleBinding | None:
    """Read one exact binding UUID after owner and kind checks."""

    return await SupplementalRulesRepository(session).get_research_rule_binding(owner, reference)


async def create_task_binding(
    session: AsyncSession,
    *,
    owner: OwnerId,
    task_id: UUID,
    listing_ref: ObjectRef,
    research_kind: str,
    method_ref: ObjectRef,
    as_of: datetime,
) -> ResearchRuleBinding:
    """Freeze and persist the admission-time rule binding for one task.

    ``as_of`` is the task's admission instant: rule validity, the subject
    snapshot, event visibility, and the recorded ``admission_as_of`` all use
    it, so a later prepare cutoff can never precede the binding. The caller
    owns the transaction; this writes without committing. A task that already
    has a binding returns it unchanged (re-admission and worker retries never
    re-enumerate). An empty rule set is a legal binding.
    """

    store = SupplementalRulesRepository(session)
    existing = await store.get_research_rule_binding_for_task(owner, task_id)
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    registry = await store.get_registry_head(owner)
    active, rules_overflow = await store.list_active_rules(owner, limit=SCAN_LIMIT)
    if rules_overflow:
        raise BindingScanOverflowError("rule candidate scan exceeded the bounded binding limit")
    candidates = tuple(sorted(
        (rule for rule in active
         if BINDING_CONTEXT in rule.contexts and _rule_state(rule, as_of) in {"active", "review_due"}),
        key=lambda item: (str(item.id), item.revision),
    ))
    events, events_overflow = await store.list_events_visible_by(
        owner, cutoff=as_of, visible_at=as_of, limit=SCAN_LIMIT
    )
    del events  # visibility is summarized, not enumerated, in the binding

    matching_manifest_hash: str | None = None
    rule_set: tuple[BindingRuleEntry, ...] = ()
    if candidates:
        # Same bounded source snapshot as evaluation freezing; exact-identity
        # mismatches never contribute foreign sources or factors.
        listing = await session.get(Listing, int(listing_ref.id))
        relevant = [
            rule for rule in candidates
            if not (rule.selector.kind == "listing" and rule.selector.listing_ref != listing_ref)
            and not (rule.selector.kind == "company" and listing is not None
                     and rule.selector.company_id != listing.company_id)
        ]
        factors: set[str] = set()
        for rule in relevant:
            factors.update(_factor_names(getattr(rule.definition, "condition", None)))
        evidence_refs = tuple(dict.fromkeys(
            ref for rule in relevant for ref in (*rule.source_refs, *_selector_sources(rule.selector))
        ))
        subject = await freeze_subject(
            session, listing_ref, as_of, owner_id=owner,
            factor_names=tuple(sorted(factors)), evidence_refs=evidence_refs,
            strict_source_subjects=False,
        )
        rows = _match_rows(candidates, subject)
        matching_manifest_hash = canonical_hash({
            "version": MATCHER_VERSION, "subject_as_of": as_of, "matches": rows,
        })
        rule_set = tuple(
            BindingRuleEntry(
                rule_ref=rule.reference,
                semantic_hash=rule.semantic_hash or canonical_hash(rule.model_dump(mode="python")),
                selector_hash=canonical_hash(rule.selector),
            )
            for rule in candidates
        )
    task_ref = ObjectRef(kind="task", id=task_id, revision=1)
    input_hash = binding_input_hash(
        owner=owner, task_ref=task_ref, listing_ref=listing_ref, research_kind=research_kind,
        method_ref=method_ref, admission_as_of=as_of, rules_generation=registry.rules_generation,
        events_generation=registry.events_generation, rule_set=rule_set,
        matching_manifest_hash=matching_manifest_hash, events_visible_until=now,
        events_visible_unknown=events_overflow,
    )
    data: dict[str, Any] = {
        "schema_version": 1,
        "id": binding_identity(owner, task_id),
        "revision": 1,
        "owner_id": owner,
        "task_ref": task_ref,
        "listing_ref": listing_ref,
        "research_kind": research_kind,
        "method_ref": method_ref,
        "admission_as_of": as_of,
        "rules_generation": registry.rules_generation,
        "events_generation": registry.events_generation,
        "rule_set": rule_set,
        "rule_set_hash": canonical_hash(rule_set),
        "matching_manifest_hash": matching_manifest_hash,
        "events_visible_until": as_of,
        "events_visible_unknown": events_overflow,
        "input_hash": input_hash,
        "created_at": now,
        "content_hash": "pending",
    }
    data["content_hash"] = canonical_hash(
        {key: value for key, value in data.items() if key != "content_hash"}
    )
    binding = ResearchRuleBinding.model_validate(data)
    return await store.save_research_rule_binding(owner, binding)

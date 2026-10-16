"""Stable open-issue inheritance and display-only deduplication."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from investment_agent.investor.common import ObjectRef

if TYPE_CHECKING:
    from investment_agent.supplemental_rules.contracts import RuleApplication
    from investment_agent.supplemental_rules.views import IssueGroup


def carry_open_issue_refs(
    prior_refs: Iterable[ObjectRef], additions: Iterable[ObjectRef] = ()
) -> tuple[ObjectRef, ...]:
    """Carry exact old application identities; no current selector can erase them."""
    refs = {ref.model_dump_json(exclude_none=True): ref for ref in (*tuple(prior_refs), *tuple(additions))}
    return tuple(refs[key] for key in sorted(refs))


def display_issue_key(
    *, company_id: int, event_ref: ObjectRef | None, issue_key: str | None, dimension: str,
    rule_id: str | None = None,
) -> str:
    """Compute a presentation grouping identity without modifying issue refs."""
    payload = {
        "company_id": company_id,
        "event_id": str(event_ref.id) if event_ref else None,
        "issue_key": issue_key,
        "dimension": dimension,
        "rule_id": rule_id if event_ref is None and issue_key is None else None,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def deduplicate_issue_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Merge display duplicates while retaining every rule/application basis.

    Measures and incompatible issue identities remain separate. This is a
    presentation projection only and never sums severity or clears a finding.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    standalone: list[Mapping[str, Any]] = []
    for row in rows:
        company_id = row.get("company_id")
        dimension = row.get("dimension")
        key = row.get("display_key")
        if not key and company_id is not None and dimension:
            event_ref = row.get("event_ref")
            if isinstance(event_ref, dict):
                event_ref = ObjectRef.model_validate(event_ref)
            key = display_issue_key(company_id=int(company_id), event_ref=event_ref,
                                    issue_key=row.get("issue_key"), dimension=str(dimension))
        if not key:
            standalone.append(row)
        else:
            grouped[str(key)].append(row)
    result = [dict(row) for row in standalone]
    for key, members in sorted(grouped.items()):
        first = dict(members[0])
        applications = []
        rules = []
        conditions = []
        conflicts = []
        effects = []
        for member in members:
            applications.extend(member.get("application_refs", ()))
            rules.extend(member.get("rule_refs", ()))
            conditions.extend(member.get("conditions", ()))
            conflicts.extend(member.get("conflicts", ()))
            effects.extend(member.get("effects", ()))
        first.update({
            "display_key": key,
            "application_refs": _stable_json_unique(applications),
            "rule_refs": _stable_json_unique(rules),
            "conditions": _stable_json_unique(conditions),
            "conflicts": _stable_json_unique(conflicts),
            "effects": _stable_json_unique(effects),
            "display_member_count": len(members),
        })
        result.append(first)
    return tuple(result)


def project_issue_groups(applications: Iterable[RuleApplication], page_refs: set[ObjectRef]) -> tuple[IssueGroup, ...]:
    """Project current-page groups with exact bases from the entire current scope.

    Grouping never changes persisted issue identities or chooses a winning
    severity, condition or semantic assessment among disagreeing rules.
    """
    from investment_agent.supplemental_rules.views import IssueBasis, IssueGroup

    grouped: dict[str, list[RuleApplication]] = defaultdict(list)
    dimensions: dict[str, Any] = {}
    for app in applications:
        if not app.open_issue_refs and not app.effective_effects:
            continue
        rule = app.frozen_pair.rule
        for dimension in sorted(rule.dimensions):
            key = display_issue_key(company_id=app.company_id, event_ref=app.event_ref,
                issue_key=rule.issue_key, dimension=dimension, rule_id=str(rule.id))
            grouped[key].append(app)
            dimensions[key] = dimension
    output = []
    for key, members in sorted(grouped.items()):
        if not any(app.reference in page_refs for app in members):
            continue
        first = members[0]
        bases = tuple(IssueBasis(application_ref=app.reference, rule_ref=app.rule_ref,
            event_ref=app.event_ref, severity=app.frozen_pair.rule.severity,
            materiality=app.frozen_pair.rule.materiality, condition_result=app.condition_result,
            evidence_status=app.evidence_status, assessment_status=app.assessment_status,
            impact_assessment=app.result.impact_assessment, effects=app.result.effective_effects,
            release_requirements=app.frozen_pair.rule.release_requirements,
            diagnostics=app.result.diagnostics) for app in members)
        output.append(IssueGroup(display_key=key, company_id=first.company_id,
            dimension=dimensions[key], issue_key=first.frozen_pair.rule.issue_key,
            application_refs=tuple(app.reference for app in members),
            rule_refs=tuple(dict.fromkeys(app.rule_ref for app in members)),
            event_refs=tuple(dict.fromkeys(app.event_ref for app in members if app.event_ref)),
            bases=bases))
    return tuple(output)


def _stable_json_unique(values: Iterable[Any]) -> list[Any]:
    keyed = {
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str): value
        for value in values
    }
    return [keyed[key] for key in sorted(keyed)]

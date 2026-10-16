"""Deterministic validation for user-authored P2 supplemental rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.store import EvidenceService
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.repositories.supplemental_rules import (
    SupplementalRulesRepository,
)
from investment_agent.supplemental_rules.canonical import (
    canonical_hash,
    rule_content_hash,
    rule_semantic_hash,
)
from investment_agent.supplemental_rules.contracts import (
    CategoryExpression,
    CategorySelector,
    CompanySelector,
    Definition,
    MetricCondition,
    RuleDefinition,
)

VALIDATOR_VERSION = "supplemental-rule-validator.v1"
VALIDATION_LIFETIME = timedelta(hours=24)


def _category_terms(expression: CategoryExpression) -> tuple[Any, ...]:
    terms: list[Any] = []
    for term in expression.terms:
        if isinstance(term, CategoryExpression):
            terms.extend(_category_terms(term))
        else:
            terms.append(term)
    return tuple(terms)


@dataclass(frozen=True)
class ValidationOutcome:
    """A stable validation result suitable for appending to rule audit history."""

    outcome: Literal["pass", "fail"]
    rule_ref: ObjectRef
    content_hash: str
    semantic_hash: str
    validation_hash: str
    validator_version: str
    dependency_fingerprint: str
    validated_at: datetime
    expires_at: datetime
    checked_head_revision: int
    committed_head_revision: int
    diagnostics: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "rule_ref": self.rule_ref.model_dump(mode="json"),
            "content_hash": self.content_hash,
            "semantic_hash": self.semantic_hash,
            "validation_hash": self.validation_hash,
            "validator_version": self.validator_version,
            "dependency_fingerprint": self.dependency_fingerprint,
            "validated_at": self.validated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "checked_head_revision": self.checked_head_revision,
            "committed_head_revision": self.committed_head_revision,
            "diagnostics": list(self.diagnostics),
        }


def _metric_factors(definition: Definition) -> tuple[str, ...]:
    if definition.kind != "metric_condition":
        return ()
    assert isinstance(definition.condition, MetricCondition)

    def walk(node: Any) -> list[str]:
        if hasattr(node, "factor_id"):
            return [node.factor_id]
        return [factor for child in node.conditions for factor in walk(child)]

    return tuple(dict.fromkeys(walk(definition.condition)))


def _metric_leaves(definition: Definition) -> tuple[Any, ...]:
    if definition.kind != "metric_condition":
        return ()
    assert isinstance(definition.condition, MetricCondition)
    leaves: list[Any] = []

    def visit(node: Any) -> None:
        if hasattr(node, "factor_id"):
            leaves.append(node)
        else:
            for child in node.conditions:
                visit(child)

    visit(definition.condition)
    return tuple(leaves)


async def _check_selector(session: AsyncSession, selector: Any) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if selector.kind == "listing":
        listing = await session.get(Listing, int(selector.listing_ref.id))
        if listing is None:
            diagnostics.append("selector_listing_not_found")
    elif selector.kind == "company":
        assert isinstance(selector, CompanySelector)
        company = await session.get(Company, selector.company_id)
        if company is None:
            diagnostics.append("selector_company_not_found")
    elif selector.kind == "category":
        assert isinstance(selector, CategorySelector)
        for term in _category_terms(selector.expression):
            if term.kind == "industry":
                if term.source_ref.kind != "evidence":
                    diagnostics.append("industry_category_source_must_be_evidence")
            elif term.source_ref.kind != "evidence":
                diagnostics.append("membership_category_source_must_be_evidence")
    return tuple(diagnostics)


async def _check_source_refs(
    session: AsyncSession,
    owner_id: OwnerId,
    rule: RuleDefinition,
    now: datetime,
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    diagnostics: list[str] = []
    dependencies: list[dict[str, Any]] = []
    source_refs = list(rule.source_refs)
    if rule.event_ref is not None:
        source_refs.append(rule.event_ref)
    if isinstance(rule.selector, CategorySelector):
        source_refs.extend(term.source_ref for term in _category_terms(rule.selector.expression))

    # Exact duplicates are harmless but need only one source probe.
    unique = {(ref.kind, str(ref.id), ref.revision): ref for ref in source_refs}
    evidence_service = EvidenceService(session)
    repository = SupplementalRulesRepository(session)
    for ref in (unique[key] for key in sorted(unique)):
        if ref.kind == "evidence":
            saved = await session.get(SavedEvidence, (str(ref.id), ref.revision))
            if saved is None:
                diagnostics.append(f"source_evidence_not_yet_saved:{ref.id}@{ref.revision}")
                continue
            if saved.owner_id != owner_id:
                diagnostics.append(f"source_evidence_owner_mismatch:{ref.id}@{ref.revision}")
                continue
            subject_ref = rule.selector.listing_ref if rule.selector.kind == "listing" else None
            try:
                result = await evidence_service.get_evidence(
                    owner_id, ref, now, subject_ref=subject_ref, visible_at=now
                )
            except InvestorError:
                # An exact saved record tied to a different listing is an
                # invalid reference, while ordinary time visibility is only
                # a runtime limitation.
                diagnostics.append(f"source_evidence_subject_mismatch:{ref.id}@{ref.revision}")
                continue
            if result.data is None:
                diagnostics.append(f"source_evidence_not_visible_yet:{ref.id}@{ref.revision}")
            else:
                dependencies.append(
                    {
                        "ref": ref.model_dump(mode="json"),
                        "content_hash": result.data.content_hash,
                        "status": result.status,
                    }
                )
        elif ref.kind == "research_event":
            source = await repository.get_event_revision(owner_id, ref)
            if source is None:
                diagnostics.append(f"source_event_unavailable:{ref.id}@{ref.revision}")
            else:
                dependencies.append(
                    {
                        "ref": ref.model_dump(mode="json"),
                        "content_hash": source.content_hash,
                        "verification": source.verification,
                    }
                )
        elif ref.kind in {"market_snapshot", "financial_snapshot", "listing"}:
            # These exact references are allowed in definitions, but no
            # provider or historical loader is run by validation.
            diagnostics.append(f"source_reference_requires_runtime_check:{ref.kind}:{ref.id}")
        else:
            diagnostics.append(f"unsupported_source_reference_kind:{ref.kind}")
    return tuple(diagnostics), tuple(dependencies)


async def validate_rule(
    session: AsyncSession,
    owner_id: OwnerId,
    rule: RuleDefinition,
    *,
    expected_head_revision: int,
    committed_head_revision: int,
    validated_at: datetime | None = None,
) -> ValidationOutcome:
    """Validate syntax, supported references, factor definitions and time.

    Missing runtime evidence remains a pass diagnostic; a malformed or
    cross-owner exact reference fails. The validator is read-only and performs
    no acquisition or model call.
    """
    now = utc_timestamp(validated_at or datetime.now(UTC))
    rule = RuleDefinition.model_validate(rule.model_dump(mode="python"))
    ref = ObjectRef(kind="supplemental_rule", id=rule.id, revision=rule.revision)
    errors: list[str] = []
    notices: list[str] = []

    if rule.valid_until is not None and rule.valid_until <= now:
        errors.append("rule_validity_already_ended")
    if rule.review_due_at <= now:
        errors.append("rule_review_due_already_passed")
    errors.extend(await _check_selector(session, rule.selector))
    source_diagnostics, source_dependencies = await _check_source_refs(session, owner_id, rule, now)
    for diagnostic in source_diagnostics:
        if diagnostic.startswith(
            (
                "source_reference_requires_runtime_check:",
                "source_evidence_not_yet_saved:",
                "source_evidence_not_visible_yet:",
            )
        ):
            notices.append(diagnostic)
        else:
            errors.append(diagnostic)

    factor_dependencies: list[dict[str, str]] = []
    factors_by_name: dict[str, Any] = {}
    for factor_name in _metric_factors(rule.definition):
        try:
            factor = DEFAULT_REGISTRY.get(factor_name)
        except KeyError:
            errors.append(f"unknown_factor:{factor_name}")
            continue
        factors_by_name[factor_name] = factor
        factor_dependencies.append(
            {"name": factor.name, "version": factor.version, "unit": factor.unit}
        )
    for leaf in _metric_leaves(rule.definition):
        factor_metadata = factors_by_name.get(leaf.factor_id)
        if factor_metadata is None:
            continue
        if leaf.unit is not None and leaf.unit != factor_metadata.unit:
            errors.append(
                f"factor_unit_mismatch:{leaf.factor_id}:{leaf.unit}:{factor_metadata.unit}"
            )
        if leaf.params:
            errors.append(f"unsupported_metric_parameters:{leaf.factor_id}")
    if rule.selector.kind == "category" and any(
        term.kind in {"product", "region"} and not term.complete
        for term in _category_terms(rule.selector.expression)
    ):
        notices.append("category_membership_is_incomplete")

    content_hash = rule_content_hash(rule)
    semantic_hash = rule_semantic_hash(rule)
    dependency_fingerprint = canonical_hash(
        {
            "validator_version": VALIDATOR_VERSION,
            "sources": source_dependencies,
            "factors": factor_dependencies,
        }
    )
    expires_at = now + VALIDATION_LIFETIME
    outcome: Literal["pass", "fail"] = "fail" if errors else "pass"
    diagnostics = tuple(dict.fromkeys((*errors, *notices)))
    validation_hash = canonical_hash(
        {
            "rule_ref": ref.model_dump(mode="json"),
            "content_hash": content_hash,
            "validator_version": VALIDATOR_VERSION,
            "dependency_fingerprint": dependency_fingerprint,
            "validated_at": now,
            "expires_at": expires_at,
            "committed_head_revision": committed_head_revision,
        }
    )
    return ValidationOutcome(
        outcome=outcome,
        rule_ref=ref,
        content_hash=content_hash,
        semantic_hash=semantic_hash,
        validation_hash=validation_hash,
        validator_version=VALIDATOR_VERSION,
        dependency_fingerprint=dependency_fingerprint,
        validated_at=now,
        expires_at=expires_at,
        checked_head_revision=expected_head_revision,
        committed_head_revision=committed_head_revision,
        diagnostics=diagnostics,
    )

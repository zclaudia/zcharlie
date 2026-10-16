"""Pure, evidence-preserving selector matching for supplemental rules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.factors.industry_groups import default_industry_groups
from investment_agent.investor.common import ObjectRef
from investment_agent.providers.shenwan_directory import default_shenwan_directory
from investment_agent.supplemental_rules.contracts import (
    CategoryExpression,
    CategorySelector,
    CompanySelector,
    FrozenSubject,
    IndustryCategoryTerm,
    ListingSelector,
    MatchStatus,
    MembershipCategoryTerm,
    Selector,
)

MATCHER_VERSION = "supplemental-matcher-v1"


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    leaves: tuple[dict[str, Any], ...]
    diagnostics: tuple[str, ...]
    manifest_hash: str

    @property
    def reason(self) -> str:
        return ";".join(self.diagnostics)


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _proof(subject: FrozenSubject, source_ref: ObjectRef) -> tuple[bool, str | None]:
    """A selector source must be present as exact, verified, conflict-free evidence."""
    ref_data = source_ref.model_dump(mode="json")
    for item in subject.evidence:
        is_exact_evidence = (
            source_ref.kind == "evidence"
            and str(item.get("id")) == str(source_ref.id)
            and item.get("revision") == source_ref.revision
        )
        is_exact_source = item.get("source_ref") == ref_data
        if is_exact_evidence or is_exact_source:
            if item.get("verification") != "verified":
                return False, "selector_source_not_verified"
            if item.get("conflict") != "none":
                return False, "selector_source_conflicted"
            if source_ref.kind == "evidence":
                manifest = next((row for row in subject.source_manifest
                    if row.get("kind") == "saved_evidence" and row.get("ref") == ref_data), None)
                if manifest is None:
                    return False, "selector_source_read_status_unavailable"
                if manifest.get("read_status") != "ok" or manifest.get("read_warnings"):
                    return False, "selector_source_limited_at_read"
            return True, None
    return False, "selector_source_unavailable"


def _industry_leaf(term: IndustryCategoryTerm, subject: FrozenSubject) -> dict[str, Any]:
    proof_ok, proof_issue = _proof(subject, term.source_ref)
    if not proof_ok:
        return {"term": term.model_dump(mode="json"), "status": "unknown",
                "reason": proof_issue or "selector_source_unavailable", "evidence": []}
    if term.system == "sw2021":
        expected_version: str | None = default_shenwan_directory().version
    elif term.system == "csrc":
        groups = default_industry_groups()
        expected_version = groups.version if groups.system == "csrc" else None
    else:
        expected_version = None
    if expected_version is None or term.scheme_version != expected_version:
        return {"term": term.model_dump(mode="json"), "status": "unknown",
                "reason": "unsupported_classification_system_or_version", "evidence": []}
    rows = [row for row in subject.selector_inputs.get("classifications", [])
            if row.get("system") == term.system]
    cutoff_day = subject.as_of.astimezone(SHANGHAI_TZ).date()
    current: list[dict[str, Any]] = []
    for row in rows:
        try:
            valid_from = date.fromisoformat(str(row["valid_from"]))
            valid_to = date.fromisoformat(str(row["valid_to"])) if row.get("valid_to") else None
        except (KeyError, TypeError, ValueError):
            return {"term": term.model_dump(mode="json"), "status": "unknown",
                    "reason": "classification_interval_unavailable", "evidence": [row]}
        if valid_from <= cutoff_day and (valid_to is None or valid_to > cutoff_day):
            current.append(row)

    if any(diagnostic.startswith(("classification_rows_truncated", "classification_row_mutated_after_cutoff",
                                 "classification_row_persisted_after_cutoff"))
           for diagnostic in subject.diagnostics):
        return {"term": term.model_dump(mode="json"), "status": "unknown",
                "reason": "classification_source_coverage_unknown", "evidence": current or rows}

    # Historical rows can prove a code during their interval, but their mere
    # presence cannot prove that an expired interval means the subject is
    # outside every category now. Absence of a current row is missing temporal
    # coverage, not a negative classification.
    if not current:
        return {"term": term.model_dump(mode="json"), "status": "unknown",
                "reason": "classification_interval_not_covering_as_of", "evidence": rows}

    codes = {row.get("code") for row in current}
    # A system has one dated classification. Distinct concurrent CSRC codes
    # contradict that history. Shenwan may expose ancestor and descendant codes
    # together; those are compatible in the versioned directory, while unrelated
    # current codes remain an explicit conflict.
    if len(codes) > 1:
        if term.system != "sw2021":
            return {"term": term.model_dump(mode="json"), "status": "unknown",
                    "reason": "classification_source_conflicted", "evidence": current}
        directory = default_shenwan_directory()
        code_values = [code for code in codes if isinstance(code, str)]
        compatible = all(
            left in {item.code for item in directory.chain(right)}
            or right in {item.code for item in directory.chain(left)}
            for index, left in enumerate(code_values)
            for right in code_values[index + 1:]
        ) and len(code_values) == len(codes)
        if not compatible:
            return {"term": term.model_dump(mode="json"), "status": "unknown",
                    "reason": "classification_source_conflicted", "evidence": current}

    matching = [row for row in current if row.get("code") == term.code]
    if term.system == "sw2021":
        directory = default_shenwan_directory()
        matching.extend(row for row in current
                        if isinstance(row.get("code"), str)
                        and term.code in {item.code for item in directory.chain(row["code"])}
                        and row not in matching)
    if matching:
        return {"term": term.model_dump(mode="json"), "status": "applicable",
                "reason": "exact_or_hierarchical_system_version_code_match", "evidence": matching}
    if not rows:
        return {"term": term.model_dump(mode="json"), "status": "unknown",
                "reason": "classification_source_missing", "evidence": []}
    return {"term": term.model_dump(mode="json"),
            "status": "not_applicable",
            "reason": "exact_code_absent", "evidence": current or rows}


def _membership_leaf(term: MembershipCategoryTerm, subject: FrozenSubject) -> dict[str, Any]:
    proof_ok, proof_issue = _proof(subject, term.source_ref)
    if not proof_ok:
        return {"term": term.model_dump(mode="json"), "status": "unknown",
                "reason": proof_issue or "selector_source_unavailable", "evidence": []}
    # The current saved-source adapters have no typed company-to-product or
    # company-to-region membership relation. A complete list describes the
    # selector's domain; it does not prove this company's membership or absence.
    return {"term": term.model_dump(mode="json"), "status": "unknown",
            "reason": "subject_membership_adapter_unavailable",
            "evidence": [{"source_ref": term.source_ref.model_dump(mode="json"),
                          "complete": term.complete, "members": list(term.members)}]}


def _combine(operator: Literal["all", "any"], statuses: list[MatchStatus]) -> MatchStatus:
    if operator == "all":
        if "not_applicable" in statuses:
            return "not_applicable"
        return "unknown" if "unknown" in statuses else "applicable"
    if "applicable" in statuses:
        return "applicable"
    return "unknown" if "unknown" in statuses else "not_applicable"


def match_selector(selector: Selector, subject: FrozenSubject) -> MatchResult:
    """Resolve listing/company/category selectors without heuristic matching."""
    leaves: list[dict[str, Any]] = []
    if isinstance(selector, ListingSelector):
        status: MatchStatus = (
            "applicable" if selector.listing_ref == subject.listing_ref else "not_applicable"
        )
        leaves.append({"term": selector.model_dump(mode="json"), "status": status,
                       "reason": "exact_listing_identity", "evidence": [subject.selector_inputs.get("listing", {})]})
    elif isinstance(selector, CompanySelector):
        if not subject.selector_inputs.get("identity_available", True):
            status = "unknown"
            reason = "company_listing_mapping_not_visible_at_cutoff"
        else:
            status = "applicable" if selector.company_id == subject.company_id else "not_applicable"
            reason = "exact_company_id"
        leaves.append({"term": selector.model_dump(mode="json"), "status": status,
                       "reason": reason, "evidence": [subject.selector_inputs.get("company", {})]})
    elif isinstance(selector, CategorySelector):
        def visit(expression: CategoryExpression) -> MatchStatus:
            local: list[MatchStatus] = []
            for term in expression.terms:
                if isinstance(term, CategoryExpression):
                    local.append(visit(term))
                else:
                    leaf = (_industry_leaf(term, subject) if isinstance(term, IndustryCategoryTerm)
                            else _membership_leaf(term, subject))
                    leaves.append(leaf)
                    local.append(leaf["status"])
            return _combine(expression.operator, local)

        status = visit(selector.expression)
    else:  # pragma: no cover - Pydantic discriminator makes this unreachable.
        raise TypeError(f"unsupported selector type: {type(selector).__name__}")
    body = {"version": MATCHER_VERSION, "selector": selector.model_dump(mode="json"),
            "status": status, "leaves": leaves}
    diagnostics = tuple(dict.fromkeys(
        leaf["reason"] for leaf in leaves if leaf["status"] == "unknown"
    ))
    return MatchResult(status, tuple(leaves), diagnostics, _hash(body))


def match_subject(selector: Selector, subject: FrozenSubject) -> MatchResult:
    """Freeze-time convenience name consumed by P2 admission/orchestration."""
    return match_selector(selector, subject)

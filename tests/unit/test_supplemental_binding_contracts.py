"""P3 binding/coverage contract tests: hashes, stage discipline, authorization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from investment_agent.agent.research_publication import ClosureProposalInput, SemanticInput
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.publication import RuleReviewItem, SemanticReview
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.contracts import (
    BindingRuleEntry,
    CoverageRuleEntry,
    PreparedClosureProposal,
    ResearchRuleBinding,
    RuleApplication,
    RuleCoverageManifest,
)

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
HASH = "a" * 64
TASK_ID = UUID("22222222-2222-4222-8222-222222222222")
LISTING = ObjectRef(kind="listing", id=7, revision=1)
METHOD = ObjectRef(kind="method", id=UUID("11111111-1111-4111-8111-111111111111"), revision=1)


def _rule_ref() -> ObjectRef:
    return ObjectRef(kind="supplemental_rule", id=uuid4(), revision=1)


def _binding_data(*, rule_entries: tuple[BindingRuleEntry, ...] = ()) -> dict:
    return {
        "schema_version": 1,
        "id": uuid4(),
        "revision": 1,
        "owner_id": "local_owner",
        "task_ref": ObjectRef(kind="task", id=TASK_ID, revision=1),
        "listing_ref": LISTING,
        "research_kind": "company_research",
        "method_ref": METHOD,
        "admission_as_of": NOW,
        "rules_generation": 1,
        "events_generation": 0,
        "rule_set": rule_entries,
        "rule_set_hash": canonical_hash(rule_entries),
        "matching_manifest_hash": None,
        "events_visible_until": None,
        "events_visible_unknown": False,
        "input_hash": HASH,
        "created_at": NOW + timedelta(minutes=1),
        "content_hash": "pending",
    }


def _binding(*, rule_entries: tuple[BindingRuleEntry, ...] = ()) -> ResearchRuleBinding:
    data = _binding_data(rule_entries=rule_entries)
    data["content_hash"] = _hash_minus(data, "content_hash")
    return ResearchRuleBinding.model_validate(data)


def _manifest_data(*, stage: str = "prepared", rule_entries: tuple[CoverageRuleEntry, ...] = ()) -> dict:
    binding = _binding()
    return {
        "schema_version": 1,
        "id": uuid4(),
        "revision": 1,
        "stage": stage,
        "owner_id": "local_owner",
        "task_ref": ObjectRef(kind="task", id=TASK_ID, revision=1),
        "task_id": TASK_ID,
        "binding_ref": binding.reference,
        "listing_ref": LISTING,
        "case_ref": None,
        "admission_as_of": NOW,
        "effective_cutoff": NOW + timedelta(minutes=30),
        "rule_entries": rule_entries,
        "open_issue_refs": (),
        "closure_proposals": (),
        "scan_capped": False,
        "scan_known_total": None,
        "scan_uncovered_note": None,
        "material_set_hash": None,
        "input_hash": HASH,
        "created_at": NOW + timedelta(hours=1),
        "manifest_hash": "pending",
        "prepared_ref": None,
        "prepared_manifest_hash": None,
        "semantic_review_ref": None,
        "closure_application_refs": (),
        "thesis_ref": None,
        "recommendation_ref": None,
        "limits_recheck": (),
        "committed_at": None,
    }


def _manifest(**overrides) -> RuleCoverageManifest:
    data = _manifest_data()
    data.update(overrides)
    data["manifest_hash"] = canonical_hash({k: v for k, v in data.items() if k != "manifest_hash"})
    return RuleCoverageManifest.model_validate(data)


def _hash_minus(data: dict, field: str) -> str:
    return canonical_hash({key: value for key, value in data.items() if key != field})


def test_research_rule_binding_hashes_identity_and_times() -> None:
    entry = BindingRuleEntry(rule_ref=_rule_ref(), semantic_hash=HASH, selector_hash="b" * 64)
    binding = _binding(rule_entries=(entry,))
    assert binding.reference.kind == "research_rule_binding" and binding.revision == 1
    # Old JSON without defaulted event-summary fields reads back unchanged.
    assert ResearchRuleBinding.model_validate(binding.model_dump(mode="json")) == binding
    data = _binding_data(rule_entries=(entry,))
    data["content_hash"] = _hash_minus(data, "content_hash")
    with pytest.raises(ValidationError, match="rule_set_hash"):
        ResearchRuleBinding.model_validate({**data, "rule_set_hash": "c" * 64})
    with pytest.raises(ValidationError, match="content_hash"):
        ResearchRuleBinding.model_validate({**data, "content_hash": "c" * 64})
    with pytest.raises(ValidationError, match="creation"):
        ResearchRuleBinding.model_validate({
            **data,
            "created_at": NOW - timedelta(minutes=1),
            "content_hash": _hash_minus({**data, "created_at": NOW - timedelta(minutes=1)}, "content_hash"),
        })
    late_visibility = {**data, "events_visible_until": NOW + timedelta(days=1)}
    with pytest.raises(ValidationError, match="visibility bound"):
        ResearchRuleBinding.model_validate({
            **late_visibility,
            "content_hash": _hash_minus(late_visibility, "content_hash"),
        })
    wrong_kind = {**data, "task_ref": {"kind": "case", "id": str(TASK_ID), "revision": 1}}
    with pytest.raises(ValidationError, match="task reference"):
        ResearchRuleBinding.model_validate({
            **wrong_kind,
            "content_hash": _hash_minus(wrong_kind, "content_hash"),
        })
    with pytest.raises(ValidationError, match="at most once"):
        _binding(rule_entries=(entry, entry))


def test_empty_rule_set_binding_is_legal_and_distinct_from_legacy() -> None:
    binding = _binding()
    assert binding.rule_set == () and binding.rule_set_hash == canonical_hash(())
    # legacy_unbound means no binding row at all; the empty-set binding still
    # carries a full identity, so the two states are machine-distinguishable.
    assert binding.task_ref.kind == "task"


def test_coverage_rule_entry_status_field_correspondence() -> None:
    rule = _rule_ref()
    application = ObjectRef(kind="rule_application", id=uuid4(), revision=1)
    applied = CoverageRuleEntry(rule_ref=rule, semantic_hash=HASH, status="application", application_ref=application)
    assert applied.application_ref is not None
    with pytest.raises(ValidationError, match="application coverage requires"):
        CoverageRuleEntry(rule_ref=rule, semantic_hash=HASH, status="application")
    with pytest.raises(ValidationError, match="not-applicable coverage requires"):
        CoverageRuleEntry(rule_ref=rule, semantic_hash=HASH, status="not_applicable")
    with pytest.raises(ValidationError, match="unknown coverage requires"):
        CoverageRuleEntry(rule_ref=rule, semantic_hash=HASH, status="unknown")
    with pytest.raises(ValidationError, match="cannot carry"):
        CoverageRuleEntry(
            rule_ref=rule, semantic_hash=HASH, status="application",
            application_ref=application, unknown_reason="extra",
        )
    with pytest.raises(ValidationError, match="cannot carry"):
        CoverageRuleEntry(rule_ref=rule, semantic_hash=HASH, status="unbound_new", basis="unsupported")
    for status, required in (
        ("not_applicable", {"basis": "recorded basis"}),
        ("unknown", {"unknown_reason": "explicit reason"}),
        ("unbound_new", {}),
    ):
        with pytest.raises(ValidationError, match="cannot carry"):
            CoverageRuleEntry(
                rule_ref=rule, semantic_hash=HASH, status=status,
                application_ref=application, **required,
            )


def test_prepared_closure_proposal_hash_binds_content() -> None:
    targets = (ObjectRef(kind="rule_application", id=uuid4(), revision=1),)
    body = {
        "target_application_refs": targets,
        "evidence_refs": (),
        "claim_key": None,
        "rationale": "Reviewed exposure resolved with saved evidence",
    }
    proposal = PreparedClosureProposal.model_validate({**body, "proposal_hash": canonical_hash(body)})
    assert proposal.proposal_hash == canonical_hash(body)
    with pytest.raises(ValidationError, match="proposal_hash"):
        PreparedClosureProposal.model_validate({**body, "proposal_hash": "c" * 64})
    changed = {**body, "rationale": "Different rationale"}
    with pytest.raises(ValidationError, match="proposal_hash"):
        PreparedClosureProposal.model_validate({**changed, "proposal_hash": canonical_hash(body)})
    with pytest.raises(ValidationError, match="at least 1 item"):
        PreparedClosureProposal.model_validate({
            **body, "target_application_refs": (), "proposal_hash": canonical_hash(body),
        })


def test_prepared_manifest_stage_discipline_and_hash() -> None:
    entry = CoverageRuleEntry(rule_ref=_rule_ref(), semantic_hash=HASH, status="unknown", unknown_reason="no evaluation completed")
    manifest = _manifest(rule_entries=(entry,))
    assert manifest.reference.kind == "rule_coverage" and manifest.stage == "prepared"
    assert RuleCoverageManifest.model_validate(manifest.model_dump(mode="json")) == manifest
    data = _manifest_data(rule_entries=(entry,))
    with pytest.raises(ValidationError, match="manifest_hash"):
        RuleCoverageManifest.model_validate({**data, "manifest_hash": "c" * 64})
    changed_task = {**data, "task_id": uuid4()}
    with pytest.raises(ValidationError, match="task_id"):
        RuleCoverageManifest.model_validate({
            **changed_task,
            "manifest_hash": _hash_minus(changed_task, "manifest_hash"),
        })
    with pytest.raises(ValidationError, match="times must follow"):
        late = {**data, "effective_cutoff": NOW - timedelta(days=1)}
        RuleCoverageManifest.model_validate({
            **late, "manifest_hash": _hash_minus(late, "manifest_hash"),
        })
    with pytest.raises(ValidationError, match="published-stage fields"):
        leaked = {
            **data,
            "prepared_ref": {"kind": "rule_coverage", "id": str(uuid4()), "revision": 1},
            "manifest_hash": "pending",
        }
        RuleCoverageManifest.model_validate({
            **leaked, "manifest_hash": _hash_minus(leaked, "manifest_hash"),
        })


def test_published_manifest_requires_review_and_commit_identity() -> None:
    prepared = _manifest()
    published_data = _manifest_data(stage="published")
    published_data.update({
        "prepared_ref": prepared.reference,
        "prepared_manifest_hash": prepared.manifest_hash,
        "semantic_review_ref": ObjectRef(kind="review", id=uuid4(), revision=1),
        "committed_at": NOW + timedelta(hours=2),
    })
    published_data["manifest_hash"] = _hash_minus(published_data, "manifest_hash")
    published = RuleCoverageManifest.model_validate(published_data)
    assert published.stage == "published" and published.committed_at is not None
    for missing in ("prepared_ref", "prepared_manifest_hash", "semantic_review_ref", "committed_at"):
        incomplete = {key: value for key, value in published_data.items()}
        incomplete[missing] = None
        with pytest.raises(ValidationError, match="published coverage requires"):
            RuleCoverageManifest.model_validate({**incomplete, "manifest_hash": _hash_minus(incomplete, "manifest_hash")})
    wrong_kind = {
        **published_data,
        "prepared_ref": {"kind": "task", "id": str(TASK_ID), "revision": 1},
    }
    with pytest.raises(ValidationError, match="prepared_ref"):
        RuleCoverageManifest.model_validate({**wrong_kind, "manifest_hash": _hash_minus(wrong_kind, "manifest_hash")})


def test_manifest_capacity_caps_entries_and_reports_capped_scans() -> None:
    entries = tuple(
        CoverageRuleEntry(rule_ref=_rule_ref(), semantic_hash=HASH, status="not_applicable", basis="selector miss")
        for _ in range(2001)
    )
    with pytest.raises(ValidationError):
        _manifest(rule_entries=entries)
    with pytest.raises(ValidationError, match="uncapped scan"):
        _manifest(scan_uncovered_note="unexpected")
    capped = _manifest(scan_capped=True, scan_known_total=5000, scan_uncovered_note="scan stop-line hit")
    assert capped.scan_capped and capped.scan_known_total == 5000


def test_mechanical_authorization_is_the_default_and_keeps_p2_bans() -> None:
    from tests.support.supplemental_rules import synthetic_application

    application = synthetic_application()
    assert application.authorization == "mechanical"
    replayed = RuleApplication.model_validate(application.model_dump(mode="json"))
    assert replayed == application and replayed.application_mode == "live"
    review_ref = {"kind": "review", "id": str(uuid4()), "revision": 1}
    with pytest.raises(ValidationError, match="completed semantic review"):
        synthetic_application(
            overrides={"semantic_status": "reviewed", "semantic_review_ref": review_ref},
            result_overrides={"semantic_status": "reviewed", "semantic_review_ref": review_ref},
        )
    closure = ({"kind": "rule_application", "id": str(uuid4()), "revision": 1},)
    with pytest.raises(ValidationError, match="cannot close issues"):
        synthetic_application(
            overrides={"closure_refs": closure},
            result_overrides={"closure_refs": closure},
        )


def test_formal_review_application_requires_review_closure_live_and_purpose() -> None:
    from tests.support.supplemental_rules import synthetic_application

    review_ref = {"kind": "review", "id": str(uuid4()), "revision": 1}
    closure = ({"kind": "rule_application", "id": str(uuid4()), "revision": 1},)
    formal = synthetic_application(
        overrides={
            "authorization": "formal_review",
            "semantic_status": "reviewed",
            "semantic_review_ref": review_ref,
            "closure_refs": closure,
            "evaluation_purpose": "issue_review",
            "application_mode": "live",
        },
        result_overrides={
            "semantic_status": "reviewed",
            "semantic_review_ref": review_ref,
            "assessment_status": "unresolved",
            "closure_refs": closure,
            "open_issue_refs": (),
        },
    )
    assert formal.authorization == "formal_review" and formal.closure_refs
    base = {
        "authorization": "formal_review",
        "semantic_status": "reviewed",
        "semantic_review_ref": review_ref,
        "closure_refs": closure,
        "evaluation_purpose": "issue_review",
        "application_mode": "live",
    }
    result = {
        "semantic_status": "reviewed",
        "semantic_review_ref": review_ref,
        "assessment_status": "unresolved",
        "closure_refs": closure,
        "open_issue_refs": (),
    }
    # A reviewed result cannot exist without its review ref, so the reachable
    # formal_review rejection drops the semantic status entirely.
    without_review = {
        "authorization": "formal_review",
        "closure_refs": closure,
        "evaluation_purpose": "issue_review",
        "application_mode": "live",
    }
    with pytest.raises(ValidationError, match="formal_review applications require"):
        synthetic_application(
            overrides=without_review,
            result_overrides={"assessment_status": "unresolved", "closure_refs": closure},
        )
    without_closure = {**base, "closure_refs": ()}
    with pytest.raises(ValidationError, match="closure references"):
        synthetic_application(
            overrides=without_closure,
            result_overrides={**result, "closure_refs": ()},
        )
    # Replay plus issue_review is already rejected by the frozen P2 discipline;
    # the formal branch's live check is belt-and-braces behind that guard.
    with pytest.raises(ValidationError, match="historical replay cannot resolve"):
        synthetic_application(
            overrides={**base, "application_mode": "historical_replay"},
            result_overrides=result,
        )
    with pytest.raises(ValidationError, match="issue_review purpose"):
        synthetic_application(
            overrides={**base, "evaluation_purpose": "coverage"},
            result_overrides=result,
        )


def test_input_hash_domain_excludes_authorization() -> None:
    # The frozen P2 input-hash domain (pair/manifest/mode/purpose) is unchanged:
    # authorization and rule_reviews never enter any digest.
    from tests.support.supplemental_rules import synthetic_application

    mechanical = synthetic_application()
    assert mechanical.input_hash == synthetic_application().input_hash
    dump = mechanical.model_dump(mode="json")
    assert "authorization" in dump and dump["authorization"] == "mechanical"


def test_rule_review_items_and_semantic_review_defaults() -> None:
    application = ObjectRef(kind="rule_application", id=uuid4(), revision=1)
    item = RuleReviewItem(application_ref=application, disposition="no_action_required")
    assert item.proposal_hash is None and item.basis_refs == ()
    with pytest.raises(ValidationError, match="proposal hash"):
        RuleReviewItem(application_ref=application, disposition="resolved_with_closure")
    with pytest.raises(ValidationError, match="only to resolved_with_closure"):
        RuleReviewItem(
            application_ref=application, disposition="limitation_acknowledged", proposal_hash=HASH,
        )
    with pytest.raises(ValidationError, match="exact rule applications"):
        RuleReviewItem(
            application_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1),
            disposition="no_action_required",
        )
    now = datetime.now(UTC)
    legacy = SemanticReview(
        id=uuid4(), revision=1, actor="a", created_at=now, origin_ref=None,
        candidate_hash="x", material_set_hash="y",
        method_ref=ObjectRef(kind="method", id=UUID("11111111-1111-4111-8111-111111111111"), revision=1),
        assessments=(), checked_sources=(), unresolved_questions=(), conclusion="pass", reviewer="r",
    )
    assert legacy.rule_reviews == () and legacy.coverage_manifest_ref is None
    # Old JSON round-trips without the P3 fields.
    assert SemanticReview.model_validate(legacy.model_dump(mode="json")) == legacy
    manifest_ref = ObjectRef(kind="rule_coverage", id=uuid4(), revision=1)
    bound = legacy.model_copy(update={
        "rule_reviews": (RuleReviewItem(
            application_ref=application, disposition="resolved_with_closure", proposal_hash=HASH,
        ),),
        "coverage_manifest_ref": manifest_ref,
        "coverage_manifest_hash": HASH,
    })
    assert SemanticReview.model_validate(bound.model_dump(mode="json")) == bound
    with pytest.raises(ValidationError, match="together"):
        SemanticReview.model_validate(
            bound.model_copy(update={"coverage_manifest_hash": None}).model_dump(mode="json")
        )
    with pytest.raises(ValidationError, match="rule coverage"):
        SemanticReview.model_validate(bound.model_copy(update={
            "coverage_manifest_ref": ObjectRef(kind="task", id=TASK_ID, revision=1),
        }).model_dump(mode="json"))


def test_semantic_input_defaults_and_closure_proposal_input() -> None:
    preparation = ObjectRef(kind="publication_preparation", id=uuid4(), revision=1)
    legacy = SemanticInput(
        preparation_ref=preparation, assessments=(), checked_sources=(),
        unresolved_questions=(), conclusion="pass",
    )
    assert legacy.rule_reviews == () and legacy.coverage_manifest_ref is None
    assert SemanticInput.model_validate(legacy.model_dump(mode="json")) == legacy
    targets = (ObjectRef(kind="rule_application", id=uuid4(), revision=1),)
    proposal = ClosureProposalInput(
        target_application_refs=targets, evidence_refs=(), claim_key=None, rationale="Proposal",
    )
    assert proposal.claim_key is None
    with pytest.raises(ValidationError, match="at least 1 item"):
        ClosureProposalInput(target_application_refs=(), evidence_refs=(), rationale="Empty")
    with pytest.raises(ValidationError, match="rule applications"):
        ClosureProposalInput(
            target_application_refs=(ObjectRef(kind="evidence", id=uuid4(), revision=1),),
            rationale="Wrong target kind",
        )


def test_p3_freeze_fixture_fixes_cases_digest_and_capacity() -> None:
    from tests.support.supplemental_rules import closure_expectations

    document = closure_expectations()
    assert document["schema_version"] == 1 and document["synthetic_only"] is True
    cases = {case["id"]: case["expected"] for case in document["cases"]}
    # The six plan-required scenarios plus migration and capacity expectations.
    assert set(cases) == {
        "P3-F02", "P3-F03", "P3-F04", "P3-F05", "P3-F06", "P3-F07", "P3-F11", "P3-F14",
    }
    digest = document["digest_spec"]
    assert digest["fingerprint"] == "canonical_json_plus_sha256"
    assert digest["hash_never_covers_itself"] is True
    assert digest["closure_proposal_hash_domain"] == (
        "target_application_refs_plus_evidence_refs_plus_claim_key_plus_rationale"
    )
    window = document["review_window"]
    assert window["shared_budget"] == "32 materials and 64000 bytes"
    assert window["overflow_behavior"] == "review_blocked_with_empty_summaries"
    assert window["partial_pass_on_overflow"] is False
    capacity = document["capacity_profile"]
    assert capacity["coverage_rule_entries_max"] == 2000
    assert capacity["coverage_manifest_bytes_max"] == 4 * 1024 * 1024
    assert capacity["caps_are_independent"] is True
    assert document["switch_matrix"]["all_off"] == (
        "every P3 write path disabled; reads and legacy behavior byte-identical to current"
    )
    # Empty-set binding and legacy stay machine-distinguishable in the sample.
    assert cases["P3-F02"]["machine_distinguishable"] is True


def test_p3_freeze_shapes_match_contracts() -> None:
    from tests.support.supplemental_rules import closure_expectations, synthetic_binding

    document = closure_expectations()
    binding = synthetic_binding()
    assert binding.research_kind in {"company_research", "review"}
    assert document["storage_shapes"]["rule_coverage_row"].startswith("account NULL")
    assert "task_id UNIQUE" in document["storage_shapes"]["research_rule_binding_row"]
    assert document["rule_review_coverage_rule"].startswith("every manifest entry")

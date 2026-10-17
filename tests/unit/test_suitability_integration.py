"""AF-P3-08 unit: fit composition truth table, legacy compatibility, ranking projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.candidates.ranking import (
    FILTERS,
    build_ranking_entries,
    page_ranking_entries,
    research_urgency_level,
)
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.enums import REASON_LABELS, CompanyAction
from investment_agent.portfolio.suitability import (
    AccountFitService,
    company_alignment_of,
    compose_supplemental_fit,
)
from investment_agent.portfolio.suitability_calculations import supplemental_bucket
from investment_agent.portfolio.suitability_contracts import AccountFitAssessment
from investment_agent.storage.repositories.investment_assessment import RuleBindingState
from tests.support.account_fit import assumptions
from tests.unit.test_candidate_ranking import _entry, _manifest

AS_OF = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
HASH = "a" * 64


def _fit(**overrides) -> AccountFitAssessment:
    """A minimal valid fit row; overrides patch the composed fields."""

    account_id = uuid4()
    listing_ref = ObjectRef(kind="listing", id=101, revision=1)
    operation_id = uuid4()
    fit_id = uuid4()
    from investment_agent.investor.contracts import Mandate, ResearchAuthorization
    from investment_agent.portfolio.contracts import Snapshot
    from investment_agent.portfolio.suitability_contracts import (
        FitRequestItem,
        FrozenAccountInputs,
        FrozenListingSource,
        FrozenPrice,
        FrozenSector,
        PurposeBinding,
        ValuationBinding,
    )

    snapshot_id = uuid4()
    snapshot = Snapshot(
        id=snapshot_id, account_id=account_id, revision=1, recorded_at=AS_OF,
        fact_as_of=AS_OF, valuation_at=AS_OF, coverage="complete",
        asset_scope="synthetic", known_cash=Decimal("1000"), cash_subtotal=Decimal("1000"),
        holdings=(), known_subtotal=Decimal("1000"), total_assets=Decimal("1000"),
        unknowns=(), historical_unknowns=(), reconciliations=(), conflicts=(),
        source_event_ids=(), excluded_prior_event_ids=(),
        reference=ObjectRef(kind="portfolio_snapshot", id=snapshot_id, revision=1),
        price_coverage="complete",
    )
    mandate = Mandate(
        id=uuid4(), schema_version=1, revision=1, created_at=AS_OF, actor="test",
        origin_ref=None, goals=(), scope_refs=(), hard_constraints=(), unknowns=(),
        authorization=ResearchAuthorization(
            daily_budget=Quantity(value=Decimal("1"), unit="token"),
            per_task_budget=Quantity(value=Decimal("1"), unit="token"),
            max_leads_per_batch=0, max_new_tasks_per_batch=0, source_refs=(),
        ),
    )
    frozen_listing = FrozenListingSource(
        listing_ref=listing_ref, market_code="SSE", name="Synthetic", ticker="600000",
        listing_source_ref=listing_ref,
        price=FrozenPrice(listing_ref=listing_ref, price=None, price_ref=None, price_date=None,
                          provider=None, captured_at=None, available_at=None, acquired_at=None,
                          unknown_reason="no_price"),
        sector=FrozenSector(listing_ref=listing_ref, row_id=None, code=None, name=None,
                            valid_from=None, valid_to=None, source=None, captured_at=None,
                            available_at=None, acquired_at=None, unknown_reason="no_sector"),
        trusted_rule=None, rule_unknown_reason="no_trusted_rule",
    )
    frozen_inputs = FrozenAccountInputs(
        account_id=account_id, snapshot=snapshot, valuation=None,
        valuation_binding=ValuationBinding(state="missing", reason="no_valuation"),
        mandate=mandate, purpose_version=None, purpose_binding=PurposeBinding(state="none_recorded"),
        listing_sources=(frozen_listing,), assessment_as_of=AS_OF, price_basis_at=None,
        mode="live", manifest_hash=HASH, source_fingerprint=HASH,
    )
    values = dict(
        id=fit_id,
        reference=ObjectRef(kind="account_fit", id=fit_id, revision=1),
        operation_id=operation_id,
        input_hash=HASH,
        account_id=account_id,
        listing_ref=listing_ref,
        action="open",
        request_item=FitRequestItem(listing_ref=listing_ref, action="open",
                                    trade_assumptions=assumptions()),
        purpose_binding_key=HASH,
        calculation_basis_key=HASH,
        assessment_as_of=AS_OF,
        price_basis_at=None,
        expires_at=AS_OF + timedelta(hours=1),
        frozen_inputs=frozen_inputs,
        financial_status="pass",
        checks=(),
        ranking_bucket="incomplete",
        coverage="complete",
    )
    values.update(overrides)
    return AccountFitAssessment(**values)


def test_legacy_row_json_round_trip_is_unchanged():
    fit = _fit()
    data = fit.model_dump(mode="json")
    assert data["supplemental_binding"] == "not_integrated"
    assert data["research_status"] == "not_assessed"
    assert data["company_alignment"] == "not_assessed"
    assert data["coverage_ref"] is None and data["application_refs"] == []
    restored = AccountFitAssessment.model_validate(data)
    assert restored.model_dump(mode="json") == data


def test_bound_requires_exact_refs_generations_and_no_reason():
    coverage_ref = ObjectRef(kind="rule_coverage", id=uuid4(), revision=1)
    binding_ref = ObjectRef(kind="research_rule_binding", id=uuid4(), revision=1)
    bound = _fit(supplemental_binding="bound", coverage_ref=coverage_ref,
                 binding_ref=binding_ref, rules_generation=3, events_generation=5,
                 subject_set_hash=HASH, research_status="current",
                 company_alignment="supportive", ranking_bucket="ready")
    assert bound.supplemental_binding == "bound"
    with pytest.raises(ValidationError, match="bound supplemental binding"):
        _fit(supplemental_binding="bound", binding_ref=binding_ref,
             rules_generation=3, events_generation=5)
    with pytest.raises(ValidationError, match="bound supplemental binding"):
        AccountFitAssessment.model_validate({**bound.model_dump(mode="json"), "rules_generation": None})
    with pytest.raises(ValidationError, match="cannot carry an unknown reason"):
        AccountFitAssessment.model_validate({**bound.model_dump(mode="json"), "unknown_reason": "x"})


def test_unknown_requires_reason_and_carries_no_bound_detail():
    unknown = _fit(supplemental_binding="unknown", unknown_reason="rule_binding_missing")
    assert unknown.ranking_bucket == "incomplete"
    with pytest.raises(ValidationError, match="unknown supplemental binding requires"):
        _fit(supplemental_binding="unknown")
    with pytest.raises(ValidationError, match="cannot carry bound detail"):
        AccountFitAssessment.model_validate({
            **unknown.model_dump(mode="json"),
            "coverage_ref": {"kind": "rule_coverage", "id": str(uuid4()), "revision": 1},
        })


def test_not_integrated_cannot_carry_rule_detail_or_view_states():
    with pytest.raises(ValidationError, match="not_integrated rows carry no supplemental detail"):
        AccountFitAssessment.model_validate({
            **_fit().model_dump(mode="json"), "unknown_reason": "rule_binding_missing",
        })
    with pytest.raises(ValidationError, match="pre-integration view states"):
        AccountFitAssessment.model_validate({
            **_fit().model_dump(mode="json"), "research_status": "current",
        })


def _bound_payload() -> dict:
    """A fully integrated bound row payload for validator-targeted mutations."""

    return _fit(
        supplemental_binding="bound",
        coverage_ref=ObjectRef(kind="rule_coverage", id=uuid4(), revision=1),
        binding_ref=ObjectRef(kind="research_rule_binding", id=uuid4(), revision=1),
        rules_generation=3,
        events_generation=5,
        subject_set_hash=HASH,
        research_status="current",
        company_alignment="supportive",
        ranking_bucket="ready",
    ).model_dump(mode="json")


def test_ready_and_limited_require_finite_financial_and_ready_requires_full_support():
    base = _bound_payload()
    with pytest.raises(ValidationError, match="passing or warning financial"):
        AccountFitAssessment.model_validate({**base, "financial_status": "unknown"})
    with pytest.raises(ValidationError, match="ready requires current research"):
        AccountFitAssessment.model_validate({**base, "research_status": "review_required"})
    with pytest.raises(ValidationError, match="ready requires current research"):
        AccountFitAssessment.model_validate({**base, "coverage": "partial"})
    with pytest.raises(ValidationError, match="ready requires current research"):
        AccountFitAssessment.model_validate({**base, "company_alignment": "restrictive"})
    # limited tolerates every non-blocking caveat.
    limited = AccountFitAssessment.model_validate({**base, "financial_status": "warning",
                                                   "company_alignment": "restrictive",
                                                   "ranking_bucket": "limited"})
    assert limited.ranking_bucket == "limited"
    # blocked↔financial mutual exclusion is preserved untouched.
    with pytest.raises(ValidationError, match="blocked bucket requires"):
        AccountFitAssessment.model_validate({**_fit().model_dump(mode="json"), "ranking_bucket": "blocked"})
    with pytest.raises(ValidationError, match="blocked financial result"):
        AccountFitAssessment.model_validate({**base, "financial_status": "blocked"})


@pytest.mark.parametrize(
    ("financial", "coverage", "research", "alignment", "expected"),
    [
        # blocked stays a proven block.
        ("blocked", "complete", "current", "supportive", "blocked"),
        # Any necessary unknown is incomplete.
        ("unknown", "complete", "current", "supportive", "incomplete"),
        ("pass", "partial", "current", "supportive", "incomplete"),
        ("pass", "complete", "review_required", "supportive", "incomplete"),
        ("pass", "complete", "not_assessed", "supportive", "incomplete"),
        ("pass", "complete", "current", "undetermined", "incomplete"),
        ("pass", "complete", "current", "not_assessed", "incomplete"),
        # Known non-blocking caveats are limited.
        ("warning", "complete", "current", "supportive", "limited"),
        ("pass", "complete", "current", "restrictive", "limited"),
        ("warning", "complete", "current", "restrictive", "limited"),
        # Everything passes with supportive alignment: ready.
        ("pass", "complete", "current", "supportive", "ready"),
    ],
)
def test_supplemental_bucket_truth_table(financial, coverage, research, alignment, expected):
    assert supplemental_bucket(
        financial_status=financial, coverage=coverage,
        research_status=research, company_alignment=alignment,
    ) == expected


def _bound_state() -> RuleBindingState:
    manifest = SimpleNamespace(
        reference=ObjectRef(kind="rule_coverage", id=uuid4(), revision=1),
        binding_ref=ObjectRef(kind="research_rule_binding", id=uuid4(), revision=1),
        rule_entries=(),
    )
    binding = SimpleNamespace(rules_generation=7, events_generation=9, rule_set_hash=HASH)
    return RuleBindingState("bound", (), manifest, binding)


def _view(*, status: str = "published", reasons: tuple = (), business_view="favorable", actions="buy"):
    from investment_agent.investor.enums import BusinessView

    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        limitation_reasons=reasons,
        recommendation=SimpleNamespace(
            business_view=BusinessView(business_view),
            conditional_actions=tuple(
                SimpleNamespace(action=CompanyAction(action), condition_refs=(ObjectRef(kind="evidence", id=uuid4(), revision=1),))
                for action in ((actions,) if isinstance(actions, str) else actions)
            ),
        ),
    )


def test_company_alignment_is_conservative():
    assert company_alignment_of(_view(actions="buy").recommendation) == "supportive"
    assert company_alignment_of(_view(actions="avoid").recommendation) == "restrictive"
    assert company_alignment_of(_view(actions="sell").recommendation) == "restrictive"
    assert company_alignment_of(_view(business_view="unfavorable", actions="wait").recommendation) == "undetermined"
    # A current view with no directional action is never supportive.
    assert company_alignment_of(_view(actions="wait").recommendation) == "undetermined"
    assert company_alignment_of(_view(business_view="undetermined", actions="wait").recommendation) == "undetermined"


def test_compose_bound_ready_and_avoid_never_ready():
    fit = _fit()
    composed = compose_supplemental_fit(
        fit, state=_bound_state(), research_status="current",
        company_alignment="supportive", coverage_limits=(),
    )
    assert composed.supplemental_binding == "bound"
    assert composed.ranking_bucket == "ready"
    assert composed.coverage_ref is not None and composed.binding_ref is not None
    assert (composed.rules_generation, composed.events_generation) == (7, 9)
    assert composed.subject_set_hash == HASH

    restrictive = compose_supplemental_fit(
        fit, state=_bound_state(), research_status="current",
        company_alignment="restrictive", coverage_limits=(),
    )
    assert restrictive.ranking_bucket == "limited"
    assert restrictive.ranking_bucket != "ready"


def test_compose_coverage_limits_cap_status_and_bucket():
    composed = compose_supplemental_fit(
        _fit(), state=_bound_state(), research_status="current",
        company_alignment="supportive", coverage_limits=("rule_coverage_pending:abc",),
    )
    assert composed.research_status == "review_required"
    assert composed.ranking_bucket == "incomplete"


def test_compose_unknown_binding_is_incomplete_and_not_treated_as_zero_rules():
    state = RuleBindingState("unknown", ("rule_binding_missing",), None, None)
    composed = compose_supplemental_fit(
        _fit(), state=state, research_status="current",
        company_alignment="supportive", coverage_limits=(),
    )
    assert composed.supplemental_binding == "unknown"
    assert composed.unknown_reason == "rule_binding_missing"
    assert composed.ranking_bucket == "incomplete"
    # A binding without a published manifest names its own reason.
    unpublished = RuleBindingState("unknown", ("rule_coverage_unpublished",), None, None)
    assert compose_supplemental_fit(
        _fit(), state=unpublished, research_status="not_assessed",
        company_alignment="not_assessed", coverage_limits=(),
    ).unknown_reason == "rule_coverage_unpublished"
    # A bound state whose binding row vanished degrades honestly.
    missing = RuleBindingState("bound", (), SimpleNamespace(
        reference=ObjectRef(kind="rule_coverage", id=uuid4(), revision=1),
        binding_ref=ObjectRef(kind="research_rule_binding", id=uuid4(), revision=1),
        rule_entries=()), None)
    degraded = compose_supplemental_fit(
        _fit(), state=missing, research_status="current",
        company_alignment="supportive", coverage_limits=(),
    )
    assert degraded.supplemental_binding == "unknown"
    assert degraded.unknown_reason == "rule_coverage_binding_missing"
    assert degraded.ranking_bucket == "incomplete"


async def test_disabled_switches_keep_the_row_byte_identical(monkeypatch):
    class _Flags:
        supplemental_rules_enabled = False
        account_fit_enabled = True

    async def _load(session):
        return _Flags

    monkeypatch.setattr("investment_agent.portfolio.suitability.load_settings", _load)
    service = AccountFitService(None)
    fit = _fit()
    integrated = await service._integrate_supplemental(fit)
    assert integrated.model_dump(mode="json") == fit.model_dump(mode="json")

    class _FlagsOn(_Flags):
        supplemental_rules_enabled = True

    async def _load_on(session):
        return _FlagsOn

    monkeypatch.setattr("investment_agent.portfolio.suitability.load_settings", _load_on)
    with pytest.raises(AttributeError):
        # With the switch on the composition reads storage; a None session
        # must never silently produce a half-integrated row.
        await service._integrate_supplemental(fit)


def test_research_urgency_level_mapping():
    assert research_urgency_level(open_critical_issues=False, coverage_limits=()) is None
    assert research_urgency_level(open_critical_issues=True, coverage_limits=()) == "critical_pending"
    assert research_urgency_level(
        open_critical_issues=False, coverage_limits=("rule_coverage_pending:abc",)
    ) == "coverage_pending"
    assert research_urgency_level(
        open_critical_issues=False, coverage_limits=("unbound_event:abc",)
    ) == "coverage_pending"
    assert research_urgency_level(
        open_critical_issues=False, coverage_limits=("rule_review_due:abc",)
    ) == "review_due"
    # critical open issues dominate every coverage limitation.
    assert research_urgency_level(
        open_critical_issues=True, coverage_limits=("rule_review_due:abc",)
    ) == "critical_pending"


def test_new_buckets_order_older_snapshots_identically_and_projection_rides_along():
    manifest = _manifest(include_manual=False)
    reference = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    legacy_ids = [entry.listing_ref.id for entry in order_of(
        manifest, [_entry(reference, member) for member in manifest.members])]
    assert legacy_ids == [101, 102, 103]
    entries = [
        _entry(reference, member, status="blocked" if member.listing_ref.id == 103 else "pass")
        for member in manifest.members
    ]
    upgraded = [
        entry.model_copy(update={"ranking_bucket": "ready"}) if entry.listing_ref.id == 101 else entry
        for entry in entries
    ]
    upgraded = [
        entry.model_copy(update={
            "ranking_bucket": "limited",
            "rule_application_refs": (ObjectRef(kind="rule_application", id=uuid4(), revision=1),),
            "research_urgency": "coverage_pending",
        }) if entry.listing_ref.id == 102 else entry
        for entry in upgraded
    ]
    # ready and limited sweep ahead; the blocked entry stays last.
    assert [entry.listing_ref.id for entry in order_of(manifest, upgraded)] == [101, 102, 103]
    # A legacy-only snapshot keeps its exact historical order.
    assert [entry.listing_ref.id for entry in order_of(manifest, entries)] == legacy_ids


def order_of(manifest, entries):
    from investment_agent.candidates.ranking import order_ranking_entries

    return order_ranking_entries(manifest, entries)


def test_builder_carries_projection_and_ready_bucket():
    manifest = _manifest(include_manual=False)
    reference = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    fit = compose_supplemental_fit(
        _fit(), state=_bound_state(), research_status="current",
        company_alignment="supportive", coverage_limits=(),
    )
    assert fit.ranking_bucket == "ready"
    projection = ((ObjectRef(kind="rule_application", id=uuid4(), revision=1),), "coverage_pending")
    entries = build_ranking_entries(
        manifest,
        ranking_ref=reference,
        resolved_actions={101: "open", 102: "open", 103: "open"},
        assessments={101: fit},
        rule_projections={101: projection},
    )
    by_listing = {int(entry.listing_ref.id): entry for entry in entries}
    assert by_listing[101].ranking_bucket == "ready"
    assert by_listing[101].research_urgency == "coverage_pending"
    assert by_listing[101].rule_application_refs == projection[0]
    # Without projections the rows keep their legacy shape.
    plain = build_ranking_entries(
        manifest, ranking_ref=reference,
        resolved_actions={101: "open", 102: "open", 103: "open"},
        assessments={},
    )
    assert all(entry.research_urgency is None for entry in plain)
    assert all(entry.rule_application_refs == () for entry in plain)


def test_bucket_filter_admits_ready_and_limited():
    assert FILTERS["bucket"] == {"incomplete", "blocked", "ready", "limited"}
    manifest = _manifest(include_manual=False)
    reference = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    entries = [_entry(reference, member) for member in manifest.members]
    page = page_ranking_entries(entries, ranking_ref=reference, limit=10, bucket_filter="ready")
    assert page.entries == ()
    with pytest.raises(ValueError, match="unknown ranking bucket"):
        page_ranking_entries(entries, ranking_ref=reference, bucket_filter="excellent")  # type: ignore[arg-type]


def test_reason_labels_register_the_new_codes():
    for code in (
        "rule_binding_missing", "rule_coverage_pending", "rule_coverage_scan_incomplete",
        "rule_review_due", "unbound_event", "rule_expired", "research_check_due",
        "rule_coverage_unpublished", "rule_coverage_binding_missing",
        "event_publication_boundary", "event_effective_started", "event_effective_ended",
    ):
        assert REASON_LABELS.get(code), code

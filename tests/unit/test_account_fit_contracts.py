"""Account-fit and ranking DTO boundary tests."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_agent.candidates.ranking_contracts import (
    CandidatePolicySnapshot,
    RankingEntry,
    SourceManifest,
    build_source_manifest,
    canonical_source_manifest_bytes,
    canonical_universe_hash,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import PortfolioConstraints
from investment_agent.portfolio.purposes import Purpose, PurposeVersion
from investment_agent.portfolio.suitability_contracts import (
    FitRequestItem,
    PurposeBinding,
    TradeAssumptions,
    ValuationBinding,
)
from tests.support.account_fit import assumptions, fit_request
from tests.support.account_fit_db import AS_OF, make_inputs


def test_decimal_inputs_reject_float_and_non_finite_values() -> None:
    with pytest.raises(ValidationError):
        TradeAssumptions.model_validate(
            {
                "execution_price": 1500.0,
                "fixed_fee": "10",
                "fee_rate": "0",
                "assumption_source": "synthetic",
            }
        )
    with pytest.raises(ValidationError):
        TradeAssumptions.model_validate(
            {
                "execution_price": "NaN",
                "fixed_fee": "10",
                "fee_rate": "0",
                "assumption_source": "synthetic",
            }
        )


def test_unknown_assumptions_are_explicit_and_zero_is_known() -> None:
    unknown = assumptions(execution_price=None, fixed_fee=None, fee_rate=None)
    known_zero_fee = assumptions(fixed_fee="0", fee_rate="0")
    assert set(unknown.unknown_fields) == {"execution_price", "fixed_fee", "fee_rate"}
    assert known_zero_fee.fixed_fee == Decimal(0)
    assert known_zero_fee.fee_rate == Decimal(0)
    with pytest.raises(ValidationError):
        TradeAssumptions.model_validate(
            {
                "execution_price": None,
                "fixed_fee": "0",
                "fee_rate": "0",
                "assumption_source": "synthetic",
            }
        )


def test_actions_separate_hold_from_trade_assumptions() -> None:
    listing = ObjectRef(kind="listing", id=600000, revision=1)
    assert FitRequestItem(listing_ref=listing, action="hold").trade_assumptions is None
    with pytest.raises(ValidationError):
        FitRequestItem(listing_ref=listing, action="hold", trade_assumptions=assumptions())
    with pytest.raises(ValidationError):
        FitRequestItem(listing_ref=listing, action="open")


def test_request_rejects_naive_time_at_nested_boundary() -> None:
    request = fit_request()
    valid = request.model_dump(mode="json")
    valid["historical_replay"] = {
        "run_ref": {"kind": "pipeline_run", "id": 7, "revision": 1},
        "manifest_hash": "a" * 64,
        "input_as_of": "2026-01-01T00:00:00",
    }
    valid["mode"] = "historical_replay"
    with pytest.raises(ValidationError):
        type(request).model_validate(valid)


def test_hash_fields_require_lowercase_sha256_hex() -> None:
    request = fit_request()
    invalid = request.model_dump(mode="python")
    invalid["historical_replay"] = {
        "run_ref": ObjectRef(kind="pipeline_run", id=7, revision=1),
        "manifest_hash": "A" * 64,
        "input_as_of": datetime(2026, 1, 1, tzinfo=UTC),
    }
    invalid["mode"] = "historical_replay"
    with pytest.raises(ValidationError, match="pattern"):
        type(request).model_validate(invalid)


def test_request_rejects_duplicate_listings_and_invalid_reference_kinds() -> None:
    request = fit_request()
    duplicated = request.model_dump(mode="python")
    duplicated["items"] = (request.items[0], request.items[0])
    with pytest.raises(ValidationError):
        type(request).model_validate(duplicated)
    with pytest.raises(ValidationError):
        ObjectRef(kind="account_fit", id=7, revision=1)
    assert (
        ObjectRef(
            kind="account_fit", id=UUID("00000000-0000-0000-0000-000000000001"), revision=1
        ).kind
        == "account_fit"
    )


def test_purpose_states_keep_empty_unselected_and_unknown_distinct() -> None:
    purpose_ref = ObjectRef(
        kind="portfolio_purposes",
        id=UUID("00000000-0000-0000-0000-000000000401"),
        revision=1,
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    selected = PurposeBinding(
        state="selected",
        purpose_ref=purpose_ref,
        purpose_id=UUID("00000000-0000-0000-0000-000000000402"),
        confirmed_at=now,
    )
    empty = PurposeBinding(state="confirmed_empty", purpose_ref=purpose_ref, confirmed_at=now)
    none = PurposeBinding(state="none_recorded")
    unselected = PurposeBinding(state="unselected")
    unknown = PurposeBinding(state="unknown", reason="purpose_read_failed")
    assert {selected.state, empty.state, none.state, unselected.state, unknown.state} == {
        "selected",
        "confirmed_empty",
        "none_recorded",
        "unselected",
        "unknown",
    }
    with pytest.raises(ValidationError):
        PurposeBinding(state="selected", purpose_ref=purpose_ref, confirmed_at=now)


def test_confirmed_empty_must_match_an_actually_empty_exact_version() -> None:
    account_id = UUID("00000000-0000-0000-0000-000000000101")
    snapshot_ref = ObjectRef(
        kind="portfolio_snapshot", id=UUID("00000000-0000-0000-0000-000000000201"), revision=1
    )
    purpose_ref = ObjectRef(
        kind="portfolio_purposes", id=UUID("00000000-0000-0000-0000-000000000401"), revision=1
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    empty_version = PurposeVersion(
        reference=purpose_ref,
        account_id=account_id,
        snapshot_ref=snapshot_ref,
        confirmed_at=now,
        purposes=(),
        confirmed_cash=Decimal(0),
        unallocated=Decimal(0),
        semantic_hash="a" * 64,
    )
    PurposeBinding(
        state="confirmed_empty", purpose_ref=purpose_ref, confirmed_at=now
    ).validate_version(empty_version)
    populated = empty_version.model_copy(
        update={
            "purposes": (
                Purpose.model_validate(
                    {
                        "id": "00000000-0000-0000-0000-000000000402",
                        "name": "reserve",
                        "amount": "1",
                        "available_budget": "0",
                        "horizon": "short_term",
                        "required_cash": "1",
                        "required_on": date(2026, 2, 1),
                        "source": "synthetic",
                        "source_version": "fixture-v1",
                    }
                ),
            ),
        }
    )
    with pytest.raises(ValueError, match="no purposes"):
        PurposeBinding(
            state="confirmed_empty", purpose_ref=purpose_ref, confirmed_at=now
        ).validate_version(populated)


def test_unselected_requires_nonempty_frozen_version_and_rejects_cross_account_version() -> None:
    inputs = make_inputs()
    purpose_ref = ObjectRef(
        kind="portfolio_purposes", id=UUID("00000000-0000-0000-0000-000000000401"), revision=1
    )
    purpose = Purpose.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000402",
            "name": "reserve",
            "amount": "10",
            "available_budget": "0",
            "horizon": "short_term",
            "required_cash": "10",
            "required_on": date(2026, 2, 1),
            "source": "synthetic",
            "source_version": "fixture-v1",
        }
    )
    version = PurposeVersion(
        reference=purpose_ref,
        account_id=inputs.account_id,
        snapshot_ref=inputs.snapshot.reference,
        confirmed_at=AS_OF,
        purposes=(purpose,),
        confirmed_cash=Decimal("10"),
        unallocated=Decimal("0"),
        semantic_hash="a" * 64,
    )
    unselected = PurposeBinding(state="unselected")
    unselected.validate_version(version)
    with pytest.raises(ValueError, match="non-empty"):
        unselected.validate_version(version.model_copy(update={"purposes": ()}))

    foreign_version = version.model_copy(
        update={"account_id": UUID("00000000-0000-0000-0000-000000000999")}
    )
    with pytest.raises(ValidationError, match="belong to the frozen account"):
        type(inputs).model_validate(
            inputs.model_dump(mode="python")
            | {"purpose_version": foreign_version, "purpose_binding": unselected}
        )


def test_owner_mandate_bound_to_another_account_remains_a_valid_unknown_input() -> None:
    from investment_agent.portfolio.suitability_calculations import evaluate_fit

    inputs = make_inputs()
    constraints = PortfolioConstraints(
        account_id=UUID("00000000-0000-0000-0000-000000000999"),
        max_single_weight=Decimal("0.5"),
    )
    mandate = inputs.mandate.model_copy(update={"portfolio_constraints": constraints})
    frozen = type(inputs).model_validate(
        inputs.model_dump(mode="python") | {"mandate": mandate}
    )
    assert frozen.mandate.portfolio_constraints == constraints
    assessment, _ = evaluate_fit(frozen, fit_request().items[0], operation_id=UUID(
        "00000000-0000-0000-0000-000000000777"
    ))
    assert assessment.financial_status == "unknown"
    assert assessment.ranking_bucket == "incomplete"
    assert any(check.state == "not_configured" for check in assessment.checks)


def test_ranking_entry_references_must_be_exact_snapshot_and_listing_refs() -> None:
    ranking_id = UUID("00000000-0000-0000-0000-000000000701")
    with pytest.raises(ValidationError, match="exact ranking snapshot"):
        RankingEntry(
            ranking_ref=ObjectRef(
                kind="candidate_ranking", id=ranking_id, revision=1, field_path="entries"
            ),
            listing_ref=ObjectRef(kind="listing", id=600000, revision=1),
            ordinal=0,
            ranking_bucket="incomplete",
            financial_status="unknown",
            resolved_action=None,
            action_unknown_reason="unknown",
            source_refs=(ObjectRef(kind="pipeline_run", id=9, revision=1),),
        )


def test_screen_source_requires_nonempty_row_and_matching_canonical_hash() -> None:
    from investment_agent.candidates.ranking_contracts import ScreenCandidateSource

    run = ObjectRef(kind="pipeline_run", id=7, revision=1)
    values = {
        "source_kind": "screen_hit",
        "source_ref": run.model_copy(update={"field_path": "screen_results.9"}),
        "listing_ref": ObjectRef(kind="listing", id=600000, revision=1),
        "run_ref": run,
        "source_row_id": 9,
        "run_status": "success",
        "run_finished_at": datetime(2026, 1, 1, tzinfo=UTC),
        "run_hash": "a" * 64,
        "source_snapshot_hash": "b" * 64,
        "outcome": "hit",
        "source_as_of": datetime(2026, 1, 1, tzinfo=UTC),
        "template_id": "template",
        "template_version": "1",
        "rank": 1,
    }
    with pytest.raises(ValidationError, match="complete frozen result row"):
        ScreenCandidateSource.model_validate(values)
    with pytest.raises(ValidationError, match="snapshot hash mismatch"):
        ScreenCandidateSource.model_validate(values | {"row_snapshot": {"id": 9}})


def test_valuation_binding_requires_exact_reference_or_reason() -> None:
    ref = ObjectRef(
        kind="portfolio_valuation", id=UUID("00000000-0000-0000-0000-000000000501"), revision=1
    )
    assert ValuationBinding(state="available", valuation_ref=ref).valuation_ref == ref
    assert ValuationBinding(state="missing", reason="no_valuation").state == "missing"
    with pytest.raises(ValidationError):
        ValuationBinding(state="missing", valuation_ref=ref)


def test_empty_source_manifest_is_canonical_and_count_zero_is_valid() -> None:
    manifest = build_source_manifest(
        {
            "source_run_refs": (),
            "source_runs": (),
            "candidate_policy": CandidatePolicySnapshot(
                expiry_days=14, dismiss_cooldown_days=30
            ).model_dump(mode="json"),
            "as_of": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            "members": (),
            "exclusions": (),
            "coverage": "complete",
            "total_unique_count": 0,
            "processed_count": 0,
            "unprocessed_count": 0,
            "source_hashes": (),
        }
    )
    assert manifest.canonical_bytes == len(canonical_source_manifest_bytes(manifest))
    assert manifest.coverage == "complete"
    assert manifest.processed_count == 0
    assert (
        canonical_universe_hash(manifest.members)
        == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    )
    bad = manifest.model_dump(mode="json")
    bad["manifest_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="canonical manifest hash mismatch"):
        SourceManifest.model_validate(bad)

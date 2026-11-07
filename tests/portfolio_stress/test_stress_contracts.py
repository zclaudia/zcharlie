"""Early PT-013 slice: PT-001 goldens and PT-002 contracts only."""

import copy
import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from tests.portfolio_stress.sample_builders import (
    LISTING,
    RECORDED_AT,
    ROOT,
    build_ag22_goldens,
)

from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.stress_canonical import (
    canonical_hash,
    canonical_json,
    input_decimal,
)
from investment_agent.portfolio.stress_contracts import (
    CalculationPath,
    FrozenHolding,
    FrozenPath,
    FrozenStressInputs,
    PlanOptionPath,
    StressCreated,
    StressShock,
    StressStudyRequest,
    StressStudyView,
)
from investment_agent.storage.models.agent import AgentTask, BudgetEntry
from investment_agent.storage.models.evidence import SavedCalculation


def golden():
    return json.loads((ROOT / "ag22_v2/complete.json").read_text())


def request(**updates):
    fixture = golden()
    value = {
        "operation_id": str(UUID(int=800)),
        "account_id": fixture["request"]["account_id"],
        "expected_account_revision": fixture["request"]["snapshot_ref"]["revision"],
        "snapshot_ref": fixture["request"]["snapshot_ref"],
        "valuation_ref": fixture["request"]["valuation_ref"],
        "alternatives": [],
        "shocks": [
            {
                "name": "市场与银行压力",
                "global": "-0.20",
                "industries": [{"system": "csrc", "code": "J66", "shock": "-0.30"}],
                "listings": [],
            }
        ],
        "thresholds": {"maximum_loss_cny": "100000", "maximum_loss_ratio": "0.25"},
    }
    value.update(updates)
    return value


async def test_goldens_reproduce_original_save_get_chain(portfolio_stress_session):
    values = await build_ag22_goldens(portfolio_stress_session)
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["synthetic"] is True and manifest["production_data"] is False
    for entry in manifest["entries"]:
        path = ROOT / entry["path"]
        saved = json.loads(path.read_text())
        assert values[path.stem] == saved
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        assert canonical_hash(saved) == entry["content_hash"]
        assert saved["calculation"]["input_hash"] == entry["calculation_input_hash"]
    calc = values["complete"]["calculation"]
    assert calc["method_version"] == "ag22-v2"
    assert calc["details"]["snapshot"]["holdings"][0]["price"] == "10.000000"
    assert calc["details"]["sources"][0]["price"] == "12.000000"
    baseline, sold, new_holding, missing_fee = calc["details"]["scenarios"]
    assert Decimal(baseline["nav"]) == 2200
    assert Decimal(sold["cash"]) == 1119 and Decimal(sold["fees"]) == 1
    assert Decimal(sold["nav"]) == 2199
    assert Decimal(new_holding["nav"]) == 2198
    assert missing_fee["cash"] is None and missing_fee["nav"] is None
    for model in (AgentTask, BudgetEntry):
        assert await portfolio_stress_session.scalar(select(func.count()).select_from(model)) == 0
    assert (
        await portfolio_stress_session.scalar(select(func.count()).select_from(SavedCalculation))
        == 2
    )


def test_two_s1_examples_and_frozen_base_contract_parse_with_real_fixture_refs():
    created = StressStudyRequest.model_validate(request())
    assert created.snapshot_ref == ObjectRef.model_validate(golden()["snapshot"]["reference"])
    assert created.shocks[0].global_shock == Decimal("-0.2")
    base = json.loads((ROOT.parent / "next_iterations/contracts/portfolio.json").read_text())
    assert StressStudyRequest.model_validate(base["create_stress"]["request"])
    assert StressCreated.model_validate(base["create_stress"]["response"])
    partial = copy.deepcopy(base["partial_result"])
    partial["version_refs"] = [request()["snapshot_ref"], request()["valuation_ref"]]
    view = StressStudyView.model_validate(partial)
    assert view.results["total_loss"].value is None
    assert view.results["total_loss"].unknown_reason == "cash_missing"


@pytest.mark.parametrize(
    "bad",
    [
        True,
        False,
        1.2,
        float("nan"),
        float("inf"),
        "NaN",
        "Infinity",
        "-Infinity",
        "1e1000000",
        "1e-1000000",
        Decimal("1e1000000"),
        "0e1000000",
        "0.0000000000000000001",
        "9" * 41,
        "1" * 300,
    ],
)
def test_numeric_input_rejects_float_bool_nonfinite_precision_and_expansion(bad):
    with pytest.raises((ValueError, ValidationError)):
        input_decimal(bad)
    with pytest.raises(ValidationError):
        StressShock.model_validate({"name": "bad", "global": bad})


@pytest.mark.parametrize("value", [-1, "-1.000", 10, "10.000000000000000000", "1e1", Decimal("-0")])
def test_valid_shock_boundaries_and_equivalent_decimals(value):
    assert StressShock.model_validate(
        {"name": "boundary", "global": value}
    ).global_shock == Decimal(str(value))


@pytest.mark.parametrize("value", ["-1.000000000000000001", "10.000000000000000001"])
def test_shock_range_is_closed(value):
    with pytest.raises(ValidationError):
        StressShock.model_validate({"name": "outside", "global": value})


def test_hash_normalizes_typed_values_defaults_utc_and_global_alias():
    first = StressStudyRequest.model_validate(request())
    data = request()
    data["shocks"][0]["global"] = "-2e-1"
    data["shocks"][0]["industries"][0]["shock"] = "-0.300000"
    data["thresholds"] = {"maximum_loss_cny": 100000, "maximum_loss_ratio": "0.25000"}
    data["basis_mode"] = "current"
    second = StressStudyRequest.model_validate(data)
    with localcontext() as ctx:
        ctx.prec = 2
        assert canonical_hash(first) == canonical_hash(second)
        assert canonical_json(Decimal("123456789.123456789000")) == '"123456789.123456789"'
    assert '"global":' in canonical_json(first) and "global_shock" not in canonical_json(first)
    assert canonical_json(datetime.fromisoformat("2026-11-07T17:30:00+08:00")) == canonical_json(
        RECORDED_AT
    )
    assert canonical_json("1.00") != canonical_json("1")
    with pytest.raises(ValueError):
        canonical_json(datetime(2026, 11, 7))
    with pytest.raises(ValueError):
        canonical_json({"bad": 1.0})
    with pytest.raises(ValueError):
        canonical_json(Decimal("1e1000000"))


def test_alternative_union_keeps_plan_and_option_together_and_rejects_quantities():
    calc_ref = {"kind": "calculation", "id": str(UUID(int=208)), "revision": 1}
    simple = {"calculation_ref": calc_ref, "scenario_index": 1}
    parsed = StressStudyRequest.model_validate(request(alternatives=[simple]))
    assert type(parsed.alternatives[0]) is CalculationPath
    option = {
        **simple,
        "plan_ref": {"kind": "portfolio_plan", "id": str(UUID(int=1000)), "revision": 1},
        "option_id": str(UUID(int=1001)),
    }
    assert isinstance(
        StressStudyRequest.model_validate(request(alternatives=[option])).alternatives[0],
        PlanOptionPath,
    )
    for invalid in (
        {**simple, "quantity": "100"},
        {**simple, "plan_ref": option["plan_ref"]},
        {**simple, "option_id": option["option_id"]},
        {**option, "scenario_index": 0},
        {**simple, "scenario_index": True},
        {**simple, "scenario_index": 21},
    ):
        with pytest.raises(ValidationError):
            StressStudyRequest.model_validate(request(alternatives=[invalid]))


def test_capacity_limits_and_duplicate_targets_are_enforced():
    def listing(i):
        return {"listing_ref": {"kind": "listing", "id": i, "revision": 1}, "shock": "-0.2"}

    assert (
        len(
            StressStudyRequest.model_validate(
                request(shocks=[{"name": str(i), "global": 0} for i in range(20)])
            ).shocks
        )
        == 20
    )
    for changes in [
        {"shocks": [{"name": str(i), "global": 0} for i in range(21)]},
        {"shocks": [{"name": "same", "global": 0}] * 2},
        {"shocks": [{"name": "dup", "listings": [listing(18)] * 2}]},
        {
            "shocks": [
                {"name": "dup", "industries": [{"system": "csrc", "code": "J66", "shock": 0}] * 2}
            ]
        },
        {"shocks": [{"name": "many", "listings": [listing(i) for i in range(1, 52)]}]},
        {
            "shocks": [
                {"name": "a", "listings": [listing(i) for i in range(1, 27)]},
                {"name": "b", "listings": [listing(i) for i in range(27, 52)]},
            ]
        },
        {
            "alternatives": [
                {
                    "calculation_ref": {
                        "kind": "calculation",
                        "id": str(UUID(int=208)),
                        "revision": 1,
                    },
                    "scenario_index": i,
                }
                for i in range(4)
            ]
        },
        {"industry_system": "CSRC"},
        {"owner_id": "local_owner"},
        {"shocks": [{"name": "forged", "global": 0, "provenance": "historical_fact"}]},
    ]:
        with pytest.raises(ValidationError):
            StressStudyRequest.model_validate(request(**changes))
    fields = [
        {
            "calculation_ref": {"kind": "calculation", "id": str(UUID(int=208)), "revision": 1},
            "scenario_index": i,
        }
        for i in range(3)
    ]
    assert (
        len(StressStudyRequest.model_validate(request(alternatives=fields)).alternatives) + 1 == 4
    )


def frozen(count=50):
    req = request()
    refs = [ObjectRef(kind="listing", id=i, revision=1) for i in range(1, count + 1)]
    holdings = [
        {"listing_ref": ref, "quantity": "0", "price": None, "price_ref": None} for ref in refs
    ]
    return {
        "account_id": req["account_id"],
        "snapshot_ref": req["snapshot_ref"],
        "valuation_ref": req["valuation_ref"],
        "mandate_ref": None,
        "valuation_date": RECORDED_AT.date(),
        "knowledge_cutoff": RECORDED_AT,
        "paths": [
            {
                "name": "actual",
                "source": "actual",
                "cash": "0",
                "coverage": "complete",
                "holdings": holdings,
            }
        ],
        "prices": [],
        "industries": [],
        "versions": [
            {"ref": req["snapshot_ref"], "payload_hash": "a" * 64},
            {"ref": req["valuation_ref"], "payload_hash": "b" * 64},
        ],
    }


def test_frozen_capacity_zero_holding_and_unknowns_do_not_turn_into_zero():
    assert len(FrozenStressInputs.model_validate(frozen()).paths[0].holdings) == 50
    with pytest.raises(ValidationError):
        FrozenStressInputs.model_validate(frozen(51))
    h = {
        "listing_ref": LISTING,
        "quantity": None,
        "price": None,
        "price_ref": None,
        "quantity_unknown_reason": "not_confirmed",
        "price_unknown_reason": "valuation_missing",
    }
    assert FrozenHolding.model_validate(h).quantity is None
    with pytest.raises(ValidationError):
        FrozenHolding.model_validate({**h, "quantity_unknown_reason": None})
    with pytest.raises(ValidationError):
        FrozenPath.model_validate(
            {
                "name": "cash",
                "source": "actual",
                "cash": None,
                "coverage": "partial",
                "holdings": [],
            }
        )
    with pytest.raises(ValidationError):
        FrozenHolding.model_validate({**h, "quantity": True})


def test_frozen_manifest_and_union_capacity_cannot_be_bypassed_with_multiple_paths():
    data = frozen(25)
    alt_ref = {"kind": "calculation", "id": str(UUID(int=208)), "revision": 1}
    alternative = {
        "name": "saved",
        "source": "calculation",
        "alternative": {"calculation_ref": alt_ref, "scenario_index": 1},
        "cash": "0",
        "coverage": "complete",
        "holdings": [
            {
                "listing_ref": {"kind": "listing", "id": i, "revision": 1},
                "quantity": "0",
                "price": None,
                "price_ref": None,
            }
            for i in range(26, 51)
        ],
    }
    data["paths"].append(alternative)
    data["versions"].append({"ref": alt_ref, "payload_hash": "c" * 64})
    assert FrozenStressInputs.model_validate(data)
    extra = {
        "listing_ref": {"kind": "listing", "id": 51, "revision": 1},
        "quantity": "0",
        "price": None,
        "price_ref": None,
    }
    alternative["holdings"].append(extra)
    with pytest.raises(ValidationError):
        FrozenStressInputs.model_validate(data)
    with pytest.raises(ValidationError):
        FrozenStressInputs.model_validate({**frozen(1), "versions": [frozen(1)["versions"][0]] * 2})


def test_frozen_price_and_industry_temporal_boundaries():
    data = frozen(1)
    source = {
        "price_ref": {"kind": "market_snapshot", "id": 1, "revision": 1},
        "listing_ref": {"kind": "listing", "id": 1, "revision": 1},
        "price": "10",
        "trade_date": RECORDED_AT.date(),
        "captured_at": RECORDED_AT,
        "provider": "synthetic",
    }
    data["prices"] = [source]
    assert FrozenStressInputs.model_validate(data)
    for invalid in (
        {"captured_at": RECORDED_AT + timedelta(microseconds=1)},
        {"trade_date": (RECORDED_AT + timedelta(days=1)).date()},
        {"adjust": "backward"},
    ):
        with pytest.raises(ValidationError):
            FrozenStressInputs.model_validate({**data, "prices": [{**source, **invalid}]})
    industry = {
        "row_id": 1,
        "listing_ref": source["listing_ref"],
        "system": "csrc",
        "code": "J66",
        "source": "synthetic",
        "valid_from": RECORDED_AT.date(),
        "valid_to": None,
        "captured_at": RECORDED_AT,
    }
    assert FrozenStressInputs.model_validate({**data, "industries": [industry]})
    with pytest.raises(ValidationError):
        FrozenStressInputs.model_validate(
            {
                **data,
                "industries": [
                    {
                        **industry,
                        "valid_from": (RECORDED_AT - timedelta(days=1)).date(),
                        "valid_to": RECORDED_AT.date(),
                    }
                ],
            }
        )


def test_read_model_does_not_claim_complete_totals_with_missing_cash():
    raw = json.loads((ROOT.parent / "next_iterations/contracts/portfolio.json").read_text())[
        "partial_result"
    ]
    assert StressStudyView.model_validate(raw)
    raw["results"]["total_loss"] = {"value": "1", "unit": "CNY", "currency": "CNY"}
    with pytest.raises(ValidationError):
        StressStudyView.model_validate(raw)
    raw["state"] = "failed"
    with pytest.raises(ValidationError):
        StressStudyView.model_validate(raw)


@pytest.mark.parametrize("change", ["cash", "quantity", "price"])
def test_complete_path_rejects_missing_amount_inputs_but_keeps_partial_scope(change):
    base = frozen(1)["paths"][0]
    if change == "cash":
        base.update(cash=None, cash_unknown_reason="cash_missing")
    elif change == "quantity":
        base["holdings"][0].update(
            quantity=None,
            quantity_unknown_reason="not_confirmed",
            price_unknown_reason="valuation_missing",
        )
    else:
        base["holdings"][0].update(quantity="1", price_unknown_reason="valuation_missing")
    with pytest.raises(ValidationError):
        FrozenPath.model_validate(base)
    assert FrozenPath.model_validate({**base, "coverage": "partial"}).coverage == "partial"
    assert (
        FrozenPath.model_validate(frozen(1)["paths"][0]).coverage == "complete"
    )  # q=0 needs no price


def test_complete_read_model_does_not_promote_partial_cash_or_unknown_totals():
    raw = json.loads((ROOT.parent / "next_iterations/contracts/portfolio.json").read_text())[
        "partial_result"
    ]
    with pytest.raises(ValidationError):
        StressStudyView.model_validate({**raw, "state": "complete"})
    # Known loss amounts can coexist with an undefined zero-NAV ratio.
    known = {
        **raw,
        "state": "complete",
        "quality": ["zero_nav_ratio_undefined"],
        "results": {
            "total_loss": {"value": "0", "unit": "CNY", "currency": "CNY"},
            "loss_ratio": {"value": None, "unit": "ratio", "unknown_reason": "zero_nav"},
        },
    }
    assert StressStudyView.model_validate(known).state == "complete"
    known["results"]["total_loss"] = {
        "value": None,
        "unit": "CNY",
        "currency": "CNY",
        "unknown_reason": "scope_partial",
    }
    with pytest.raises(ValidationError):
        StressStudyView.model_validate(known)


@pytest.mark.parametrize("change", ["missing", "wrong_value", "wrong_version", "wrong_listing"])
def test_usable_holding_price_requires_the_matching_frozen_source(change):
    data = frozen(1)
    ref = {"kind": "market_snapshot", "id": 999, "revision": 2}
    holding = data["paths"][0]["holdings"][0]
    holding.update(quantity="1", price="999", price_ref=ref)
    price = {
        "price_ref": ref,
        "listing_ref": holding["listing_ref"],
        "price": "999",
        "trade_date": RECORDED_AT.date(),
        "captured_at": RECORDED_AT,
        "provider": "synthetic",
    }
    data["prices"] = [price]
    assert FrozenStressInputs.model_validate(data)
    if change == "missing":
        data["prices"] = []
    elif change == "wrong_value":
        price["price"] = "998"
    elif change == "wrong_version":
        price["price_ref"] = {**ref, "revision": 1}
    else:
        price["listing_ref"] = {"kind": "listing", "id": 2, "revision": 1}
    with pytest.raises(ValidationError):
        FrozenStressInputs.model_validate(data)

"""PT-012 pure result contracts: partial amounts, frozen limits and reproducibility."""

from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Context, Decimal, Inexact, localcontext
from uuid import UUID

import pytest

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import Mandate, PortfolioConstraints, ResearchAuthorization
from investment_agent.portfolio.stress_canonical import canonical_hash
from investment_agent.portfolio.stress_contracts import FrozenStressInputs, StressStudyRequest
from investment_agent.portfolio.stress_results import assemble_stress_result, stress_input_hash

NOW = datetime(2026, 11, 7, 10, tzinfo=UTC)
ACCOUNT = UUID(int=1)
SNAPSHOT = ObjectRef(kind="portfolio_snapshot", id=UUID(int=2), revision=1)
VALUATION = ObjectRef(kind="portfolio_valuation", id=UUID(int=3), revision=1)
LISTING = ObjectRef(kind="listing", id=18, revision=1)
PRICE = ObjectRef(kind="market_snapshot", id=18, revision=1)


def policy(**limits):
    return Mandate(id=UUID(int=4), revision=1, created_at=NOW, actor="fixture", origin_ref=None,
        goals=(), scope_refs=(LISTING,), hard_constraints=(), unknowns=(),
        portfolio_constraints=PortfolioConstraints(account_id=ACCOUNT, **limits),
        authorization=ResearchAuthorization(background_enabled=False, max_leads_per_batch=0,
            max_new_tasks_per_batch=0, source_refs=()))


def basis(*, cash="100", quantity="10", price="20", coverage="complete", industry=True, mandate=None):
    holding = {"listing_ref": LISTING, "quantity": quantity, "price": price,
        "price_ref": PRICE if price is not None else None,
        "quantity_unknown_reason": "not_confirmed" if quantity is None else None,
        "price_unknown_reason": "stale_price" if price is None and quantity != "0" else None}
    copies = [{"row_id": 1, "listing_ref": LISTING, "code": "J66", "source": "fixture",
               "valid_from": date(2025, 1, 1), "valid_to": None, "captured_at": NOW}] if industry else []
    versions = [{"ref": SNAPSHOT, "payload_hash": "a" * 64}, {"ref": VALUATION, "payload_hash": "b" * 64}]
    mandate_ref = None
    if mandate is not None:
        mandate_ref = ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision)
        versions.append({"ref": mandate_ref, "payload_hash": canonical_hash(mandate)})
    return FrozenStressInputs.model_validate({
        "account_id": ACCOUNT, "snapshot_ref": SNAPSHOT, "valuation_ref": VALUATION,
        "mandate_ref": mandate_ref, "valuation_date": NOW.date(), "knowledge_cutoff": NOW,
        "versions": versions, "industries": copies,
        "prices": [{"price_ref": PRICE, "listing_ref": LISTING, "price": price,
                    "trade_date": NOW.date(), "captured_at": NOW, "provider": "fixture", "trade_status": "suspended"}] if price is not None else [],
        "paths": [{"name": "actual", "source": "actual", "cash": cash,
                   "cash_unknown_reason": "cash_missing" if cash is None else None,
                   "coverage": coverage, "holdings": [holding], "industries": copies}],
    })


def request(**values):
    return StressStudyRequest.model_validate({"operation_id": UUID(int=8), "account_id": ACCOUNT,
        "expected_account_revision": 1, "snapshot_ref": SNAPSHOT, "valuation_ref": VALUATION,
        "shocks": [{"name": "down", "global": "-0.2"}], **values})


def result(req=None, inputs=None, **kwargs):
    return assemble_stress_result(req or request(), inputs or basis(), result_id=UUID(int=9),
                                  created_at=NOW, **kwargs)


def checks(record):
    return {item["key"]: item for item in record.details["rows"][0]["constraints"]}


def test_hand_oracle_constraints_are_per_item_and_frozen():
    mandate = policy(max_single_weight="0.5", max_sector_weight="0.7", minimum_cash="110", minimum_cash_weight="0.3")
    req = request(thresholds={"maximum_loss_cny": "40", "maximum_loss_ratio": "0.1"})
    inputs = basis(mandate=mandate)
    before = mandate.model_dump_json()
    with localcontext(Context(prec=2, rounding=ROUND_DOWN)) as ambient:
        ambient.traps[Inexact] = True
        saved = result(req, inputs, mandate=mandate)
    assert saved.status.value == "ok" and saved.details["quality_state"] == "complete"
    assert saved.results["p0_s0_nav_after"].value == 260
    assert saved.results["p0_s0_total_loss"].value == 40
    limits = checks(saved)
    assert {name: item["status"] for name, item in limits.items()} == {
        "max_single_weight": "fail", "max_sector_weight": "pass", "minimum_cash": "fail",
        "minimum_cash_weight": "pass", "maximum_loss_cny": "pass", "maximum_loss_ratio": "fail",
    }
    assert mandate.model_dump_json() == before
    assert saved.details["liquidity_assessed"] is False
    assert saved.details["price_diagnostics"][0]["trade_status"] == "suspended"
    assert {item.provenance for item in saved.assumptions} == {"user_scenario"}


def test_missing_industry_only_degrades_configured_sector_limit_for_global_shock():
    mandate = policy(max_single_weight="0.7", max_sector_weight="0.7", minimum_cash="50")
    saved = result(inputs=basis(industry=False, mandate=mandate), mandate=mandate)
    limits = checks(saved)
    assert saved.status.value == "ok" and saved.details["quality_state"] == "partial"
    assert saved.results["p0_s0_total_loss"].value == 40
    assert limits["max_sector_weight"]["status"] == "unknown"
    assert limits["max_sector_weight"]["reason"] == "industry_missing"
    assert limits["max_single_weight"]["status"] == limits["minimum_cash"]["status"] == "pass"
    assert result(inputs=basis(industry=False)).details["quality_state"] == "complete"


@pytest.mark.parametrize("missing", ["cash", "quantity", "price", "scope"])
def test_partial_does_not_pass_whole_account_thresholds(missing):
    arguments = {"coverage": "partial"}
    if missing != "scope":
        arguments[missing] = None
    saved = result(request(thresholds={"maximum_loss_cny": "100", "maximum_loss_ratio": "1"}), basis(**arguments))
    assert saved.status.value == "ok"
    assert saved.details["quality_state"] == "partial"
    assert saved.results["p0_s0_total_loss"].value is None
    assert checks(saved)["maximum_loss_cny"]["status"] == "unknown"
    assert checks(saved)["maximum_loss_ratio"]["status"] == "unknown"
    assert saved.results["p0_s0_known_securities_loss"].value == (40 if missing in {"cash", "scope"} else 0)


def test_completely_missing_inputs_have_no_successful_results():
    saved = result(inputs=basis(cash=None, quantity=None, price=None, coverage="partial", industry=False))
    assert saved.status.value == "missing_inputs" and saved.results == {}
    assert saved.applicable is False and saved.errors == ("missing_usable_inputs",)


def test_zero_nav_preserves_amounts_but_does_not_pass_ratio_constraints():
    mandate = policy(max_single_weight="1", minimum_cash="0", minimum_cash_weight="0")
    saved = result(request(shocks=[{"name": "wipeout", "global": "-1"}], thresholds={"maximum_loss_ratio": "1"}),
                   basis(cash="0", mandate=mandate), mandate=mandate)
    limits = checks(saved)
    assert saved.results["p0_s0_total_loss"].value == 200
    assert saved.results["p0_s0_loss_ratio"].value == 1
    assert limits["maximum_loss_ratio"]["status"] == limits["minimum_cash"]["status"] == "pass"
    assert limits["max_single_weight"]["status"] == limits["minimum_cash_weight"]["status"] == "unknown"


def test_hash_replays_fixed_inputs_and_diagnoses_any_source_or_threshold_change():
    req, inputs = request(), basis()
    manifest = {"system": "csrc", "sources": [{"row_id": 1, "hash": "a" * 64}]}
    diagnostics = ({"path": "actual", "reason": "liquidity_not_assessed"},)
    digest = stress_input_hash(req, inputs, industry_manifest=manifest, input_diagnostics=diagnostics)
    options = {"industry_manifest": manifest, "input_diagnostics": diagnostics, "expected_input_hash": digest}
    assert result(req, inputs, **options).model_dump_json() == result(req, inputs, **options).model_dump_json()
    changed_operation = req.model_copy(update={"operation_id": UUID(int=10)})
    assert stress_input_hash(changed_operation, inputs, industry_manifest=manifest, input_diagnostics=diagnostics) == digest
    with pytest.raises(ValueError, match="input hash changed"):
        result(req, inputs, **(options | {"industry_manifest": {"system": "csrc", "sources": []}}))
    with pytest.raises(ValueError, match="input hash changed"):
        result(request(thresholds={"maximum_loss_cny": "1"}), inputs, **options)
    with pytest.raises(ValueError, match="input hash changed"):
        result(req, inputs, **(options | {"input_diagnostics": ()}))


def test_policy_must_match_exact_hash_and_account():
    mandate = policy(minimum_cash="50")
    inputs = basis(mandate=mandate)
    with pytest.raises(ValueError, match="payload is required"):
        result(inputs=inputs)
    altered = mandate.model_copy(update={"portfolio_constraints": PortfolioConstraints(account_id=ACCOUNT, minimum_cash="60")})
    with pytest.raises(ValueError, match="frozen version hash"):
        result(inputs=inputs, mandate=altered)
    foreign = policy().model_copy(update={"portfolio_constraints": PortfolioConstraints(account_id=UUID(int=50))})
    with pytest.raises(ValueError, match="another account"):
        result(inputs=basis(mandate=foreign), mandate=foreign)


def test_unknown_industry_does_not_erase_known_before_values_or_invent_loss():
    saved = result(request(shocks=[{"name": "sector", "industries": [{"code": "J66", "shock": "-0.3"}]}]),
                   basis(cash=None, coverage="partial", industry=False))
    assert saved.status.value == "ok" and saved.details["quality_state"] == "partial"
    assert saved.results["p0_s0_known_securities_before"].value == Decimal(200)
    assert saved.results["p0_s0_total_loss"].value is None

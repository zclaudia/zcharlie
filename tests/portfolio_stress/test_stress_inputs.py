"""PT-006 source identities over the actual AG22 fixture save/get chain."""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, update
from tests.portfolio_stress.sample_builders import (
    ACCOUNT_ID,
    FACT_AT,
    RECORDED_AT,
    build_ag22_goldens,
)

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.contracts import AppendEvent, CashFact, CreateAccount
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.stress_contracts import StressStudyRequest
from investment_agent.portfolio.stress_inputs import StressInputResolver
from investment_agent.storage.models.investor import MandateHead, MandateRevision
from investment_agent.storage.models.portfolio import (
    PortfolioAccount,
    PortfolioSnapshot,
    PortfolioValuation,
)


@pytest.fixture
async def sample(portfolio_stress_session):
    return (await build_ag22_goldens(portfolio_stress_session))["complete"]


def request(sample, **updates):
    return StressStudyRequest.model_validate({
        "operation_id": str(UUID(int=800)), "account_id": sample["request"]["account_id"],
        "expected_account_revision": 1, "snapshot_ref": sample["request"]["snapshot_ref"],
        "valuation_ref": sample["request"]["valuation_ref"],
        "shocks": [{"name": "global", "global": "-0.2"}], **updates,
    })


async def test_actual_basis_uses_valuation_price_and_exact_policy_versions(portfolio_stress_session, sample):
    basis = await StressInputResolver(portfolio_stress_session).resolve_basis(request(sample), now=RECORDED_AT)
    assert basis.snapshot.holdings[0].price == 10 and basis.valuation.holdings[0].price == 12
    assert basis.account_revision == 1 and basis.current_basis_changed is False
    assert basis.mandate is not None and str(basis.mandate.id) == sample["mandate"]["id"]
    assert {value.ref.kind for value in basis.versions} == {"mandate", "portfolio_snapshot", "portfolio_valuation"}
    assert len({value.payload_hash for value in basis.versions}) == 3


async def test_owner_is_rechecked_from_persistent_head_before_returning_basis(portfolio_stress_session, sample):
    resolver = StressInputResolver(portfolio_stress_session)
    await resolver.resolve_basis(request(sample), now=RECORDED_AT)
    await portfolio_stress_session.execute(update(PortfolioAccount).values(owner_id="foreign")
                                          .execution_options(synchronize_session=False))
    with pytest.raises(InvestorError) as error:
        await resolver.resolve_basis(request(sample), now=RECORDED_AT)
    assert error.value.detail.code == "not_found"


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("model", [PortfolioSnapshot, PortfolioValuation])
async def test_wrong_account_is_invalid_but_foreign_account_is_not_found(portfolio_stress_session, sample, model, foreign):
    other = await PortfolioService(portfolio_stress_session).create_account(
        CreateAccount(idempotency_key=UUID(int=999), name="other synthetic account"))
    if foreign:
        await portfolio_stress_session.execute(update(PortfolioAccount).where(PortfolioAccount.id == str(other.id)).values(owner_id="foreign"))
    await portfolio_stress_session.execute(update(model).values(account_id=str(other.id)))
    with pytest.raises(InvestorError) as error:
        await StressInputResolver(portfolio_stress_session).resolve_basis(request(sample), now=RECORDED_AT)
    assert error.value.detail.code == ("not_found" if foreign else "invalid_reference")


async def test_wrong_snapshot_revision_is_invalid(portfolio_stress_session, sample):
    ref = {**sample["request"]["snapshot_ref"], "revision": 2}
    with pytest.raises(InvestorError) as error:
        await StressInputResolver(portfolio_stress_session).resolve_basis(request(sample, snapshot_ref=ref), now=RECORDED_AT)
    assert error.value.detail.code == "invalid_reference"


async def test_account_advance_requires_explicit_historical_mode_and_new_cas(portfolio_stress_session, sample):
    await PortfolioService(portfolio_stress_session).append(AppendEvent(
        account_id=ACCOUNT_ID, expected_revision=1, idempotency_key=UUID(int=888),
        fact=CashFact(kind="cash_transfer", amount="1", occurred_at=FACT_AT + timedelta(hours=1),
                      source="synthetic confirmation", user_confirmed=True)))
    resolver = StressInputResolver(portfolio_stress_session)
    with pytest.raises(InvestorError) as error:
        await resolver.resolve_basis(request(sample), now=RECORDED_AT + timedelta(days=1))
    assert error.value.detail.code == "revision_conflict"
    with pytest.raises(InvestorError) as error:
        await resolver.resolve_basis(request(sample, expected_account_revision=2), now=RECORDED_AT + timedelta(days=1))
    assert error.value.detail.code == "invalid_reference"
    historical = await resolver.resolve_basis(request(sample, expected_account_revision=2, basis_mode="historical"), now=RECORDED_AT + timedelta(days=1))
    assert historical.current_basis_changed
    assert historical.snapshot.revision == historical.valuation.ledger_revision == 1
    assert historical.snapshot.known_cash == 1000  # No implicit rebind to new cash 1001.


@pytest.mark.parametrize("defect", ["snapshot_id", "ledger_revision", "quantity", "cash", "duplicate", "future", "hash"])
async def test_valuation_columns_payload_and_original_facts_must_agree(portfolio_stress_session, sample, defect):
    row = await portfolio_stress_session.scalar(select(PortfolioValuation))
    payload = deepcopy(row.payload)
    if defect == "snapshot_id":
        payload["snapshot_id"] = str(UUID(int=999))
    elif defect == "ledger_revision":
        payload["ledger_revision"] = 2
    elif defect == "quantity":
        payload["holdings"][0]["quantity"] = "999"
    elif defect == "cash":
        payload["known_cash"] = "999"
    elif defect == "duplicate":
        payload["holdings"].append(deepcopy(payload["holdings"][0]))
    elif defect == "future":
        payload["knowledge_cutoff"] = (RECORDED_AT + timedelta(days=1)).isoformat()
    else:
        payload["input_digest"] = "f" * 64
    await portfolio_stress_session.execute(update(PortfolioValuation).values(payload=payload)
                                          .execution_options(synchronize_session=False))
    with pytest.raises(InvestorError) as error:
        await StressInputResolver(portfolio_stress_session).resolve_basis(request(sample), now=RECORDED_AT)
    assert error.value.detail.code == "invalid_reference"


async def test_foreign_mandate_and_non_applicable_policy_do_not_sneak_into_basis(portfolio_stress_session, sample):
    row = await portfolio_stress_session.scalar(select(MandateRevision))
    ref = ObjectRef(kind="mandate", id=UUID(row.mandate_id), revision=row.revision)
    resolver = StressInputResolver(portfolio_stress_session)
    await portfolio_stress_session.execute(update(MandateRevision).values(owner_id="foreign")
                                          .execution_options(synchronize_session=False))
    with pytest.raises(InvestorError) as error:
        await resolver.resolve_basis(request(sample), mandate_ref=ref, now=RECORDED_AT)
    assert error.value.detail.code == "not_found"
    payload = deepcopy(sample["mandate"])
    payload["portfolio_constraints"]["account_id"] = str(UUID(int=999))
    await portfolio_stress_session.execute(update(MandateRevision).values(owner_id="local_owner", payload=payload))
    with pytest.raises(InvestorError) as error:
        await resolver.resolve_basis(request(sample), mandate_ref=ref, now=RECORDED_AT)
    assert error.value.detail.code == "invalid_reference"
    basis = await resolver.resolve_basis(request(sample), now=RECORDED_AT)
    assert basis.mandate is None and len(basis.versions) == 2


async def test_invalid_or_future_mandate_reference_is_rejected(portfolio_stress_session, sample):
    head = await portfolio_stress_session.get(MandateHead, "local_owner")
    resolver = StressInputResolver(portfolio_stress_session)
    for ref in [ObjectRef(kind="evidence", id=UUID(head.mandate_id), revision=1),
                ObjectRef(kind="mandate", id=UUID(head.mandate_id), revision=1, field_path="constraints")]:
        with pytest.raises(InvestorError) as error:
            await resolver.mandate(ACCOUNT_ID, ref)
        assert error.value.detail.code == "invalid_reference"
    await portfolio_stress_session.execute(update(MandateHead).values(revision=0))
    with pytest.raises(InvestorError) as error:
        await resolver.mandate(ACCOUNT_ID, ObjectRef(kind="mandate", id=UUID(head.mandate_id), revision=1))
    assert error.value.detail.code == "invalid_reference"

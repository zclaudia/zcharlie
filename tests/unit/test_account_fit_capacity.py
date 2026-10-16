"""Diagnostic capacity stays aligned across fit and ranking DTOs."""

from uuid import UUID, uuid4

import pytest

from investment_agent.candidates.ranking_contracts import RankingEntry
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.suitability_calculations import evaluate_fit
from investment_agent.portfolio.suitability_contracts import MAX_FIT_CHECKS, FitRequestItem
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import make_inputs


def _inputs(*, unknown_quantities: bool):
    inputs = make_inputs(
        with_valuation=False,
        cash="10000000",
        holdings=tuple((700000 + index, "1", "1") for index in range(500)),
        prices={600000: "1500"},
    )
    if not unknown_quantities:
        return inputs
    holdings = tuple(
        holding.model_copy(update={"confirmed_quantity": None})
        for holding in inputs.snapshot.holdings
    )
    snapshot = inputs.snapshot.model_copy(
        update={
            "holdings": holdings,
            "coverage": "partial",
            "unknowns": ("holding_quantity_unknown",),
        }
    )
    return type(inputs).model_validate(inputs.model_dump(mode="python") | {"snapshot": snapshot})


@pytest.mark.parametrize("unknown_quantities", [False, True])
def test_500_holdings_with_missing_prices_and_quantities_fit_and_rank(unknown_quantities: bool) -> None:
    inputs = _inputs(unknown_quantities=unknown_quantities)
    item = FitRequestItem(
        listing_ref=ObjectRef(kind="listing", id=600000, revision=1),
        action="open",
        trade_assumptions=assumptions(),
    )

    assessment, _ = evaluate_fit(
        inputs,
        item,
        operation_id=UUID("00000000-0000-0000-0000-000000000777"),
    )

    assert assessment.financial_status == "unknown"
    assert assessment.coverage == "partial"
    assert 1000 < len(assessment.checks) <= MAX_FIT_CHECKS
    ranking_ref = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    entry = RankingEntry(
        ranking_ref=ranking_ref,
        listing_ref=item.listing_ref,
        ordinal=0,
        ranking_bucket=assessment.ranking_bucket,
        financial_status=assessment.financial_status,
        resolved_action="open",
        fit_ref=assessment.reference,
        source_refs=(ObjectRef(kind="pipeline_run", id=9, revision=1),),
        checks=assessment.checks,
    )
    assert len(entry.checks) == len(assessment.checks)

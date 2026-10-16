"""Pure helpers for account-fit-v1 DTO fixtures; no database or app imports."""

from uuid import UUID, uuid4

from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.suitability_contracts import (
    AccountFitRequest,
    FitRequestItem,
    PurposeBinding,
    TradeAssumptions,
    ValuationBinding,
)


def assumptions(
    *,
    execution_price: str | None = "1500",
    fixed_fee: str | None = "10",
    fee_rate: str | None = "0",
    assumption_source: str = "synthetic_user_scenario",
) -> TradeAssumptions:
    values = {
        "execution_price": execution_price,
        "fixed_fee": fixed_fee,
        "fee_rate": fee_rate,
        "assumption_source": assumption_source,
        "unknown_fields": tuple(
            name
            for name, value in (
                ("execution_price", execution_price),
                ("fixed_fee", fixed_fee),
                ("fee_rate", fee_rate),
            )
            if value is None
        ),
    }
    return TradeAssumptions.model_validate(values)


def fit_request(
    *,
    account_id: UUID | None = None,
    listing_id: int = 600000,
    action: str = "open",
    trade: TradeAssumptions | None = None,
) -> AccountFitRequest:
    selected_action = action
    if trade is None and selected_action != "hold":
        trade = assumptions()
    return AccountFitRequest(
        operation_id=uuid4(),
        account_id=account_id or UUID("00000000-0000-0000-0000-000000000101"),
        snapshot_ref=ObjectRef(
            kind="portfolio_snapshot",
            id=UUID("00000000-0000-0000-0000-000000000201"),
            revision=1,
        ),
        mandate_ref=ObjectRef(
            kind="mandate", id=UUID("00000000-0000-0000-0000-000000000301"), revision=1
        ),
        valuation_binding=ValuationBinding(state="missing", reason="synthetic_valuation_missing"),
        purpose_binding=PurposeBinding(state="none_recorded"),
        items=(
            FitRequestItem(
                listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
                action=selected_action,  # type: ignore[arg-type]
                trade_assumptions=trade,
            ),
        ),
    )

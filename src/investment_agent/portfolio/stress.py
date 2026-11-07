"""Pure Decimal arithmetic over frozen holdings, with no trade simulation."""

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal

from investment_agent.investor.common import Contract, DecimalValue, ObjectRef, Quantity
from investment_agent.portfolio.stress_contracts import FrozenIndustry, FrozenPath, StressShock
from investment_agent.portfolio.stress_shocks import AppliedShock, resolve_shock


class HoldingStressResult(Contract):
    listing_ref: ObjectRef
    shock: AppliedShock
    value_before: Quantity
    value_after: Quantity
    loss: Quantity
    weight_after: Quantity


class PathStressResult(Contract):
    path_name: str
    shock_name: str
    quality_state: Literal["complete", "partial"]
    quality: tuple[str, ...]
    holdings: tuple[HoldingStressResult, ...]
    known_loss_count: int
    known_securities_before: Quantity
    known_securities_after: Quantity
    known_securities_loss: Quantity
    nav_before: Quantity
    nav_after: Quantity
    total_loss: Quantity
    loss_ratio: Quantity


class DisplayAmount(Contract):
    value: DecimalValue
    rounding_delta: DecimalValue


def _money(value: Decimal | None, reason: str | None = None) -> Quantity:
    return Quantity(value=value, unit="CNY", currency="CNY", unknown_reason=reason if value is None else None)


def _ratio(numerator: Decimal | None, denominator: Decimal | None, reason: str | None) -> Quantity:
    if numerator is None or denominator is None:
        return Quantity(value=None, unit="ratio", unknown_reason=reason or "incomplete_inputs")
    if denominator <= 0:
        return Quantity(value=None, unit="ratio", unknown_reason="nonpositive_nav")
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        return Quantity(value=numerator / denominator, unit="ratio")


def _span(values: tuple[Decimal, ...]) -> int:
    integer = max((max(value.adjusted() + 1, 0) for value in values), default=1)
    fractional = max((max(-int(value.as_tuple().exponent), 0) for value in values), default=0)
    return integer + fractional


def amount_precision(path: FrozenPath, shock: StressShock) -> int:
    """Cover q*p*(1+s), sums and mixed decimal spans before any multiplication."""
    quantities = tuple(row.quantity for row in path.holdings if row.quantity is not None)
    prices = tuple(row.price for row in path.holdings if row.price is not None)
    shocks = tuple(item.shock for item in shock.industries) + tuple(item.shock for item in shock.listings)
    if shock.global_shock is not None:
        shocks += (shock.global_shock,)
    cash = () if path.cash is None else (path.cash,)
    return max(128, _span(quantities) + _span(prices) + _span(shocks) + _span(cash)
               + len(str(len(path.holdings) + 1)) + 8)


def display_amount(value: Decimal) -> DisplayAmount:
    """Display only; never feed individually rounded lines back into totals."""
    if not value.is_finite() or abs(int(value.as_tuple().exponent)) > 4096 or len(value.as_tuple().digits) > 4096:
        raise ValueError("Display amount must be a bounded finite Decimal")
    with localcontext(Context(prec=max(128, _span((value,)) + 4), rounding=ROUND_HALF_EVEN)):
        rounded = value.quantize(Decimal("0.01"))
        return DisplayAmount(value=rounded, rounding_delta=rounded - value)


def calculate_path(
    path: FrozenPath, shock: StressShock, industries: tuple[FrozenIndustry, ...] = (),
) -> PathStressResult:
    """A local valuation assumption, not liquidity, probability or execution."""
    # Saved alternatives have their own source copies. An explicit empty copy
    # means unknown; it cannot inherit the actual account's classification.
    if path.industries is not None:
        industries = path.industries
    elif path.source != "actual":
        industries = ()
    if len({item.listing_ref for item in industries}) != len(industries):
        raise ValueError("Frozen industry copies must be unambiguous")
    industry_by_listing = {item.listing_ref: item for item in industries}
    quality: list[str] = []
    if path.cash is None:
        quality.append("cash_missing")
    if path.coverage != "complete":
        quality.append("asset_scope_partial")
    with localcontext(Context(prec=amount_precision(path, shock), rounding=ROUND_HALF_EVEN)):
        rows: list[tuple[ObjectRef, AppliedShock, Quantity, Quantity, Quantity]] = []
        for holding in path.holdings:
            applied = resolve_shock(shock, holding.listing_ref, industry_by_listing.get(holding.listing_ref))
            if holding.quantity == 0:
                before = after = loss = _money(Decimal(0))
            elif holding.quantity is None or holding.price is None:
                reason = "quantity_missing" if holding.quantity is None else "price_missing"
                before = after = loss = _money(None, reason)
                quality.append(reason)
            else:
                value = holding.quantity * holding.price
                before = _money(value)
                if applied.value is None:
                    after = loss = _money(None, "industry_missing")
                    quality.append("industry_missing")
                else:
                    changed = value * (Decimal(1) + applied.value)
                    after, loss = _money(changed), _money(value - changed)
            rows.append((holding.listing_ref, applied, before, after, loss))
        known_before = sum((row[2].value for row in rows if row[2].value is not None), Decimal(0))
        known_after = sum((row[3].value for row in rows if row[3].value is not None), Decimal(0))
        known_loss = sum((row[4].value for row in rows if row[4].value is not None), Decimal(0))
        scope_reason = "cash_missing" if path.cash is None else "asset_scope_partial" if path.coverage != "complete" else None
        before_reason = scope_reason or next((row[2].unknown_reason for row in rows if row[2].value is None), None)
        after_reason = scope_reason or next((row[3].unknown_reason for row in rows if row[3].value is None), None)
        nav_before = _money(None, before_reason) if before_reason else _money(path.cash + known_before)  # type: ignore[operator]
        nav_after = _money(None, after_reason) if after_reason else _money(path.cash + known_after)  # type: ignore[operator]
        total_loss = _money(None, before_reason or after_reason) if before_reason or after_reason else _money(nav_before.value - nav_after.value)  # type: ignore[operator]
        return PathStressResult(
            path_name=path.name, shock_name=shock.name,
            quality_state="partial" if quality else "complete", quality=tuple(dict.fromkeys(quality)),
            holdings=tuple(HoldingStressResult(listing_ref=ref, shock=applied, value_before=before,
                           value_after=after, loss=loss, weight_after=_ratio(after.value, nav_after.value, after_reason))
                           for ref, applied, before, after, loss in rows),
            known_loss_count=sum(row[4].value is not None for row in rows),
            known_securities_before=_money(known_before), known_securities_after=_money(known_after),
            known_securities_loss=_money(known_loss), nav_before=nav_before, nav_after=nav_after,
            total_loss=total_loss, loss_ratio=_ratio(total_loss.value, nav_before.value, before_reason or after_reason),
        )

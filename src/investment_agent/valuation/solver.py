"""FCFE equity-value inversion. All arithmetic is Decimal, display rounding is separate."""

from decimal import Decimal, DecimalException, localcontext

from investment_agent.investor.common import Contract, DecimalValue
from investment_agent.investor.enums import CalculationStatus

METHOD_VERSION = "reverse_dcf_fcfe.v1"
LOW = Decimal("-0.95")
HIGH = Decimal("1")
WIDTH = Decimal("1e-8")
RESIDUAL = Decimal("1e-6")


class Solution(Contract):
    status: CalculationStatus
    growth: DecimalValue | None = None
    terminal_pv_fraction: DecimalValue | None = None
    relative_residual: DecimalValue | None = None
    interval_width: DecimalValue | None = None
    iterations: int = 0
    reason: str | None = None


def present_value(base: Decimal, growth: Decimal, rate: Decimal,
                  terminal_growth: Decimal) -> tuple[Decimal, Decimal]:
    """Five equal annual periods from as_of, then a Gordon terminal equity value."""
    cash, discount, interim = base, Decimal(1), Decimal(0)
    for _ in range(5):
        cash *= 1 + growth
        discount *= 1 + rate
        interim += cash / discount
    terminal = cash * (1 + terminal_growth) / (rate - terminal_growth) / discount
    return interim + terminal, terminal


def solve(market_value: Decimal, base: Decimal, rate: Decimal,
          terminal_growth: Decimal) -> Solution:
    """Bounded monotonic inversion; never expand the documented method interval."""
    if (not all(v.is_finite() for v in (market_value, base, rate, terminal_growth))
        or market_value <= 0 or base <= 0 or not 0 <= terminal_growth < rate):
        return Solution(status=CalculationStatus.CALCULATION_ERROR, reason="invalid_parameters")
    try:
        with localcontext() as ctx:
            ctx.prec = 40
            lo, hi = LOW, HIGH
            low_value = present_value(base, lo, rate, terminal_growth)[0]
            high_value = present_value(base, hi, rate, terminal_growth)[0]
            if market_value < low_value or market_value > high_value:
                return Solution(status=CalculationStatus.OUTSIDE_SEARCH_RANGE,
                                reason="below_search_range" if market_value < low_value
                                else "above_search_range")
            for iteration in range(1, 201):
                middle = (lo + hi) / 2
                value, terminal = present_value(base, middle, rate, terminal_growth)
                error = abs(value - market_value) / market_value
                width = hi - lo
                if width <= WIDTH and error <= RESIDUAL:
                    return Solution(status=CalculationStatus.OK, growth=middle,
                                    terminal_pv_fraction=terminal / value,
                                    relative_residual=error, interval_width=width,
                                    iterations=iteration)
                if value < market_value:
                    lo = middle
                else:
                    hi = middle
            return Solution(status=CalculationStatus.CALCULATION_ERROR,
                            reason="non_convergence", iterations=200)
    except DecimalException:
        return Solution(status=CalculationStatus.CALCULATION_ERROR, reason="decimal_error")

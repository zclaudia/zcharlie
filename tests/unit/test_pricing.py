from decimal import Decimal

from investment_agent.llm.pricing import calculate_cost, get_pricing


def test_pricing_known_model():
    p = get_pricing("gpt-4o-mini")
    assert p.input_per_1m > 0
    assert p.output_per_1m > 0


def test_pricing_unknown_model_returns_default():
    p = get_pricing("never-heard-of-this-model")
    # default: assume 1.0 USD per 1M input, 3.0 USD per 1M output (conservative)
    assert p.input_per_1m == Decimal("1.0")
    assert p.output_per_1m == Decimal("3.0")


def test_calculate_cost():
    cost = calculate_cost(
        model="gpt-4o-mini", prompt_tokens=10_000, completion_tokens=2_000
    )
    # gpt-4o-mini: $0.15/M input, $0.60/M output
    # 10k * 0.15/M + 2k * 0.60/M = 0.0015 + 0.0012 = 0.0027
    assert cost == Decimal("0.0027")

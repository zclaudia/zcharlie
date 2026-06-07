"""Per-model pricing (USD per 1M tokens). Keep tightly aligned to provider docs."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m: Decimal
    output_per_1m: Decimal


_PRICING: dict[str, ModelPricing] = {
    "gpt-5":           ModelPricing(Decimal("5.00"),  Decimal("15.00")),
    "gpt-4o":          ModelPricing(Decimal("2.50"),  Decimal("10.00")),
    "gpt-4o-mini":     ModelPricing(Decimal("0.15"),  Decimal("0.60")),
    "claude-opus-4-7": ModelPricing(Decimal("15.00"), Decimal("75.00")),
    "claude-sonnet-4-6": ModelPricing(Decimal("3.00"), Decimal("15.00")),
    "deepseek-chat":   ModelPricing(Decimal("0.14"),  Decimal("0.28")),
    "deepseek-reasoner": ModelPricing(Decimal("0.55"), Decimal("2.19")),
}

_DEFAULT = ModelPricing(Decimal("1.0"), Decimal("3.0"))


def get_pricing(model: str) -> ModelPricing:
    return _PRICING.get(model, _DEFAULT)


def calculate_cost(
    *, model: str, prompt_tokens: int, completion_tokens: int
) -> Decimal:
    p = get_pricing(model)
    return (
        Decimal(prompt_tokens) * p.input_per_1m / Decimal(1_000_000)
        + Decimal(completion_tokens) * p.output_per_1m / Decimal(1_000_000)
    )

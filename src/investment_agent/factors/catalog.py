"""The default factor registry: every factor rules may reference."""

from __future__ import annotations

from investment_agent.factors.library import CORE_FACTORS
from investment_agent.factors.registry import FactorRegistry
from investment_agent.factors.valuation import VALUATION_FACTORS


def build_default_registry() -> FactorRegistry:
    return FactorRegistry((*CORE_FACTORS, *VALUATION_FACTORS))


DEFAULT_REGISTRY = build_default_registry()

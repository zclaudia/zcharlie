"""Most-specific static assumption selection over copied industry inputs."""

from decimal import Decimal
from typing import Literal

from pydantic import model_validator

from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.portfolio.stress_contracts import (
    FrozenIndustry,
    ShockRatio,
    StressShock,
    exact,
)


class AppliedShock(Contract):
    source: Literal["listing", "industry", "global", "unmatched_zero", "unknown_industry"]
    value: ShockRatio | None
    provenance: Literal["user_scenario"] = "user_scenario"
    industry_code: str | None = None
    unknown_reason: Literal["industry_missing"] | None = None

    @model_validator(mode="after")
    def explicit_unknown(self) -> "AppliedShock":
        if (self.source == "unknown_industry") != (self.value is None):
            raise ValueError("Only an unresolved industry target has an unknown shock")
        if (self.value is None) != (self.unknown_reason is not None):
            raise ValueError("Unknown shock requires its reason")
        return self


def resolve_shock(
    shock: StressShock, listing_ref: ObjectRef, industry: FrozenIndustry | None,
) -> AppliedShock:
    """Do not add overlapping shocks or guess membership from current data."""
    exact(listing_ref, "listing", immutable=True)
    if industry is not None and industry.listing_ref != listing_ref:
        raise ValueError("Frozen industry must belong to the exact listing")
    for listing_target in shock.listings:
        if listing_target.listing_ref == listing_ref:
            return AppliedShock(source="listing", value=listing_target.shock)
    if shock.industries:
        if industry is None:
            # Unknown membership may match an overriding industry target. The
            # global fallback would silently invent a different assumption.
            return AppliedShock(source="unknown_industry", value=None, unknown_reason="industry_missing")
        for industry_target in shock.industries:
            if industry_target.code == industry.code:
                return AppliedShock(source="industry", value=industry_target.shock, industry_code=industry.code)
    if shock.global_shock is not None:
        return AppliedShock(source="global", value=shock.global_shock)
    return AppliedShock(source="unmatched_zero", value=Decimal(0))

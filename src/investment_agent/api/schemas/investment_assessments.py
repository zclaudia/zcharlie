"""HTTP read models; writes reuse the frozen domain contracts directly."""

from investment_agent.candidates.ranking_contracts import CandidateSource, RankingEntry
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.portfolio.suitability import CurrentLimits
from investment_agent.portfolio.suitability_contracts import FrozenListingSource
from investment_agent.portfolio.suitability_inputs import FitContext


class AssessmentFeatureFlags(Contract):
    account_fit: bool
    candidate_ranking: bool
    portfolio_ledger: bool


class AssessmentListing(Contract):
    listing_ref: ObjectRef
    ticker: str
    name: str


class FitContextResponse(FitContext):
    enabled: AssessmentFeatureFlags
    listing: AssessmentListing | None
    source_templates: tuple[str, ...]
    listing_source: FrozenListingSource | None = None
    supported_trade_policies: tuple[str, ...] = ("per_listing", "valuation_price_as_scenario")


class RankingEntryResponse(Contract):
    entry: RankingEntry
    sources: tuple[CandidateSource, ...]
    current_limits: CurrentLimits | None

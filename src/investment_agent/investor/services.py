"""Application service boundaries; implementations arrive in AG04/AG12/AG14."""

from typing import Protocol
from uuid import UUID

from investment_agent.investor.common import Mutation, OwnerId
from investment_agent.investor.contracts import (
    Case,
    CompanyRecommendation,
    Mandate,
    Memory,
    PublicationCheck,
    RecommendationView,
    UserDecision,
)


class InvestorQueries(Protocol):
    async def get_mandate(self, user_id: OwnerId) -> Mandate: ...
    async def get_case(self, user_id: OwnerId, case_id: UUID) -> Case: ...
    async def get_recommendation(
        self, user_id: OwnerId, recommendation_id: UUID
    ) -> RecommendationView: ...
    async def get_memories(self, user_id: OwnerId) -> tuple[Memory, ...]: ...


class InvestorWrites(Protocol):
    async def revise_mandate(
        self, user_id: OwnerId, candidate: Mandate, mutation: Mutation
    ) -> Mandate: ...
    async def publish_company_view(
        self, user_id: OwnerId, candidate: CompanyRecommendation, mutation: Mutation
    ) -> PublicationCheck: ...
    async def save_decision(
        self, user_id: OwnerId, decision: UserDecision, idempotency_key: UUID
    ) -> UserDecision: ...
    async def revoke_memory(
        self, user_id: OwnerId, memory_id: UUID, mutation: Mutation
    ) -> Memory: ...

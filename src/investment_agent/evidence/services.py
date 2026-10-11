"""Read and saved-record boundaries, without data-source or database implementations."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord, ToolResult
from investment_agent.investor.common import ObjectRef, OwnerId


class EvidenceQueries(Protocol):
    async def get_evidence(
        self,
        user_id: OwnerId,
        ref: ObjectRef,
        as_of: datetime,
        *,
        visible_at: datetime | None = None,
    ) -> ToolResult[EvidenceRecord]: ...
    async def get_calculation(
        self,
        user_id: OwnerId,
        ref: ObjectRef,
        as_of: datetime,
        *,
        visible_at: datetime | None = None,
    ) -> ToolResult[CalculationRecord]: ...


class EvidenceWrites(Protocol):
    async def save_evidence(
        self, user_id: OwnerId, record: EvidenceRecord, operation_id: UUID
    ) -> ObjectRef: ...
    async def save_calculation(
        self, user_id: OwnerId, record: CalculationRecord, operation_id: UUID
    ) -> ObjectRef: ...
